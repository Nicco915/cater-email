"""Validate the untrusted-by-contract core response before persistence/delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ValidationError
from .util import is_within, safe_filename, sha256_file

VALID_STATUSES = {"ready", "waiting_input", "unsupported"}
# The core reports "unsupported" as the kind when it cannot classify a mail or
# workbook. This is a valid terminal-ish response: the app turns it into a
# WAITING_INPUT task with an explanation, never a retry loop.
VALID_KINDS = {"parts", "machine", "auto", "unsupported"}


def validate_core_response(response: Any, *, output_dir: Path) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValidationError("core process_job must return a dict")
    status = response.get("status")
    if status not in VALID_STATUSES:
        raise ValidationError(f"Invalid core status: {status!r}")
    kind = response.get("kind")
    if kind not in VALID_KINDS:
        raise ValidationError(f"Invalid core kind: {kind!r}")
    records = response.get("records") or []
    if not isinstance(records, list):
        raise ValidationError("core records must be a list")
    issues = response.get("issues") or []
    if not isinstance(issues, list):
        raise ValidationError("core issues must be a list")
    provenance = response.get("provenance") or []
    if not isinstance(provenance, list):
        raise ValidationError("core provenance must be a list")
    artifacts_raw = response.get("artifacts") or []
    if not isinstance(artifacts_raw, list):
        raise ValidationError("core artifacts must be a list")

    issues_normalized: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValidationError("each core issue must be an object")
        issues_normalized.append(
            {
                "code": str(issue.get("code") or "CORE_ISSUE"),
                "record_key": issue.get("record_key"),
                "field": issue.get("field"),
                "message": str(issue.get("message") or ""),
                "sources": issue.get("sources") or [],
                "required_action": issue.get("required_action"),
                "blocking": bool(issue.get("blocking", True)),
            }
        )

    blocking = any(issue["blocking"] for issue in issues_normalized)
    if status == "ready" and blocking:
        raise ValidationError("core returned ready with blocking issues")
    if status == "ready" and not records:
        raise ValidationError("core returned ready with zero records")
    if status == "waiting_input" and not issues_normalized:
        raise ValidationError("core returned waiting_input without issues")
    if status == "ready" and not response.get("content_hash"):
        raise ValidationError("core returned ready without content_hash")

    artifacts: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for raw in artifacts_raw:
        if not isinstance(raw, dict) or not raw.get("path"):
            raise ValidationError("each artifact must have a path")
        path = Path(str(raw["path"]))
        if not path.is_absolute():
            path = output_dir / path
        path = path.resolve()
        if not is_within(path, output_dir):
            raise ValidationError("core artifact path escapes the task output directory")
        if not path.is_file():
            raise ValidationError(f"core artifact is missing: {path.name}")
        if path in seen_paths:
            raise ValidationError(f"duplicate core artifact: {path.name}")
        seen_paths.add(path)
        expected = str(raw.get("sha256") or "")
        actual = sha256_file(path)
        if not expected or expected != actual:
            raise ValidationError(f"core artifact hash mismatch: {path.name}")
        filename = safe_filename(str(raw.get("filename") or path.name))
        artifacts.append(
            {
                "path": str(path),
                "filename": filename,
                "sha256": actual,
                "size_bytes": path.stat().st_size,
                "kind": raw.get("kind"),
            }
        )
    if status == "ready" and not artifacts:
        raise ValidationError("core returned ready without output artifacts")

    return {
        "status": status,
        "kind": kind,
        "records": records,
        "issues": issues_normalized,
        "artifacts": artifacts,
        "provenance": [item for item in provenance if isinstance(item, dict)],
        "business_identity": response.get("business_identity"),
        "content_hash": response.get("content_hash"),
    }