"""阶段 4 逐票目的国确认 CLI 契约测试（纯离线、全合成数据）。

覆盖：只读展示、显式确认（可重复 --task-id）、逐票结果输出、重复参数
拒绝、跨批次/未知国家/空理由失败且不写入、不调用 AppService/load_config、
不读取 .env、不自动执行核心；离线报告记录当前票目的国确认来源，改国后
新运行产生新报告而旧报告保持原样。

F 服务由另一工作流实现；本文件只通过冻结公共接口驱动真实 CLI，不复制
第二个服务、不直接改数据库任务目的国来伪造“已确认”。
"""
from __future__ import annotations

import json
from pathlib import Path

from cater.app.batch_destinations import BatchDestinationService
from cater.app.batch_offline import REPORT_NAME, BatchOfflineRunner
from cater.app.cli import main as cli_main

from batchkit import import_two_ticket
from helpers import FakeEngine


def _service(service) -> BatchDestinationService:
    return BatchDestinationService(
        service.db, service.references, service.tasks, service.logger)


def _materialize(service) -> tuple[str, dict, dict[str, dict]]:
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    assert len(keys) == 2
    service.batches.confirm_and_create(
        batch_id,
        plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys,
        exclusions={},
        actor="tester",
    )
    data = _service(service).get_batch_destinations(batch_id)
    by_ticket = {str(row["ticket_no"]): row for row in data["rows"]}
    return batch_id, data, by_ticket


def _rows(service, batch_id: str) -> dict[str, dict]:
    data = _service(service).get_batch_destinations(batch_id)
    return {str(row["task_id"]): row for row in data["rows"]}


def _state(service, task_id: str) -> dict:
    task = service.tasks.require(task_id)
    link = service.db.fetchone(
        "SELECT execution_hold, hold_reason FROM batch_tasks WHERE task_id = ?",
        (task_id,))
    return {
        "status": task["status"],
        "version": task["version"],
        "destination_name": task["destination_name"],
        "destination_alpha3": task["destination_alpha3"],
        "hold": int(link["execution_hold"]),
        "hold_reason": link["hold_reason"],
        "outbox": int(service.db.scalar(
            "SELECT COUNT(*) FROM outbox WHERE task_id = ?", (task_id,)) or 0),
        "events": int(service.db.scalar(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)) or 0),
        "versions": int(service.db.scalar(
            "SELECT COUNT(*) FROM task_versions WHERE task_id = ?", (task_id,)) or 0),
    }


def _cli(*args: str) -> int:
    return cli_main(list(args))


def test_read_only_listing_and_json(service, config, capsys):
    batch_id, data, by_ticket = _materialize(service)
    code = _cli(
        "batch-destinations", batch_id,
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "INV-A-001" in out
    assert "INV-B-001" in out
    assert "未设置" in out
    assert "本命令只读" in out

    code = _cli(
        "batch-destinations", batch_id,
        "--runtime", str(config.paths.runtime_dir),
        "--json",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["batch_id"] == batch_id
    assert payload["plan_id"] == data["plan_id"]
    assert len(payload["rows"]) == 2
    assert all(row["destination_alpha3"] is None for row in payload["rows"])
    assert all(row["revision"] == 0 for row in payload["rows"])

    missing = _cli(
        "batch-destinations", "batch-does-not-exist",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert missing == 1
    assert "批次不存在" in capsys.readouterr().err


def test_confirm_outputs_each_ticket_and_preserves_state(
        service, config, capsys, monkeypatch):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    b = by_ticket["INV-B-001"]
    before_a = _state(service, a["task_id"])
    before_b = _state(service, b["task_id"])

    # 证明不自动执行核心：若 CLI 误调用 run_task 则立即失败。
    from cater.app.batch_offline import BatchOfflineRunner as RunnerClass

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("confirm-batch-destinations 不得执行核心")

    monkeypatch.setattr(RunnerClass, "run_task", _must_not_run)

    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--task-id", b["task_id"],
        "--country", "FRA",
        "--reason", "客户邮件明确确认发往法国",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert a["task_id"] in out and b["task_id"] in out
    assert "法国" in out and "FRA" in out
    assert "任务状态/版本/execution_hold 未改变" in out
    assert "未自动执行离线核心" in out

    rows = _rows(service, batch_id)
    assert rows[a["task_id"]]["destination_alpha3"] == "FRA"
    assert rows[b["task_id"]]["destination_alpha3"] == "FRA"
    assert rows[a["task_id"]]["confirmed_by"] == "cli"
    assert rows[b["task_id"]]["confirmed_by"] == "cli"
    assert rows[a["task_id"]]["revision"] > 0
    assert rows[b["task_id"]]["revision"] > 0
    after_a = _state(service, a["task_id"])
    after_b = _state(service, b["task_id"])
    for before, after in ((before_a, after_a), (before_b, after_b)):
        assert after["status"] == before["status"] == "RECEIVED"
        assert after["version"] == before["version"]
        assert after["hold"] == before["hold"] == 1
        assert after["outbox"] == before["outbox"] == 0
        assert after["versions"] == before["versions"]


def test_json_confirm_output(service, config, capsys):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--country", "DEU",
        "--reason", "客户要求德国",
        "--runtime", str(config.paths.runtime_dir),
        "--json",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["batch_id"] == batch_id
    assert payload["plan_id"] == data["plan_id"]
    assert len(payload["confirmed"]) == 1
    confirmed = payload["confirmed"][0]
    assert confirmed["task_id"] == a["task_id"]
    assert confirmed["destination_alpha3"] == "DEU"
    assert confirmed["destination_name"] == "德国"
    assert confirmed["revision"] > 0
    assert confirmed["confirmed_by"] == "cli"


def test_cli_uses_offline_config_and_never_appservice(
        service, config, capsys, monkeypatch, tmp_path):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    (tmp_path / ".env").write_text(
        "CATER_MAIL_ACCOUNT=prod@example.com\n"
        "CATER_IMAP_PASSWORD=supersecret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CATER_MAIL_ACCOUNT", "prod@example.com")
    monkeypatch.setenv("CATER_IMAP_PASSWORD", "supersecret")
    monkeypatch.setenv("CATER_RUNTIME_DIR", str(tmp_path / "env-runtime"))

    import cater.app.cli as cli_module

    def _must_not_load(*_args, **_kwargs):
        raise AssertionError("CLI 不得调用 AppService/load_config")

    monkeypatch.setattr(cli_module, "AppService", _must_not_load)
    monkeypatch.setattr(cli_module, "load_config", _must_not_load)

    import socket

    def _no_network(*_args, **_kwargs):
        raise AssertionError("目的国 CLI 不得访问网络")

    monkeypatch.setattr(socket, "create_connection", _no_network)

    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--country", "FRA",
        "--reason", "离线测试",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "supersecret" not in captured.out + captured.err
    assert "prod@example.com" not in captured.out + captured.err
    assert not (tmp_path / "env-runtime").exists()
    assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] == "FRA"


def test_duplicate_task_id_is_rejected_not_deduped(
        service, config, capsys):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    before = _state(service, a["task_id"])
    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--task-id", a["task_id"],
        "--country", "FRA",
        "--reason", "重复参数测试",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "重复" in err
    assert "不会自动去重" in err
    assert _state(service, a["task_id"]) == before


def test_cross_batch_unknown_country_and_bad_reason_fail_without_writes(
        service, config, capsys):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    other_batch_id, other_data, other_by_ticket = _materialize(service)
    foreign = other_by_ticket["INV-A-001"]
    before_a = _state(service, a["task_id"])
    before_foreign = _state(service, foreign["task_id"])

    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", foreign["task_id"],
        "--country", "FRA",
        "--reason", "跨批次测试",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 1
    assert "不属于该批次" in capsys.readouterr().err
    assert _state(service, a["task_id"]) == before_a
    assert _state(service, foreign["task_id"]) == before_foreign

    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--country", "ZZZ_NOT_A_COUNTRY",
        "--reason", "未知国家测试",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 1
    assert "不可用" in capsys.readouterr().err
    assert _state(service, a["task_id"]) == before_a

    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--country", "FRA",
        "--reason", "   ",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 1
    assert "reason" in capsys.readouterr().err
    assert _state(service, a["task_id"]) == before_a
    assert _rows(service, other_batch_id)[foreign["task_id"]]["destination_alpha3"] is None


def _run_offline(config, task_id: str, output_dir: Path,
                 engine: FakeEngine | None = None) -> dict:
    instance = BatchOfflineRunner(
        config, process_job=(engine or FakeEngine()).process_job)
    try:
        return instance.run_task(task_id, output_dir, actor="tester")
    finally:
        instance.close()


def test_offline_report_records_destination_source_and_missing_country(
        service, config, tmp_path):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    unset = by_ticket["INV-B-001"]
    code = _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"],
        "--country", "FRA",
        "--reason", "客户确认法国",
        "--runtime", str(config.paths.runtime_dir),
    )
    assert code == 0
    confirmed_row = _rows(service, batch_id)[a["task_id"]]

    out_dir = tmp_path / "offline-a"
    report = _run_offline(config, a["task_id"], out_dir)
    assert report["status"] == "ready"
    assert report["destination"] == {
        "name": "法国",
        "alpha3": "FRA",
        "revision": confirmed_row["revision"],
        "confirmed_by": "cli",
        "confirmed_at": confirmed_row["confirmed_at"],
    }
    persisted = json.loads((out_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert persisted["destination"] == report["destination"]
    assert persisted["snapshot"]["trusted_hash"] == report["snapshot"]["trusted_hash"]
    assert persisted["state_unchanged"] is True

    # 缺目的国：报告如实为空来源，核心按既有规则 waiting_input。
    waiting = _run_offline(
        config, unset["task_id"], tmp_path / "offline-unset",
        FakeEngine(mode="waiting"))
    assert waiting["status"] == "waiting_input"
    assert waiting["destination"] == {
        "name": None,
        "alpha3": None,
        "revision": 0,
        "confirmed_by": None,
        "confirmed_at": None,
    }


def test_destination_change_writes_new_report_without_rewriting_old(
        service, config, tmp_path):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    assert _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"], "--country", "FRA",
        "--reason", "客户先要求法国",
        "--runtime", str(config.paths.runtime_dir),
    ) == 0
    old_dir = tmp_path / "offline-old"
    old_report = _run_offline(config, a["task_id"], old_dir)
    old_bytes = (old_dir / REPORT_NAME).read_bytes()
    assert old_report["destination"]["alpha3"] == "FRA"

    assert _cli(
        "confirm-batch-destinations", batch_id,
        "--task-id", a["task_id"], "--country", "DEU",
        "--reason", "客户改为德国",
        "--runtime", str(config.paths.runtime_dir),
    ) == 0
    new_row = _rows(service, batch_id)[a["task_id"]]
    assert new_row["revision"] > old_report["destination"]["revision"]
    new_dir = tmp_path / "offline-new"
    new_report = _run_offline(config, a["task_id"], new_dir)
    assert new_report["destination"]["alpha3"] == "DEU"
    assert new_report["destination"]["name"] == "德国"
    assert new_report["destination"]["revision"] == new_row["revision"]
    assert new_report["destination"]["confirmed_by"] == "cli"
    # 旧报告原样保留（同一任务/快照也不再重写旧目录）。
    assert (old_dir / REPORT_NAME).read_bytes() == old_bytes
    old_json = json.loads(old_bytes)
    assert old_json["destination"]["alpha3"] == "FRA"
    assert old_json["destination"]["revision"] == old_report["destination"]["revision"]
    assert _state(service, a["task_id"])["hold"] == 1
