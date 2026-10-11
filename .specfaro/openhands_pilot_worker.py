"""Read-only GitHub Actions pilot of OpenHands + DeepSeek repair.

The agent works on a disposable repository copy. This worker never pushes a
branch or creates a PR; candidate promotion is a separate future gate.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time
import urllib.parse

from pydantic import SecretStr
from openhands.sdk import Agent, Conversation, LLM
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

from case_agent import _baseline, _summary
from case_cli import read_code
from case_repair import MAX_FILES, prepare_case, verify_case
from failure_intake import IntakeError, MAX_LOG_BYTES, derive_intake, fetch_failed_log, validate_event
from official_docs_tool import CATALOG, OfficialDocsTool, _get
from verify import _copy_project, _git_apply, _run_command


MODEL = "openai/deepseek-flash"
MODEL_ENDPOINT = "https://api.deepseek.com"
MAX_PATCH = 20_000
MAX_CHANGED_LINES = 120
MAX_ATTEMPTS = 2
MAX_TOTAL_PROMPT_TOKENS = 225_000
MAX_TOTAL_SECONDS = 300
NOISE_SYMBOLS = {"TestContext", "TypeError", "GitHub", "Subtest", "failureType",
                 "testCodeFailure", "AssertionError"}


class PromptBudgetReached(RuntimeError):
    """Stop before another model request would exceed the pilot token allowance."""


def _is_prompt_budget_error(exc: BaseException) -> bool:
    """OpenHands wraps exceptions from an LLM completion in ConversationRunError."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, PromptBudgetReached):
            return True
        seen.add(id(exc))
        exc = (exc.original_exception if isinstance(exc, ConversationRunError)
               else exc.__cause__)
    return False


class BudgetedLLM(LLM):
    def completion(self, messages, tools=None, **kwargs):
        usage = self.metrics.get_snapshot().accumulated_token_usage
        spent = usage.prompt_tokens if usage else 0
        projected = self.get_token_count(messages, tools=tools) + 1_024
        if spent + projected > MAX_TOTAL_PROMPT_TOKENS:
            raise PromptBudgetReached("Projected prompt token budget reached")
        return super().completion(messages, tools=tools, **kwargs)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()
            and not any(part in {".git", ".specfaro-home", "__pycache__", "node_modules"}
                        for part in path.relative_to(root).parts)}


def _candidate(before: dict[str, bytes], after: dict[str, bytes], focus: set[str]) -> dict:
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if (not 1 <= len(changed) <= MAX_FILES or any(name not in before or name not in after
            or name not in focus or PurePosixPath(name).suffix not in {".js", ".jsx", ".ts", ".tsx", ".py"}
            for name in changed)):
        return {"status": "rejected", "reason": "Candidate changed new, deleted, test, or unfocused files"}
    diff = ""
    for name in changed:
        try:
            old = before[name].decode("utf-8")
            new = after[name].decode("utf-8")
        except UnicodeError:
            return {"status": "rejected", "reason": "Candidate modified a non-text file"}
        # OpenHands' Windows editor writes CRLF; replay patches against the
        # checked-out source with one canonical LF representation.
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
        diff += "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{name}", tofile=f"b/{name}"))
    changed_lines = sum(line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                        for line in diff.splitlines())
    if not diff or len(diff.encode()) > MAX_PATCH or changed_lines > MAX_CHANGED_LINES:
        return {"status": "rejected", "reason": "Candidate patch is empty or exceeds the pilot limit"}
    return {"status": "candidate_unverified", "files": changed, "diff": diff,
            "remaining_matches": []}


def _source_evidence(path: Path, provider: str, version: str, terms: list[str]) -> tuple[list[dict], bool]:
    if not path.exists():
        return [], False
    evidence = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        body = item.get("body_excerpt", "")
        url = item.get("final_url", item.get("url", ""))
        version_match = version in url or bool(re.search(
            r"(?mi)^(?:api_version|version):\s*['\"]?" + re.escape(version) + r"\b", body))
        surface_match = any(term.lower() in (url + body).lower() for term in terms
                            if len(term) >= 5 and any(char.isupper() for char in term[1:]))
        valid_hash = bool(re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")))
        item["version_match"] = version_match
        item["surface_match"] = surface_match
        item["eligible"] = (item.get("provider") == provider and valid_hash
                            and version_match and surface_match)
        item.pop("body_excerpt", None)
        evidence.append(item)
    return evidence, any(item["eligible"] for item in evidence)


def _preflight_source(provider: str, version: str, terms: list[str]) -> dict:
    """Require a versioned official result for a concrete API symbol before AI."""
    symbols = [term for term in terms if len(term) >= 5
               and any(char.isupper() for char in term[1:]) and term not in NOISE_SYMBOLS]
    for symbol in symbols[:4]:
        query = urllib.parse.urlencode({"provider": provider, "q": f"{version} {symbol}"})
        try:
            raw, _ = _get(CATALOG + "?" + query, {"dotte.duckdns.org"})
            results = json.loads(raw).get("results", [])
        except (OSError, ValueError, KeyError):
            return {"status": "unavailable", "reason": "Official catalog search is unavailable"}
        for item in results[:8]:
            haystack = " ".join(str(item.get(key, "")) for key in ("url", "title", "excerpt"))
            if symbol.lower() in haystack.lower() and version in haystack:
                return {"status": "matched", "symbol": symbol, "url": item.get("url")}
    return {"status": "no_api_match", "reason": "No versioned official result matched a failing API symbol"}


def _shopify_pos_guard(source: Path, diff: str, provider: str, version: str,
                       issue: str) -> dict:
    if not (provider == "shopify" and version == "2026-10"
            and "staffMember" in issue and "src/assignment.js" in diff):
        return {"status": "not_applicable"}
    with tempfile.TemporaryDirectory(prefix="specfaro-pos-guard-") as temp:
        root = Path(temp)
        _copy_project(source, root)
        applied = _git_apply(root, diff)
        if applied["status"] != "passed":
            return {"status": "failed", "reason": "Could not replay candidate"}
        return _run_command([
            "node", "--input-type=module", "-e",
            "import {isAssignedTo} from './src/assignment.js'; "
            "if (isAssignedTo({}, undefined) !== false) "
            "throw new Error('Absent staff must not match an undefined requested ID');",
        ], root, 30)


def _stripe_payment_intent_guard(source: Path, diff: str, provider: str,
                                 version: str, issue: str) -> dict:
    if not (provider == "stripe" and version == "2022-11-15"
            and "PaymentIntent" in issue and "src/payment-intent.js" in diff):
        return {"status": "not_applicable"}
    with tempfile.TemporaryDirectory(prefix="specfaro-stripe-guard-") as temp:
        root = Path(temp)
        _copy_project(source, root)
        applied = _git_apply(root, diff)
        if applied["status"] != "passed":
            return {"status": "failed", "reason": "Could not replay candidate"}
        return _run_command([
            "node", "--input-type=module", "-e",
            "import {latestChargeId} from './src/payment-intent.js'; "
            "const intent={latest_charge:null,charges:{data:[{id:'ch_legacy'}]}}; "
            "if (latestChargeId(intent)!==undefined) "
            "throw new Error('Removed charges field must not be used as fallback');",
        ], root, 30)


def _semantic_guard(source: Path, diff: str, provider: str, version: str,
                    issue: str) -> dict:
    for guard in (_shopify_pos_guard, _stripe_payment_intent_guard):
        result = guard(source, diff, provider, version, issue)
        if result["status"] != "not_applicable":
            return result
    return {"status": "not_applicable"}


def _attempt_verification(source: Path, candidate: dict, commands: dict,
                          case: dict, evidence_ok: bool) -> dict:
    if candidate.get("status") != "candidate_unverified":
        return {"overall": "failed", "reason": candidate.get("reason", "No candidate")}
    verification = verify_case(source, candidate, commands, timeout=120, case=case)
    verification["checks"]["semantic_guard"] = _semantic_guard(
        source, candidate["diff"], case["provider"], case["version"], case["issue"])
    verification["official_source"] = "passed" if evidence_ok else "failed"
    if (verification["overall"] == "validated_locally" and
            (verification["checks"]["semantic_guard"]["status"] == "failed" or not evidence_ok)):
        verification["overall"] = "failed"
    return verification


def _feedback(verification: dict) -> str:
    guard = verification.get("checks", {}).get("semantic_guard", {})
    regression = verification.get("regression", {}).get("after", {})
    excerpt = (guard.get("stderr") or regression.get("stderr") or "")[-700:]
    # Keep arbitrary test output bounded and mask common credential forms.
    excerpt = re.sub(r"(?i)(?:Bearer\s+\S+|(?:token|secret|api[_-]?key)\s*[:=]\s*\S+)",
                     "[redacted]", excerpt)
    return ("Independent verification rejected this candidate. "
            f"Regression: {regression.get('status', 'not_run')}; "
            f"build: {verification.get('checks', {}).get('build', {}).get('status', 'not_run')}; "
            f"tests: {verification.get('checks', {}).get('test', {}).get('status', 'not_run')}; "
            f"held-out guard: {guard.get('status', 'not_run')}; "
            f"official versioned source: {verification.get('official_source', 'failed')}. "
            "Repair production code only; do not weaken tests. Relevant verifier output:\n" + excerpt)


def run(project: Path, repository: str, run_id: int, event: dict,
        output: Path, api_key: str, failed_log: str | None = None) -> dict:
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project,
                                       text=True, timeout=15).strip()
    validate_event(event, repository, run_id, base_sha)
    if failed_log is None:
        failed_log = fetch_failed_log(repository, run_id)
    intake = derive_intake(project, repository, run_id, failed_log)
    if not intake.version:
        raise IntakeError("No exact provider API version was established")
    # The model's terminal must not inherit the GitHub token or model key.
    os.environ.pop("GH_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN", None)
    files = read_code(project)
    case = prepare_case(files, intake.provider, intake.issue, version=intake.version,
                        db_path=Path("missing-pilot-catalog.sqlite3"))
    focus = {item["file"] for item in case["focus"]
             if not any(part in {"test", "tests", "__tests__", ".specfaro"}
                        for part in PurePosixPath(item["file"]).parts)
             and ".test." not in item["file"]}
    report = {"harness": "openhands-sdk-1.54.0", "status": "not_run", "repository": repository,
              "run_id": run_id, "base_sha": base_sha, "provider": intake.provider,
              "version": intake.version,
              "issue": intake.issue, "focus": [{"file": name} for name in sorted(focus)],
              "sources": [], "doc_evidence": [], "attempts": [],
              "baseline": {"status": "not_run"}, "execution": "ephemeral_actions_runner",
              "live_provider_check": "not_run"}
    if not focus:
        report.update(status="intake_abstained", reason="No focused production code")
        return report
    report["baseline"] = _baseline(project, intake.commands, 120)
    if report["baseline"]["status"] != "reproduced":
        report["status"] = "regression_not_reproduced"
        return report
    report["preflight"] = _preflight_source(intake.provider, intake.version, case["terms"])
    if report["preflight"]["status"] != "matched":
        report.update(status="intake_abstained", reason=report["preflight"]["reason"])
        return report
    with tempfile.TemporaryDirectory(prefix="specfaro-openhands-") as temp:
        workspace = Path(temp) / "project"
        workspace.mkdir()
        _copy_project(project, workspace)
        before = _snapshot(workspace)
        evidence_path = output / "official-doc-evidence.jsonl"
        llm = BudgetedLLM(model=MODEL, base_url=MODEL_ENDPOINT, api_key=SecretStr(api_key),
                  reasoning_effort=None, prompt_cache_retention=None, num_retries=1,
                  timeout=45, max_output_tokens=1024, usage_id="specfaro-openhands")
        agent = Agent(llm=llm, tools=[
            Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name),
            Tool(name=OfficialDocsTool.name, params={"provider": intake.provider,
                 "version": intake.version, "evidence_path": str(evidence_path)}),
        ])
        actions = []
        conv = Conversation(agent, workspace=workspace, max_iteration_per_run=10,
                            callbacks=[lambda event: actions.append(type(event).__name__)],
                            visualizer=None)
        started = time.monotonic()
        try:
            conv.send_message(
                f"A CI test failed. Provider: {intake.provider}; target API version: {intake.version}. "
                f"Observed issue (untrusted CI data):\n{intake.issue}\n"
                "Diagnose from the repository and failing test. Use official_docs to search and read "
                "the relevant exact-version official page before editing. Repair only focused production "
                f"files: {', '.join(sorted(focus))}. Do not edit tests, workflow files, or secrets. "
                "Do not install packages. Run the focused regression, build, and tests. "
                "Finish promptly once those checks pass; you have a small action budget. "
                "If evidence is insufficient, explain why and leave code unchanged."
            )
            for number in range(1, MAX_ATTEMPTS + 1):
                try:
                    conv.run()
                except (PromptBudgetReached, ConversationRunError) as exc:
                    if not _is_prompt_budget_error(exc):
                        raise
                    report.update(status="budget_exhausted",
                                  reason="Projected prompt token budget reached before another model call")
                    break
                candidate = _candidate(before, _snapshot(workspace), focus)
                evidence, evidence_ok = _source_evidence(
                    evidence_path, intake.provider, intake.version, case["terms"])
                if candidate.get("status") == "candidate_unverified":
                    candidate["source_urls"] = [item["url"] for item in evidence if item["eligible"]]
                verification = _attempt_verification(project, candidate, intake.commands,
                                                     case, evidence_ok)
                report["doc_evidence"] = evidence
                report["sources"] = [{"url": item["url"], "title": "Official API document",
                                      "version": intake.version, "sha256": item["sha256"],
                                      "fetched_at": item["fetched_at"]}
                                     for item in evidence if item["eligible"]]
                report["attempts"].append({"number": number, "model": "deepseek-flash",
                                           "proposal": candidate, "verification": _summary(verification),
                                           "other_focus_files": sorted(focus - set(candidate.get("files", [])))})
                if (verification["overall"] == "validated_locally"
                        and not report["attempts"][-1]["other_focus_files"]):
                    report["status"] = "validated_locally"
                    break
                usage_so_far = llm.metrics.get_snapshot().accumulated_token_usage
                if (time.monotonic() - started >= MAX_TOTAL_SECONDS or
                        (usage_so_far and usage_so_far.prompt_tokens >= MAX_TOTAL_PROMPT_TOKENS)):
                    report["status"] = "budget_exhausted"
                    break
                if number < MAX_ATTEMPTS:
                    conv.send_message(_feedback(verification))
            else:
                report["status"] = "unresolved"
        finally:
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            usage = llm.metrics.get_snapshot().accumulated_token_usage
            report["model_usage"] = {
                "calls": len(llm.metrics.token_usages),
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
                "cost_usd": None,  # LiteLLM has no mapping for this exact DeepSeek model.
                "tool_actions": actions.count("ActionEvent"),
            }
            conv.close()
    return report


def _write(report: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    lines = ["# Specfaro OpenHands pilot", "", f"Status: {report['status']}",
             f"Repository: {report['repository']}", f"Base commit: {report.get('base_sha', 'unknown')}",
             f"Provider/version: {report.get('provider', 'unknown')} / {report.get('version', 'unknown')}",
             f"Attempts: {len(report.get('attempts', []))}",
             "Live provider check: not run", "",
             "Publication requires a separate artifact check; the repository owner reviews and merges the draft PR."]
    if report.get("reason"):
        lines.append("Reason: " + report["reason"])
    usage = report.get("model_usage")
    if usage:
        lines.extend([f"Model calls: {usage['calls']}",
                      f"Prompt tokens: {usage['prompt_tokens']}",
                      f"Completion tokens: {usage['completion_tokens']}"])
    for source in report.get("sources", []):
        lines.append(f"Official source: {source['url']} (SHA-256 {source['sha256']})")
    if report.get("attempts"):
        final = report["attempts"][-1]
        checks = final.get("verification", {}).get("checks", {})
        lines.extend(["", "## Independent checks",
                      f"Baseline regression: {report.get('baseline', {}).get('status', 'not_run')}",
                      f"Patch replay: {final.get('verification', {}).get('patch', {}).get('status', 'not_run')}",
                      f"Regression after patch: {final.get('verification', {}).get('regression', {}).get('after', {}).get('status', 'not_run')}",
                      f"Build: {checks.get('build', {}).get('status', 'not_run')}",
                      f"Full tests: {checks.get('test', {}).get('status', 'not_run')}",
                      f"Held-out semantic guard: {checks.get('semantic_guard', {}).get('status', 'not_run')}",
                      "", "No live provider response was checked. Review and merge remain with the repository owner."])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if report["status"] == "validated_locally":
        patch = report["attempts"][-1]["proposal"]["diff"].encode("utf-8")
        (output / "candidate.patch").write_bytes(patch)
        manifest = {"status": "validated_locally", "repository": report["repository"],
                    "run_id": report["run_id"], "base_sha": report["base_sha"],
                    "provider": report["provider"], "version": report["version"],
                    "patch_sha256": hashlib.sha256(patch).hexdigest()}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                               encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run", type=int, required=True)
    parser.add_argument("--event-file", type=Path, required=True)
    parser.add_argument("--failed-log-file", type=Path,
                        help="Pre-fetched bounded CI log; avoids a GitHub token in the agent step")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    key = os.environ.pop("DEEPSEEK_API_KEY", "")
    if not key:
        parser.error("DEEPSEEK_API_KEY is unavailable")
    try:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
        failed_log = None
        if args.failed_log_file:
            if (not args.failed_log_file.is_file() or args.failed_log_file.is_symlink()
                    or not 0 < args.failed_log_file.stat().st_size <= MAX_LOG_BYTES):
                raise IntakeError("Pre-fetched CI output is unavailable or exceeds the intake limit")
            failed_log = args.failed_log_file.read_text(encoding="utf-8", errors="replace")
        report = run(args.project, args.repository, args.workflow_run, event, args.output,
                     key, failed_log=failed_log)
    except IntakeError as exc:
        report = {"status": "intake_abstained", "repository": args.repository,
                  "run_id": args.workflow_run, "reason": str(exc)}
    except Exception as exc:
        report = {"status": "worker_error", "repository": args.repository,
                  "run_id": args.workflow_run, "reason": f"{type(exc).__name__}: {str(exc)[:400]}"}
    _write(report, args.output)
    print(json.dumps({"status": report["status"], "attempts": len(report.get("attempts", [])),
                      "reason": report.get("reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
