from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cater.app import state
from cater.app.web.app import create_app
from cater.app.web.security import COOKIE_NAME, CSRF_FIELD

BASE_URL = "http://127.0.0.1:8765"


def _client(service) -> TestClient:
    return TestClient(create_app(service), base_url=BASE_URL)


def _csrf(client: TestClient) -> str:
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie
    signer = client.app.state.session_signer
    session = signer.load(cookie)
    token = session.get("csrf")
    assert token
    return str(token)


def _submit(service, source: Path) -> dict:
    return service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[source]
    )


def test_task_list_shows_lifecycle_tabs(service) -> None:
    with _client(service) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "归档" in response.text
    assert "回收站" in response.text


def test_batch_delete_requires_csrf_and_works(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    with _client(service) as client:
        client.get("/")
        # Without CSRF: rejected.
        denied = client.post(
            "/tasks/batch",
            data={"action": "delete", "task_ids": task["id"]},
            follow_redirects=False,
        )
        assert denied.status_code == 403
        token = _csrf(client)
        accepted = client.post(
            "/tasks/batch",
            data={CSRF_FIELD: token, "action": "delete", "task_ids": task["id"]},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
    assert service.tasks.is_trashed(task["id"])


def test_cross_origin_batch_post_rejected(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        response = client.post(
            "/tasks/batch",
            headers={"origin": "http://evil.example"},
            data={CSRF_FIELD: token, "action": "delete", "task_ids": task["id"]},
            follow_redirects=False,
        )
        assert response.status_code == 403
    assert not service.tasks.is_trashed(task["id"])


def test_batch_disallowed_lists_reasons(service, offline_source: Path) -> None:
    blocked = _submit(service, offline_source)
    service.tasks.transition(blocked["id"], state.PROCESSING, stage="test", actor="test")
    ok = _submit(service, offline_source)  # not deduped against a PROCESSING task
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        response = client.post(
            "/tasks/batch",
            data={
                CSRF_FIELD: token,
                "action": "delete",
                "task_ids": [ok["id"], blocked["id"]],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        # Follow to the redirected page to read the flash error listing items.
        page = client.get(response.headers["location"])
        assert "批量操作已撤销" in page.text
        assert blocked["id"] in page.text
    assert not service.tasks.is_trashed(ok["id"])


def test_task_detail_has_archive_and_delete_buttons(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    with _client(service) as client:
        response = client.get(f"/tasks/{task['id']}")
    assert response.status_code == 200
    assert "归档（仅隐藏）" in response.text
    assert "删除到回收站" in response.text


def test_purge_flow_requires_confirmation(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    service.delete_task(task["id"], actor="test")
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        preview = client.get(f"/tasks/{task['id']}/purge")
        assert preview.status_code == 200
        # Without the confirm checkbox: refused, files kept.
        refused = client.post(
            f"/tasks/{task['id']}/purge",
            data={CSRF_FIELD: token},
            follow_redirects=False,
        )
        assert refused.status_code == 303
        confirmed = client.post(
            f"/tasks/{task['id']}/purge",
            data={CSRF_FIELD: token, "confirm": "1"},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303


def test_references_page_shows_crud_and_authoritative_note(service) -> None:
    with _client(service) as client:
        response = client.get("/references")
    assert response.status_code == 200
    assert "新增国家" in response.text
    assert "权威" in response.text
    assert "预览" in response.text


def test_clear_test_blocked_for_production_identity(
    service_factory, tmp_path: Path
) -> None:
    from helpers import make_config

    prod_config = make_config(
        tmp_path, mode="production", production_addresses=("op@example.com",)
    )
    service = service_factory(prod_config)
    with _client(service) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert "未可靠判定为测试身份" in response.text


def test_country_create_via_web_requires_csrf(service) -> None:
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        response = client.post(
            "/references/countries",
            data={CSRF_FIELD: token, "alpha3": "zwe", "name_zh": "津巴布韦", "name_en": "Zimbabwe"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert service.references.get_country("ZWE") is not None


def test_default_scope_machine_toggle_via_web(service) -> None:
    service.references.create_machine("SEM510", "", declaration="d", actor="test")
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        # default scope renders as "default" in the URL path.
        response = client.post(
            "/references/machines/sem510/default/enabled",
            data={CSRF_FIELD: token, "enabled": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert service.references.get_machine("SEM510", "")["enabled"] is False


def test_import_preview_page_without_token_renders(service) -> None:
    with _client(service) as client:
        response = client.get("/references/machines/preview")
    assert response.status_code == 200
    assert "没有待确认的导入预览" in response.text


def test_import_confirm_requires_csrf(service) -> None:
    with _client(service) as client:
        client.get("/")
        denied = client.post(
            "/references/machines/confirm",
            data={"token": "anything"},
            follow_redirects=False,
        )
        assert denied.status_code == 403


def test_country_alias_enable_disable_via_web(service) -> None:
    alias = [a for a in service.references.country_aliases() if a["alias_raw"] == "France"][0]
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        response = client.post(
            f"/references/aliases/{alias['alias_norm']}/enabled",
            data={CSRF_FIELD: token, "enabled": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    rows = {a["alias_norm"]: a for a in service.references.country_aliases()}
    assert rows[alias["alias_norm"]]["enabled"] == 0


def test_machine_alias_add_via_web(service) -> None:
    service.references.create_machine("SEM510", "", declaration="d", actor="test")
    with _client(service) as client:
        client.get("/")
        token = _csrf(client)
        response = client.post(
            "/references/machine-aliases",
            data={
                CSRF_FIELD: token,
                "model": "SEM510",
                "alias": "SEM-510",
                "destination_scope": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert any(
        a["alias_norm"] == "sem-510" for a in service.references.list_machine_aliases()
    )


def _forms(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def _assert_no_nested_forms(soup) -> None:
    for form in soup.find_all("form"):
        assert form.find_parent("form") is None, (
            "nested <form> detected; browsers drop inner forms: "
            f"{form.get('action')}"
        )


def test_references_html_has_no_nested_forms_and_correct_form_owners(service) -> None:
    service.references.create_machine("SEM510", "", declaration="d", actor="test")
    with _client(service) as client:
        html = client.get("/references").text
    soup = _forms(html)
    _assert_no_nested_forms(soup)

    batch_ids = {
        "batch-country": "country_alpha3",
        "batch-country-alias": "country_alias_key",
        "batch-machine": "machine_key",
        "batch-machine-alias": "machine_alias_key",
    }
    for form_id, checkbox_name in batch_ids.items():
        batch = soup.find("form", id=form_id)
        if batch is None:
            # The machine-alias batch form only renders when aliases exist.
            continue
        assert batch.get("action") == "/references/batch"
        assert batch.find("input", attrs={"name": "csrf_token"}) is not None
        resource = batch.find("input", attrs={"name": "resource"})
        assert resource is not None
        # Every row checkbox is associated to this batch form (DOM descendant or
        # HTML5 form= attribute) and each row action form is separate.
        for box in soup.find_all("input", attrs={"name": checkbox_name}):
            if box.get("form"):
                assert box["form"] == form_id
            else:
                assert box.find_parent("form") is batch

    # Every references row action form carries its own CSRF token.
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if action.startswith("/references/") and action != "/references/batch":
            assert form.find("input", attrs={"name": "csrf_token"}) is not None


def test_tasks_and_task_pages_have_no_nested_forms(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    with _client(service) as client:
        for path in ("/", f"/tasks/{task['id']}"):
            soup = _forms(client.get(path).text)
            _assert_no_nested_forms(soup)


def test_app_js_select_all_covers_alias_checkboxes() -> None:
    from cater.app.web.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in (
        "task_ids",
        "country_alpha3",
        "machine_key",
        "country_alias_key",
        "machine_alias_key",
    ):
        assert name in source


def test_machine_edit_get_shows_old_values_and_post_updates(service) -> None:
    service.references.create_machine(
        "SEM510", "", declaration="OLD-DECL", english_name="OLD-EN",
        special_rule_confirmed=True, actor="test",
    )
    with _client(service) as client:
        page = client.get("/references/machines/sem510/default/edit")
        assert page.status_code == 200
        assert "OLD-DECL" in page.text
        assert "SEM510" in page.text
        token = _csrf(client)
        response = client.post(
            "/references/machines/sem510/default/edit",
            data={
                CSRF_FIELD: token,
                "declaration": "NEW-DECL",
                "english_name": "NEW-EN",
                "hs_code": "",
                "product_name": "",
                "notes": "",
                "special": "",
                "verification": "",
                "special_rule_confirmed": "1",
                "reason": "web-edit-test",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    machine = service.references.get_machine("SEM510", "")
    assert machine["declaration"] == "NEW-DECL"
    assert machine["english_name"] == "NEW-EN"
    # Scope/key is stable: it was not changed by the edit.
    assert machine["destination_scope"] == ""
    audit = service.settings.audit(limit=50)
    assert any(item["reason"] == "web-edit-test" for item in audit)


def test_machine_edit_requires_csrf(service) -> None:
    service.references.create_machine("SEM510", "", declaration="d", actor="test")
    with _client(service) as client:
        client.get("/references")
        denied = client.post(
            "/references/machines/sem510/default/edit",
            data={"declaration": "hacked"},
            follow_redirects=False,
        )
        assert denied.status_code == 403
    assert service.references.get_machine("SEM510", "")["declaration"] == "d"


def test_references_country_pagination_is_real_pages(service) -> None:
    with _client(service) as client:
        page1 = client.get("/references").text
        assert "country_page=2" in page1  # real link, not just a limit cut
        page2 = client.get("/references?country_page=2").text

    def country_codes(html: str) -> list:
        soup = _forms(html)
        for section in soup.find_all("section"):
            h2 = section.find("h2")
            if h2 and h2.get_text(strip=True).startswith("国家（"):
                table = section.find("table")
                return [
                    tr.find_all("td")[1].get_text(strip=True)
                    for tr in table.find("tbody").find_all("tr")
                ]
        return []

    c1 = country_codes(page1)
    c2 = country_codes(page2)
    assert c1 and c2
    # Genuine slice: no overlap between page 1 and page 2, and page size 50.
    assert set(c1).isdisjoint(set(c2))
    assert len(c1) == 50


def test_health_shows_clear_blockers_when_unknown_outbox(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    service.db.insert(
        """
        INSERT INTO outbox (id, dedupe_key, task_id, version, purpose, recipient,
            subject, body, payload_hash, message_id, status, created_at)
        VALUES ('out-wu', 'dk-wu', ?, 1, 'delivery', 'r@x', 's', 'b', 'ph', 'mid-wu',
            'send_unknown', 't')
        """,
        (task["id"],),
    )
    with _client(service) as client:
        page = client.get("/health")
    assert "当前不可清空" in page.text
