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
        sender="tester@example.com",
        destination="法国",
        source_paths=[source],
    )


def test_default_testserver_host_is_rejected(service) -> None:
    client = TestClient(create_app(service))
    response = client.get("/")
    assert response.status_code == 400


def test_loopback_pages_and_security_headers_render(service) -> None:
    with _client(service) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "CATER" in response.text
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_state_change_requires_csrf_and_is_audited(service) -> None:
    with _client(service) as client:
        assert client.get("/settings").status_code == 200
        denied = client.post(
            "/settings",
            data={"mode": "test", "test": "tester@example.com", "dry_run": "true"},
            follow_redirects=False,
        )
        assert denied.status_code == 403

        token = _csrf(client)
        accepted = client.post(
            "/settings",
            data={
                CSRF_FIELD: token,
                "mode": "test",
                "test": "tester@example.com",
                "production": "",
                "dry_run": "false",
                "enabled": "false",
                "poll_interval": "30",
                "reason": "web-test",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert service.settings.current().sending.dry_run is False
        audit = service.settings.audit(limit=10)
        assert any(item["reason"] == "web-test" for item in audit)


def test_cross_origin_post_is_rejected_even_with_token(service) -> None:
    with _client(service) as client:
        client.get("/settings")
        token = _csrf(client)
        response = client.post(
            "/settings",
            headers={"origin": "http://evil.example"},
            data={CSRF_FIELD: token, "mode": "test"},
            follow_redirects=False,
        )
        assert response.status_code == 403


def _settings_payload(token: str) -> dict:
    return {
        CSRF_FIELD: token,
        "mode": "test",
        "test": "tester@example.com",
        "production": "",
        "dry_run": "true",
        "enabled": "false",
        "poll_interval": "60",
        "reason": "web-origin-test",
    }


def test_same_origin_post_with_matching_origin_header_succeeds(service) -> None:
    with _client(service) as client:
        client.get("/settings")
        token = _csrf(client)
        response = client.post(
            "/settings",
            headers={"origin": BASE_URL},
            data=_settings_payload(token),
            follow_redirects=False,
        )
        assert response.status_code == 303


def test_origin_must_match_scheme_host_and_port_exactly(service) -> None:
    with _client(service) as client:
        client.get("/settings")
        token = _csrf(client)
        hostile_origins = [
            "http://127.0.0.1:9999",  # wrong port
            "https://127.0.0.1:8765",  # wrong scheme
            "http://localhost:8765",  # different loopback host is cross-origin
            "http://user@127.0.0.1:8765",  # userinfo is never same-origin
            "http://127.0.0.1:8765/path",  # origins never carry a path
            "null",
        ]
        for origin in hostile_origins:
            response = client.post(
                "/settings",
                headers={"origin": origin},
                data=_settings_payload(token),
                follow_redirects=False,
            )
            assert response.status_code == 403, origin
            assert "Invalid CSRF" not in response.text


def test_null_origin_rejection_explains_how_to_recover(service) -> None:
    # Browsers posting from a page cached with the old no-referrer policy send
    # ``Origin: null``; the operator must know to refresh the page.
    with _client(service) as client:
        client.get("/settings")
        token = _csrf(client)
        response = client.post(
            "/settings",
            headers={"origin": "null"},
            data=_settings_payload(token),
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert "刷新页面" in response.text


def test_artifact_download_is_controlled_by_task_root(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    assert task["status"] == state.READY_TO_SEND
    artifact = service.db.fetchone(
        "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY id LIMIT 1",
        (task["id"],),
    )
    assert artifact is not None

    with _client(service) as client:
        allowed = client.get(f"/artifacts/{artifact['id']}")
        assert allowed.status_code == 200
        assert allowed.content == Path(str(artifact["path"])).read_bytes()

        outside = service.config.paths.runtime_dir.parent / "outside.xlsx"
        outside.write_bytes(b"outside")
        service.db.insert(
            "UPDATE task_artifacts SET path = ? WHERE id = ?",
            (str(outside), artifact["id"]),
        )
        blocked = client.get(f"/artifacts/{artifact['id']}")
        assert blocked.status_code == 404


def test_diagnostics_export_and_download(service) -> None:
    with _client(service) as client:
        client.get("/diagnostics")
        token = _csrf(client)
        created = client.post(
            "/diagnostics",
            data={CSRF_FIELD: token},
            follow_redirects=False,
        )
        assert created.status_code == 200
        assert created.content[:2] == b"PK"
        files = sorted(service.config.diagnostics_dir.glob("cater-diagnostics-*.zip"))
        assert files
        response = client.get(f"/diagnostics/files/{files[-1].name}")
        assert response.status_code == 200
        assert response.content[:2] == b"PK"


def test_unknown_task_and_artifact_return_404(service) -> None:
    with _client(service) as client:
        assert client.get("/tasks/task_missing").status_code == 404
        assert client.get("/artifacts/999999").status_code == 404


def test_references_page_shows_destination_scopes_and_special_marker(service) -> None:
    service.references.store_machines(
        [
            {"model": "915F", "declaration": "110KW", "destination_scope": ""},
            {
                "model": "915F",
                "declaration": "123KW",
                "destination_scope": "BRA",
                "special": "巴西",
                "verification": "特殊配置",
                "source_row": 40,
            },
        ],
        source="fixture.xlsx",
        actor="test",
    )
    with _client(service) as client:
        response = client.get("/references")
    assert response.status_code == 200
    assert "巴西" in response.text
    assert "BRA" in response.text
    assert "110KW" in response.text
    assert "123KW" in response.text