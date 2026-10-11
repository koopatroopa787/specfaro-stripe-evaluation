"""Replay a model candidate in a fresh, read-only GitHub Actions job.

This process receives no model key or write-capable GitHub token. It treats the
repair artifact as untrusted and derives test commands from the failed CI log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import URLError

from case_repair import verify_case
from failure_intake import MAX_LOG_BYTES, derive_intake, validate_event
from openhands_publish import OFFICIAL_HOST, validate_artifact
from verify import _copy_project, _git_apply, _run_command


MAX_SOURCE_BYTES = 500_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _read_log(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_LOG_BYTES:
        raise ValueError("Failed CI log is missing or exceeds the intake limit")
    return path.read_text(encoding="utf-8", errors="replace")


def _check_sources(report: dict) -> list[str]:
    """Re-fetch exact official documents; stale or changed content fails closed."""
    provider = report["provider"]
    expected = {(item["url"], item["sha256"]) for item in report["sources"]}
    evidence = report["doc_evidence"]
    verified = set()
    for item in evidence:
        if not isinstance(item, dict) or not item.get("eligible"):
            continue
        pair = (item.get("url"), item.get("sha256"))
        if pair not in expected:
            continue
        final_url = item.get("final_url")
        parsed = urlsplit(final_url or "")
        if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST[provider]
                or parsed.username or parsed.password or parsed.port or parsed.fragment):
            raise ValueError("Official source URL is outside the allowed host")
        request = Request(final_url, headers={"User-Agent": "specfaro-independent-verifier/0.1"})
        for attempt in range(2):
            try:
                with build_opener(_NoRedirect).open(request, timeout=15) as response:
                    if response.geturl() != final_url:
                        raise ValueError("Official source redirected during recheck")
                    body = response.read(MAX_SOURCE_BYTES + 1)
                break
            except (URLError, TimeoutError):
                if attempt:
                    raise ValueError("Official source was unavailable during recheck") from None
                time.sleep(1)
        if len(body) > MAX_SOURCE_BYTES or hashlib.sha256(body).hexdigest() != pair[1]:
            raise ValueError("Official source content changed since the repair run")
        verified.add(pair)
    if verified != expected:
        raise ValueError("One or more cited official sources were not rechecked")
    return sorted(url for url, _ in verified)


def _held_out_check(project: Path, patch: str, provider: str, version: str,
                    paths: list[str]) -> dict:
    if (provider == "shopify" and version == "2026-10"
            and {"src/staff.js", "src/assignment.js"} <= set(paths)):
        script = ("import {isAssignedTo} from './src/assignment.js'; "
                  "if (isAssignedTo({}, undefined) !== false) "
                  "throw new Error('Absent staff must not match an undefined requested ID');")
    elif (provider == "stripe" and version == "2022-11-15"
          and "src/payment-intent.js" in paths):
        script = ("import {latestChargeId} from './src/payment-intent.js'; "
                  "const intent={latest_charge:null,charges:{data:[{id:'ch_legacy'}]}}; "
                  "if (latestChargeId(intent)!==undefined) "
                  "throw new Error('Removed charges field must not be used as fallback');")
    else:
        raise ValueError("No independent held-out check covers this pilot patch")
    with tempfile.TemporaryDirectory(prefix="specfaro-reverify-guard-") as temporary:
        root = Path(temporary)
        _copy_project(project, root)
        if _git_apply(root, patch)["status"] != "passed":
            raise ValueError("Could not replay patch for held-out check")
        return _run_command(["node", "--input-type=module", "-e", script], root, 30)


def reverify(project: Path, bundle: Path, repository: str, base_sha: str,
             failed_run_id: int, event: dict, failed_log: str) -> dict:
    actual_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project,
                                         text=True, timeout=15).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha) or actual_sha != base_sha:
        raise ValueError("Verifier checkout differs from the failed run base")
    validate_event(event, repository, failed_run_id, base_sha)
    paths = validate_artifact(project, bundle, repository, base_sha, failed_run_id)
    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    intake = derive_intake(project, repository, failed_run_id, failed_log)
    if (intake.provider != report["provider"] or intake.version != report["version"]
            or intake.issue != report["issue"]):
        raise ValueError("Fresh CI intake differs from the model job's case")
    sources = _check_sources(report)
    patch = (bundle / "candidate.patch").read_text(encoding="utf-8")
    replay = verify_case(project, {"status": "candidate_unverified", "diff": patch},
                         intake.commands, timeout=120,
                         case={"provider": intake.provider, "version": intake.version,
                               "issue": intake.issue})
    if (replay["overall"] != "validated_locally" or replay["manual_checks"]
            or replay["regression"]["before"]["status"] != "failed"
            or replay["regression"]["after"]["status"] != "passed"
            or any(replay["checks"][name]["status"] != "passed"
                   for name in ("build", "test"))):
        raise ValueError("Fresh regression, build, or test replay failed")
    guard = _held_out_check(project, patch, intake.provider, intake.version, paths)
    if guard["status"] != "passed":
        raise ValueError("Fresh held-out behavior check failed")
    return {"status": "independently_verified", "repository": repository,
            "failed_run_id": failed_run_id, "base_sha": base_sha,
            "provider": intake.provider, "version": intake.version,
            "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            "official_sources": sources,
            "checks": {"baseline": "failed", "regression": "passed",
                       "build": "passed", "test": "passed", "held_out": "passed"},
            "live_provider_check": "not_run"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--failed-run-id", type=int, required=True)
    parser.add_argument("--event-file", type=Path, required=True)
    parser.add_argument("--failed-log-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    event = json.loads(args.event_file.read_text(encoding="utf-8"))
    result = reverify(args.project, args.bundle, args.repository, args.base_sha,
                      args.failed_run_id, event, _read_log(args.failed_log_file))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "patch_sha256": result["patch_sha256"]}))


if __name__ == "__main__":
    main()
