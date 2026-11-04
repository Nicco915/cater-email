from __future__ import annotations

from pathlib import Path

import pytest

from cater.app.engine import CORE_TEMPLATE_FILES, CoreUnavailable, ensure_core_templates


def test_template_guard_fails_clearly_for_wheel_like_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATER_TEMPLATE_DIR", str(tmp_path / "missing"))
    with pytest.raises(CoreUnavailable, match="source-checkout|templates are missing"):
        ensure_core_templates()


def test_template_guard_accepts_explicit_complete_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CORE_TEMPLATE_FILES:
        (tmp_path / name).write_bytes(b"template")
    monkeypatch.setenv("CATER_TEMPLATE_DIR", str(tmp_path))
    assert ensure_core_templates() == tmp_path