"""Bounded local reproduce, propose, test, retry workflow for issue-led repairs.

Repository commands run only when an operator supplies them on a trusted runner.
This module is never called by the public web service.
"""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from case_repair import review_case, verify_case
from deepseek_review import DeepSeekError
from verify import _copy_project, _run_command

REQUIRED_COMMANDS = ("regression", "build", "test")


def _production_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return not (any(part in {"tests", "test", "__tests__"} or part.startswith("test_") for part in parts)
                or ".test." in path or ".spec." in path)


def _summary(value: object) -> object:
    """Keep command output out of saved and printed reports by default."""
    if isinstance(value, dict):
        return {key: _summary(item) for key, item in value.items()
                if key not in {"stdout", "stderr"}}
    if isinstance(value, list):
        return [_summary(item) for item in value]
    return value


def _baseline(source: Path, commands: dict, timeout: int,
              run_command: Callable[[list[str], Path, int], dict] = _run_command) -> dict:
    with tempfile.TemporaryDirectory(prefix="specfaro-baseline-") as temporary:
        workspace = Path(temporary) / "project"
        workspace.mkdir()
        copied = _copy_project(source.resolve(), workspace)
        install = run_command(commands["install"], workspace, timeout) if "install" in commands else {"status": "not_run"}
        if install["status"] == "failed":
            return {"status": "setup_failed", "project_copy": copied, "install": _summary(install)}
        regression = run_command(commands["regression"], workspace, timeout)
        return {"status": "reproduced" if regression["status"] == "failed" else "not_reproduced",
                "project_copy": copied, "install": _summary(install), "regression": _summary(regression)}


def run_agent(source: Path, files: dict[str, str], case: dict, commands: dict,
              *, max_attempts: int = 2, timeout: int = 180,
              share_test_output: bool = False,
              reviewer: Callable[..., dict] | None = None) -> dict:
    """Stop only after a reproduced regression passes or an explicit limit is reached."""
    if not source.is_dir() or not isinstance(commands, dict) or not 1 <= max_attempts <= 3 or not 1 <= timeout <= 900:
        raise ValueError("Invalid project, commands, attempt limit, or timeout")
    if any(stage not in commands or not isinstance(commands[stage], list) for stage in REQUIRED_COMMANDS):
        raise ValueError("Agent mode requires regression, build, and test command arrays")
    report = {"provider": case["provider"], "version": case["version"], "issue": case["issue"],
              "sources": [{key: item.get(key) for key in ("url", "title", "version", "category")}
                          for item in case["sources"]],
              "focus": [{"file": item["file"], "line": item["line"]} for item in case["focus"]],
              "scanned_files": len(files), "baseline": {"status": "not_run"}, "attempts": [],
              "status": "not_run", "execution": "local_temporary_copy",
              "live_provider_check": "not_run"}
    if case["status"] != "ready":
        report["status"] = "no_code_match"
        return report
    report["baseline"] = _baseline(source, commands, timeout)
    if report["baseline"]["status"] != "reproduced":
        report["status"] = "setup_failed" if report["baseline"]["status"] == "setup_failed" else "regression_not_reproduced"
        return report

    feedback = None
    for number in range(1, max_attempts + 1):
        try:
            ai = (reviewer or review_case)(files, case, feedback=feedback)
        except DeepSeekError as exc:
            report["attempts"].append({"number": number, "status": "ai_error", "reason": str(exc)})
            report["status"] = "ai_error"
            return report
        candidate = ai.get("proposal", {})
        attempt = {"number": number, "model": ai.get("model"), "summary": ai.get("summary"),
                   "proposal": candidate, "verification": {"overall": "not_run"}}
        report["attempts"].append(attempt)
        if candidate.get("status") != "candidate_unverified":
            feedback = {"attempt": number, "proposal_status": candidate.get("status"),
                        "reason": candidate.get("reason")}
            if candidate.get("status") == "none":
                report["status"] = "insufficient_evidence"
                return report
            continue
        edited = set(candidate.get("files", [candidate.get("file")]))
        attempt["other_focus_files"] = [item["file"] for item in case["focus"]
                                        if item["file"] not in edited and _production_file(item["file"])]
        verification = verify_case(source, candidate, commands, timeout=timeout, case=case)
        attempt["verification"] = _summary(verification)
        if verification["overall"] in {"validated_locally", "review_required"}:
            report["status"] = ("review_required" if candidate.get("remaining_matches") or attempt["other_focus_files"]
                                else verification["overall"])
            return report
        feedback = {"attempt": number, "previous_files": candidate.get("files", [candidate.get("file")]),
                    "previous_diff": candidate.get("diff", "")[:1800],
                    "verification": _summary(verification)}
        if share_test_output:
            checks = verification.get("checks", {})
            feedback["test_output"] = {
                stage: {key: item.get(key, "")[-700:] for key in ("stdout", "stderr")}
                for stage, item in checks.items() if item.get("status") == "failed"}
            feedback["regression_output"] = {
                key: verification.get("regression", {}).get("after", {}).get(key, "")[-700:]
                for key in ("stdout", "stderr")}
    report["status"] = "unresolved"
    return report


def markdown_report(report: dict) -> str:
    """Optional human-readable report; command output is intentionally omitted."""
    lines = ["# Specfaro issue repair report", "", f"**Result:** {report['status']}",
             f"**Provider:** {report['provider']}", f"**Target version:** {report['version'] or 'unspecified'}",
             f"**Execution:** {report['execution']}", f"**Live provider check:** {report['live_provider_check']}",
             "", "## Reported issue", "", report["issue"], "", "## Evidence", ""]
    for source in report["sources"]:
        lines.append(f"- [{source['title'] or 'Official source'}]({source['url']}) — {source['version'] or 'version unspecified'}")
    if not report["sources"]:
        lines.append("- No relevant fresh official document was retrieved.")
    lines.extend(("", "## Reproduction and attempts", "",
                  f"Baseline regression: {report['baseline']['status']}"))
    for attempt in report["attempts"]:
        proposal = attempt.get("proposal", {})
        lines.append(f"- Attempt {attempt['number']}: proposal {proposal.get('status', attempt.get('status', 'unknown'))}; "
                     f"verification {attempt.get('verification', {}).get('overall', 'not_run')}")
        if proposal.get("files") or proposal.get("file"):
            lines.append(f"  - Proposed files: {', '.join(proposal.get('files', [proposal.get('file')]))}")
        verification = attempt.get("verification", {})
        regression = verification.get("regression", {})
        if regression:
            lines.append(f"  - Regression: {regression.get('before', {}).get('status', 'not_run')} before; "
                         f"{regression.get('after', {}).get('status', 'not_run')} after")
        for stage in ("build", "test", "schema"):
            check = verification.get("checks", {}).get(stage, {})
            if check:
                lines.append(f"  - {stage.capitalize()}: {check.get('status', 'not_run')}"
                             + (f" (exit {check['exit_code']})" if "exit_code" in check else ""))
        for check in proposal.get("required_checks", []):
            lines.append(f"  - Required check: {check}")
        if attempt.get("other_focus_files"):
            lines.append(f"  - Other likely affected files: {', '.join(attempt['other_focus_files'])}")
        for check in attempt.get("verification", {}).get("manual_checks", []):
            lines.append(f"  - Manual check: {check['id']} — {check['detail']}")
    final_proposal = report["attempts"][-1].get("proposal", {}) if report["attempts"] else {}
    if final_proposal.get("diff"):
        lines.extend(("", "## Candidate patch", "", "~~~diff", final_proposal["diff"].rstrip(), "~~~"))
    lines.extend(("", "## Interpretation", "",
                  "A local pass means the supplied regression failed before the candidate and passed after it, "
                  "and the supplied build and test commands passed. It does not establish live provider behavior. "
                  "A review-required result still has the listed manual checks. No repository source was changed."))
    return "\n".join(lines) + "\n"
