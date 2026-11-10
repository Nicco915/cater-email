"""阶段 4 逐票目的国确认 Web 契约测试（全合成数据，无网络）。

覆盖：详情入口、每票独立选择、勾选批量同国、未选不变、当前值/空值显示、
确认人/时间/修订、过期修订/伪造方案/参考版本、跨批次任务、重复选择、
CSRF/Origin/Host、恶意值转义，以及原批次详情模板字段保持兼容。

F 服务由另一工作流实现；本文件只通过冻结公共接口驱动真实路由，不复制
第二个服务、不直接改数据库任务目的国来伪造“已确认”。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from cater.app.batch_destinations import BatchDestinationService
from cater.app.web.app import create_app
from cater.app.web.security import COOKIE_NAME, CSRF_FIELD

from batchkit import import_two_ticket

BASE_URL = "http://127.0.0.1:8765"
REASON = "客户邮件明确要求确认该国"


def _client(service) -> TestClient:
    return TestClient(create_app(service), base_url=BASE_URL)


def _csrf(client: TestClient) -> str:
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie
    session = client.app.state.session_signer.load(cookie)
    return str(session.get("csrf"))


def _service(service) -> BatchDestinationService:
    return BatchDestinationService(
        service.db, service.references, service.tasks, service.logger)


def _materialize(service) -> tuple[str, dict[str, Any], dict[str, Any]]:
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
    assert {"INV-A-001", "INV-B-001"} <= set(by_ticket)
    return batch_id, data, by_ticket


def _state(service, task_id: str) -> dict[str, Any]:
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


def _form(
    client: TestClient,
    data: dict[str, Any],
    *,
    selected: list[str],
    countries: dict[str, str] | None = None,
    bulk: str = "",
    reason: str = REASON,
    revisions: dict[str, int] | None = None,
    plan_id: str | None = None,
    reference_version: int | None = None,
) -> list[tuple[str, str]]:
    form: dict[str, Any] = {
        CSRF_FIELD: _csrf(client),
        "plan_id": plan_id if plan_id is not None else str(data["plan_id"]),
        "reference_version": str(
            reference_version if reference_version is not None
            else data["reference_version"]),
        "reason": reason,
        "selected": list(selected),
    }
    if bulk:
        form["bulk_country"] = bulk
    revisions = revisions or {}
    for task_id in selected:
        if countries is not None and task_id in countries:
            form[f"country_{task_id}"] = countries[task_id]
        form[f"revision_{task_id}"] = str(revisions.get(task_id, 0))
    return form


def _post(client: TestClient, batch_id: str, pairs: dict[str, Any]) -> Any:
    return client.post(
        f"/batches/{batch_id}/destinations",
        data=pairs,
        follow_redirects=False,
    )


def _post_with_headers(client: TestClient, batch_id: str,
                       pairs: dict[str, Any], *,
                       headers: dict[str, str]) -> Any:
    return client.post(
        f"/batches/{batch_id}/destinations",
        data=pairs,
        headers=headers,
        follow_redirects=False,
    )


def _rows(service, batch_id: str) -> dict[str, dict[str, Any]]:
    data = _service(service).get_batch_destinations(batch_id)
    return {str(row["task_id"]): row for row in data["rows"]}


def test_detail_entry_and_page_show_explicit_empty_values(service):
    batch_id, data, by_ticket = _materialize(service)
    assert data["rows"]
    with _client(service) as client:
        detail = client.get(f"/batches/{batch_id}")
        assert detail.status_code == 200
        assert "确认目的国（逐票）" in detail.text
        assert f"/batches/{batch_id}/destinations" in detail.text
        # 原详情页布局/字段保持兼容。
        assert "关联任务" in detail.text
        assert "批次材料" in detail.text
        assert "批次事件" in detail.text

        page = client.get(f"/batches/{batch_id}/destinations")
        assert page.status_code == 200
        assert "INV-A-001" in page.text
        assert "INV-B-001" in page.text
        assert "未设置（空值）" in page.text
        assert "尚无目的国确认记录（修订 0）" in page.text
        assert "法国" in page.text and "FRA" in page.text
        assert "批量同国" in page.text
        # 不预选国家、不默认选全部票。
        assert "checked" not in page.text.lower()
        assert '<option value="FRA" selected' not in page.text
        assert "批次默认" in page.text  # 明确说明不存在批次默认值


def test_single_ticket_then_other_ticket_different_countries(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    b = by_ticket["INV-B-001"]
    before_b = _state(service, b["task_id"])
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        response = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: "FRA"},
                revisions={a["task_id"]: a["revision"]},
            ),
        )
        assert response.status_code == 303
        assert "error=" not in response.headers["location"]
        assert "message=" in response.headers["location"]
        # 未勾选票完全不变（包括确认事件数与目的国）。
        assert _state(service, b["task_id"]) == before_b
        rows = _rows(service, batch_id)
        assert rows[a["task_id"]]["destination_alpha3"] == "FRA"
        assert rows[a["task_id"]]["destination_name"] == "法国"
        assert rows[a["task_id"]]["revision"] > 0
        assert rows[a["task_id"]]["confirmed_by"] == "web-admin"
        assert rows[a["task_id"]]["confirmed_at"]
        assert rows[b["task_id"]]["destination_alpha3"] is None
        assert rows[b["task_id"]]["revision"] == 0
        after_a = _state(service, a["task_id"])
        assert after_a["status"] == "RECEIVED"
        assert after_a["version"] == 1
        assert after_a["hold"] == 1
        assert after_a["outbox"] == 0
        assert after_a["versions"] == 1

        page = client.get(f"/batches/{batch_id}/destinations")
        assert "法国" in page.text and "FRA" in page.text
        assert "web-admin" in page.text
        assert "未设置（空值）" in page.text  # B 仍为空

        # 第二票选不同国家，只改 B；A 保持法国。
        response = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[b["task_id"]],
                countries={b["task_id"]: "DEU"},
                revisions={b["task_id"]: b["revision"]},
            ),
        )
        assert response.status_code == 303
        assert "error=" not in response.headers["location"]
    rows = _rows(service, batch_id)
    assert rows[a["task_id"]]["destination_alpha3"] == "FRA"
    assert rows[b["task_id"]]["destination_alpha3"] == "DEU"
    assert rows[b["task_id"]]["destination_name"] == "德国"
    assert rows[a["task_id"]]["revision"] != rows[b["task_id"]]["revision"]


def test_bulk_same_country_only_for_selected_tickets(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    b = by_ticket["INV-B-001"]
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        response = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"], b["task_id"]],
                bulk="FRA",
                revisions={a["task_id"]: a["revision"],
                           b["task_id"]: b["revision"]},
            ),
        )
        assert response.status_code == 303
        assert "error=" not in response.headers["location"]
    rows = _rows(service, batch_id)
    assert rows[a["task_id"]]["destination_alpha3"] == "FRA"
    assert rows[b["task_id"]]["destination_alpha3"] == "FRA"
    assert rows[a["task_id"]]["confirmed_by"] == "web-admin"
    assert rows[b["task_id"]]["confirmed_by"] == "web-admin"


def test_bulk_and_per_ticket_values_conflict_rejected(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    before = _state(service, a["task_id"])
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        response = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: "DEU"},
                bulk="FRA",
                revisions={a["task_id"]: a["revision"]},
            ),
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        page = client.get(response.headers["location"])
        assert "同时提交" in page.text
    assert _state(service, a["task_id"]) == before
    assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] is None


def test_empty_selection_or_missing_country_writes_nothing(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    before = _state(service, a["task_id"])
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        empty = _post(client, batch_id, _form(client, data, selected=[]))
        assert empty.status_code == 303
        assert "error=" in empty.headers["location"]
        missing = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                revisions={a["task_id"]: a["revision"]},
            ),
        )
        assert missing.status_code == 303
        assert "error=" in missing.headers["location"]
    assert _state(service, a["task_id"]) == before


def test_stale_revision_forged_plan_and_reference_rejected(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        first = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: "FRA"},
                revisions={a["task_id"]: a["revision"]},
            ),
        )
        assert first.status_code == 303
        assert "error=" not in first.headers["location"]
        after_first = _state(service, a["task_id"])
        current = _rows(service, batch_id)[a["task_id"]]
        assert current["revision"] > a["revision"]

        stale = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: "DEU"},
                revisions={a["task_id"]: a["revision"]},
            ),
        )
        assert stale.status_code == 303
        assert "error=" in stale.headers["location"]
        assert _state(service, a["task_id"]) == after_first
        assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] == "FRA"

        forged_plan = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: "DEU"},
                revisions={a["task_id"]: current["revision"]},
                plan_id="plan-forged",
            ),
        )
        assert forged_plan.status_code == 303
        assert "error=" in forged_plan.headers["location"]
        assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] == "FRA"

        forged_reference = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: "DEU"},
                revisions={a["task_id"]: current["revision"]},
                reference_version=int(data["reference_version"]) + 999,
            ),
        )
        assert forged_reference.status_code == 303
        assert "error=" in forged_reference.headers["location"]
        assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] == "FRA"
    assert _state(service, a["task_id"]) == after_first


def test_cross_batch_task_rejected_without_leaking_other_rows(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    other_batch_id, other_data, other_by_ticket = _materialize(service)
    foreign = other_by_ticket["INV-A-001"]
    before_foreign = _state(service, foreign["task_id"])
    before_local = _state(service, a["task_id"])
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        response = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[foreign["task_id"]],
                countries={foreign["task_id"]: "FRA"},
                revisions={foreign["task_id"]: foreign["revision"]},
            ),
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert other_batch_id not in response.headers["location"]
        page = client.get(response.headers["location"])
        assert "不属于该批次" in page.text
        # 错误信息不暴露另一批次的任务明细或目的国。
        assert "INV-B-001" not in response.headers["location"]
    assert _state(service, foreign["task_id"]) == before_foreign
    assert _state(service, a["task_id"]) == before_local
    assert _rows(service, other_batch_id)[foreign["task_id"]]["destination_alpha3"] is None
    assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] is None


def test_duplicate_selected_and_duplicate_revision_rejected(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    before = _state(service, a["task_id"])
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        base = _form(
            client,
            data,
            selected=[a["task_id"]],
            countries={a["task_id"]: "FRA"},
            revisions={a["task_id"]: a["revision"]},
        )
        duplicated = {
            **base,
            "selected": [a["task_id"], a["task_id"]],
        }
        response = _post(client, batch_id, duplicated)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        page = client.get(response.headers["location"])
        assert "重复" in page.text

        duplicated_revision = {
            **base,
            f"revision_{a['task_id']}": [
                str(a["revision"]), str(a["revision"])],
        }
        response = _post(client, batch_id, duplicated_revision)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
    assert _state(service, a["task_id"]) == before


def test_unknown_country_error_is_escaped(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    before = _state(service, a["task_id"])
    payload = "<script>alert('country')</script>"
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        response = _post(
            client,
            batch_id,
            _form(
                client,
                data,
                selected=[a["task_id"]],
                countries={a["task_id"]: payload},
                revisions={a["task_id"]: a["revision"]},
            ),
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        page = client.get(response.headers["location"])
        assert "<script>alert('country')</script>" not in page.text
        assert "&lt;script&gt;" in page.text
    assert _state(service, a["task_id"]) == before


def test_csrf_origin_and_host_protection(service):
    batch_id, data, by_ticket = _materialize(service)
    a = by_ticket["INV-A-001"]
    before = _state(service, a["task_id"])
    with _client(service) as client:
        client.get(f"/batches/{batch_id}/destinations")
        pairs = _form(
            client,
            data,
            selected=[a["task_id"]],
            countries={a["task_id"]: "FRA"},
            revisions={a["task_id"]: a["revision"]},
        )
        no_csrf = {key: value for key, value in pairs.items() if key != CSRF_FIELD}
        denied = _post(client, batch_id, no_csrf)
        assert denied.status_code == 403

        hostile_origin = _post_with_headers(
            client, batch_id, pairs,
            headers={"origin": "http://evil.example"})
        assert hostile_origin.status_code == 403
        null_origin = _post_with_headers(
            client, batch_id, pairs, headers={"origin": "null"})
        assert null_origin.status_code == 403

        allowed = _post_with_headers(
            client, batch_id, pairs, headers={"origin": BASE_URL})
        assert allowed.status_code == 303
        assert "error=" not in allowed.headers["location"]

    bad_host = TestClient(create_app(service), base_url="http://evil.example")
    with bad_host:
        host_denied = bad_host.get(f"/batches/{batch_id}/destinations")
        assert host_denied.status_code == 400
        assert "Invalid Host" in host_denied.text
    assert _state(service, a["task_id"])["status"] == "RECEIVED"
    assert _rows(service, batch_id)[a["task_id"]]["destination_alpha3"] == "FRA"
