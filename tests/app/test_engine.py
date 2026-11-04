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

def test_relative_runtime_processes_and_registers_outputs(tmp_path, monkeypatch, service_factory):
    from dataclasses import replace
    from cater.app.config import PathsConfig
    from cater.app import state
    from helpers import make_config
    monkeypatch.chdir(tmp_path)
    config = replace(make_config(tmp_path), paths=PathsConfig(runtime_dir=Path('runtime/test')))
    service = service_factory(config)
    source = tmp_path / 'invoice.xlsx'
    source.write_bytes(b'fake input')
    task = service.submit_offline_sources(sender='tester@example.com', destination='法国', source_paths=[source])
    assert task['status'] == state.READY_TO_SEND
    artifacts = service.tasks.artifacts(task['id'], 1)
    assert len(artifacts) == 1 and Path(artifacts[0]['path']).is_file()
    assert Path(artifacts[0]['path']).is_absolute()
