"""Validate a read-only OpenHands result before a separate job publishes a draft PR.

The publisher must never execute code from the candidate checkout. It reads the
artifact, checks the exact patch and evidence, and exits without changing files.
The workflow applies and publishes only after this validation succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlparse

from verify import _patch_paths


PILOT_VERSIONS = {"shopify": "2026-10", "stripe": "2022-11-15"}
OFFICIAL_HOST = {"shopify": "shopify.dev", "stripe": "docs.stripe.com"}


def _json_file(path: Path, limit: int) -> dict:
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"Missing or oversized artifact: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid artifact object: {path.name}")
    return value


def _passed(value: dict, *keys: str) -> bool:
    for key in keys:
        if not isinstance(value, dict):
            return False
        value = value.get(key)
    return isinstance(value, dict) and value.get("status") == "passed"


def validate_artifact(project: Path, bundle: Path, repository: str,
                      base_sha: str, failed_run_id: int) -> list[str]:
    report = _json_file(bundle / "report.json", 250_000)
    manifest = _json_file(bundle / "manifest.json", 5_000)
    patch_path = bundle / "candidate.patch"
    if not patch_path.is_file() or not 0 < patch_path.stat().st_size <= 20_000:
        raise ValueError("Missing or oversized candidate patch")
    patch = patch_path.read_bytes()
    try:
        patch_text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Candidate patch is not UTF-8") from exc
    if (not re.fullmatch(r"[0-9a-f]{40}", base_sha)
            or report.get("status") != "validated_locally"
            or manifest.get("status") != "validated_locally"
            or report.get("repository") != repository
            or manifest.get("repository") != repository
            or report.get("base_sha") != base_sha
            or manifest.get("base_sha") != base_sha
            or report.get("run_id") != failed_run_id
            or manifest.get("run_id") != failed_run_id
            or manifest.get("provider") != report.get("provider")
            or manifest.get("version") != report.get("version")
            or manifest.get("patch_sha256") != hashlib.sha256(patch).hexdigest()):
        raise ValueError("Artifact status, run, base revision, or checksum mismatch")
    provider = report.get("provider")
    version = report.get("version")
    if (provider not in PILOT_VERSIONS or version != PILOT_VERSIONS[provider]
            or report.get("preflight", {}).get("status") != "matched"
            or report.get("baseline", {}).get("status") != "reproduced"):
        raise ValueError("Pilot provider, version, preflight, or baseline is invalid")

    sources = report.get("sources")
    evidence = report.get("doc_evidence")
    if not isinstance(sources, list) or not sources or not isinstance(evidence, list):
        raise ValueError("Exact-version official documentation evidence is missing")
    eligible = {(item.get("url"), item.get("sha256")) for item in evidence
                if isinstance(item, dict) and item.get("eligible") is True
                and item.get("provider") == provider
                and item.get("requested_version") == version
                and item.get("version_match") is True
                and item.get("surface_match") is True}
    for source in sources:
        if (not isinstance(source, dict) or source.get("version") != version
                or urlparse(source.get("url", "")).hostname != OFFICIAL_HOST[provider]
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))
                or (source["url"], source["sha256"]) not in eligible):
            raise ValueError("Source does not match eligible official evidence")

    attempts = report.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        raise ValueError("Expected one or two recorded attempts")
    final = attempts[-1]
    verification = final.get("verification", {})
    if (verification.get("overall") != "validated_locally"
            or not _passed(verification, "patch")
            or verification.get("regression", {}).get("before", {}).get("status") != "failed"
            or not _passed(verification, "regression", "after")
            or not all(_passed(verification, "checks", check)
                       for check in ("build", "test", "semantic_guard"))
            or verification.get("manual_checks")
            or final.get("other_focus_files")):
        raise ValueError("Independent verification is incomplete")
    proposal = final.get("proposal", {})
    if proposal.get("status") != "candidate_unverified" or proposal.get("diff") != patch_text:
        raise ValueError("Patch differs from the independently checked proposal")
    paths = _patch_paths(patch_text, project)
    focus = {item.get("file") for item in report.get("focus", []) if isinstance(item, dict)}
    if (not 1 <= len(paths) <= 4 or set(paths) != set(proposal.get("files", []))
            or not set(paths) <= focus or proposal.get("remaining_matches")
            or any(PurePosixPath(path).parts[0] != "src"
                   or PurePosixPath(path).suffix not in {".js", ".jsx", ".ts", ".tsx"}
                   for path in paths)):
        raise ValueError("Patch paths exceed the focused production files")
    cited = proposal.get("source_urls")
    if not isinstance(cited, list) or not cited or not set(cited) <= {source["url"] for source in sources}:
        raise ValueError("Proposal cites unverified sources")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--failed-run-id", type=int, required=True)
    args = parser.parse_args()
    paths = validate_artifact(args.project, args.bundle, args.repository,
                              args.base_sha, args.failed_run_id)
    print("Validated files:", ", ".join(paths))


if __name__ == "__main__":
    main()
