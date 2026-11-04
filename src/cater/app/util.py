"""Small shared helpers: identifiers, timestamps, hashes and safe filenames."""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.rstrip("Z")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def safe_filename(name: str, fallback: str = "attachment.bin") -> str:
    """Return a single safe path component.

    Never returns directories and never accepts separators or NUL bytes.
    Unicode letters are kept so Chinese filenames remain readable.
    """

    name = unicodedata.normalize("NFC", name or "").replace("\x00", "")
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    if name in {"", ".", ".."}:
        return fallback
    # Windows reserved names and trailing dots/spaces.
    stem = name.rstrip(" .")
    if not stem:
        return fallback
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", stem):
        stem = f"_{stem}"
    if len(stem) > 180:
        suffix = Path(stem).suffix[:20]
        stem = stem[: 180 - len(suffix)] + suffix
    return stem


def stored_filename(sha256: str, original: str) -> str:
    """Deterministic storage name that keeps the readable original name."""

    return f"{sha256[:16]}_{safe_filename(original)}"


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    return (value or "").strip().strip("<>").casefold()


def normalize_key(value: str) -> str:
    """Normalize reference aliases without fuzzy matching."""

    text = unicodedata.normalize("NFKC", value or "").strip().casefold()
    return re.sub(r"\s+", " ", text)


def normalize_filename(value: str) -> str:
    """Normalize an attachment name for same-name replacement matching."""

    text = unicodedata.normalize("NFC", value or "").strip().casefold()
    return re.sub(r"\s+", "", text)