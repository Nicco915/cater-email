"""End-to-end with the REAL business core (no FakeEngine).

Exercises the authoritative database reference mode against the existing parts
sample: new DB -> ready, then disabling the origin country (CHN, matched via the
alpha2 "CN" on the sample) blocks a NEW task, while the already-saved reference
snapshot still converts on a same-version retry.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cater.app import state
from cater.app.application import AppService
from cater.app.config import AllowlistConfig, PathsConfig
from cater.app.engine import CoreProcessEngine
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import make_config

REPO_ROOT = Path(__file__).resolve().parents[2]
INPUT_PARTS = REPO_ROOT / "input" / "配件" / "1x920_BEECS-6613427.xlsx"
TEMPLATE_DIR = REPO_ROOT / "templates"

pytestmark = pytest.mark.skipif(
    not INPUT_PARTS.is_file() or not (TEMPLATE_DIR / "parts_ledger.xlsx").is_file(),
    reason="real parts sample or templates not available",
)


def _relative_config(tmp_path: Path):
    # Deliberately use a RELATIVE runtime dir (resolved against CWD) to mirror
    # the production layout and catch relative-path bugs. Two independent
    # senders are allowlisted so the blocked second task (different owner) does
    # not collide with the first task's business-identity dedupe.
    config = make_config(
        tmp_path, test_addresses=("tester@example.com", "tester2@example.com")
    )
    return replace(
        config,
        paths=PathsConfig(runtime_dir=Path("runtime")),
        allowlist=AllowlistConfig(
            mode="test", test=("tester@example.com", "tester2@example.com")
        ),
    )


def test_real_core_parts_ready_then_disabled_origin_blocks_new_task(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _relative_config(tmp_path)
    service = AppService(
        config, engine=CoreProcessEngine(), gateway=SimulatorMailGateway()
    )
    try:
        # Sanity: the authoritative seed carries CHN/CN and TGO.
        assert service.references.resolve("多哥")["alpha3"] == "TGO"
        assert service.references.resolve("CN")["alpha3"] == "CHN"

        first = service.submit_offline_sources(
            sender="tester@example.com",
            destination="多哥",
            source_paths=[INPUT_PARTS],
        )
        assert first["status"] == state.READY_TO_SEND, first.get("last_error")
        issues = service.tasks.issues(str(first["id"]))
        assert not any(i["code"] == "UNKNOWN_COUNTRY" for i in issues)
        assert service.tasks.artifacts(str(first["id"])), "expected a generated ledger"

        # Disable China (matched through alpha2 'CN' on the sample).
        service.references.set_country_enabled("CHN", False, actor="test")

        # A NEW task (different owner) must now block: the authoritative
        # snapshot omits CHN.
        second = service.submit_offline_sources(
            sender="tester2@example.com",
            destination="多哥",
            source_paths=[INPUT_PARTS],
        )
        assert second["status"] == state.WAITING_INPUT
        second_issues = service.tasks.issues(str(second["id"]))
        assert any(i["code"] == "UNKNOWN_COUNTRY" for i in second_issues)

        # Same-version retry of the FIRST task reuses its saved snapshot (which
        # still contains CHN) and still converts to ready.
        retried = service.process_task(str(first["id"]), actor="test")
        assert retried["status"] == state.READY_TO_SEND
        retried_issues = service.tasks.issues(str(first["id"]))
        assert not any(i["code"] == "UNKNOWN_COUNTRY" for i in retried_issues)
    finally:
        service.close()