"""多票批次 Web 测试：页面渲染、上传限制、CSRF、XSS 转义、调整/确认闭环。

全部经 TestClient 走真实路由；无任何网络访问。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from cater.app.web.app import create_app
from cater.app.web.security import COOKIE_NAME, CSRF_FIELD

from batchkit import import_two_ticket, two_ticket_mail

BASE_URL = "http://127.0.0.1:8765"


def _client(service) -> TestClient:
    return TestClient(create_app(service), base_url=BASE_URL)


def _csrf(client: TestClient) -> str:
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie
    signer = client.app.state.session_signer
    session = signer.load(cookie)
    return str(session.get("csrf"))


def _upload(client, raw: bytes, filename: str = "batch.eml"):
    return client.post(
        "/batches/import",
        data={CSRF_FIELD: _csrf(client)},
        files={"file": (filename, raw, "message/rfc822")},
        follow_redirects=False,
    )


def test_batch_list_and_detail_render(service):
    batch_id, _ = import_two_ticket(service)
    with _client(service) as client:
        response = client.get("/batches")
        assert response.status_code == 200
        assert "两票整机发票" in response.text
        assert batch_id in response.text
        detail = client.get(f"/batches/{batch_id}")
        assert detail.status_code == 200
        assert "INV-A-001" in detail.text
        assert "INV-B-001" in detail.text
        assert "候选票" in detail.text
        assert "确认方案并创建逐票任务" in detail.text
        assert "批次材料" in detail.text
        assert "批次事件" in detail.text
        missing = client.get("/batches/batch-nope")
        assert missing.status_code == 404


def test_upload_eml_via_web_and_dedupe_message(service):
    raw = two_ticket_mail()
    with _client(service) as client:
        client.get("/batches")  # 建立会话
        first = _upload(client, raw)
        assert first.status_code == 303
        assert "/batches/batch_" in first.headers["location"]
        # 重复上传 -> 返回既有批次（路径相同，仅提示语不同）
        second = _upload(client, raw)
        assert second.status_code == 303
        assert (second.headers["location"].split("?")[0]
                == first.headers["location"].split("?")[0])
        assert len(service.batches.list_batches()) == 1


def test_upload_rejects_non_eml_and_requires_csrf(service):
    raw = two_ticket_mail()
    with _client(service) as client:
        client.get("/batches")
        no_csrf = client.post(
            "/batches/import",
            files={"file": ("batch.eml", raw, "message/rfc822")},
            follow_redirects=False,
        )
        assert no_csrf.status_code == 403
        wrong_ext = _upload(client, raw, filename="batch.txt")
        assert wrong_ext.status_code == 303
        assert "error=" in wrong_ext.headers["location"]
        assert len(service.batches.list_batches()) == 0


def test_upload_size_limit(service):
    with _client(service) as client:
        client.get("/batches")
        object.__setattr__(service.config.mail, "max_message_bytes", 128)
        big = two_ticket_mail()
        assert len(big) > 128
        response = _upload(client, big)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert len(service.batches.list_batches()) == 0


def test_subject_xss_escaped(service):
    raw = two_ticket_mail(subject="<script>alert('x')</script>")
    batch = service.batches.import_eml_bytes(
        raw, filename="x.eml", actor="t")["batch"]
    with _client(service) as client:
        listing = client.get("/batches")
        assert "<script>alert" not in listing.text
        assert "&lt;script&gt;" in listing.text
        detail = client.get(f"/batches/{batch['id']}")
        assert "<script>alert" not in detail.text
        assert "&lt;script&gt;" in detail.text


def test_adjust_via_web_creates_new_plan_version(service):
    batch_id, result = import_two_ticket(
        service, extra_rows=[("MSOXX", "S009999", 3000)])
    plan = result["plan"]
    row = result["preview"]["unmatched_mail_rows"][0]
    with _client(service) as client:
        client.get(f"/batches/{batch_id}")
        response = client.post(
            f"/batches/{batch_id}/adjust",
            data={
                CSRF_FIELD: _csrf(client),
                "base_plan_id": plan["id"],
                "op_type": "mail_row",
                "table_index": str(row["table_index"]),
                "row": str(row["row"]),
                "action": "exclude",
                "reason": "非本批业务",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    detail = service.batches.batch_detail(batch_id)
    assert detail["plan"]["version"] == 2
    assert detail["preview"]["unmatched_mail_rows"] == []
    # 无 CSRF 的调整被拒
    with _client(service) as client:
        client.get(f"/batches/{batch_id}")
        denied = client.post(
            f"/batches/{batch_id}/adjust",
            data={"op_type": "mail_row", "action": "exclude"},
            follow_redirects=False,
        )
        assert denied.status_code == 403


def test_confirm_via_web_creates_held_tasks(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    with _client(service) as client:
        client.get(f"/batches/{batch_id}")
        response = client.post(
            f"/batches/{batch_id}/confirm",
            data={
                CSRF_FIELD: _csrf(client),
                "plan_id": plan["id"],
                "fingerprint": plan["source_fingerprint"],
                "selected": keys,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    batch = service.batches.require_batch(batch_id)
    assert batch["status"] == "materialized"
    assert len(service.batches.batch_tasks(batch_id)) == 2
    # 详情页显示挂起任务
    with _client(service) as client:
        detail = client.get(f"/batches/{batch_id}")
        assert detail.status_code == 200
        assert "挂起" in detail.text or "尚未开放执行" in detail.text
        assert "确认方案并创建逐票任务" not in detail.text  # 已物化不再显示表单


def test_confirm_via_web_stale_fingerprint_flashes_error(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    with _client(service) as client:
        client.get(f"/batches/{batch_id}")
        response = client.post(
            f"/batches/{batch_id}/confirm",
            data={
                CSRF_FIELD: _csrf(client),
                "plan_id": plan["id"],
                "fingerprint": "f" * 64,
                "selected": keys,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
    assert service.batches.require_batch(batch_id)["status"] == "open"
    assert service.batches.batch_tasks(batch_id) == []


def test_asset_download_and_cross_batch_isolation(service):
    batch_id, _ = import_two_ticket(service)
    other_id, _ = import_two_ticket(service, subject="另一批次")
    asset = next(a for a in service.batches.assets(batch_id)
                 if a["role"] == "attachment")
    with _client(service) as client:
        ok = client.get(f"/batches/{batch_id}/assets/{asset['id']}")
        assert ok.status_code == 200
        import hashlib
        assert hashlib.sha256(ok.content).hexdigest() == asset["sha256"]
        # 跨批次访问同一资产 ID -> 404
        cross = client.get(f"/batches/{other_id}/assets/{asset['id']}")
        assert cross.status_code == 404
        missing = client.get(f"/batches/{batch_id}/assets/asset-nope")
        assert missing.status_code == 404
