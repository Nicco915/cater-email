"""Loopback-only local administration web UI.

The UI is a thin server-rendered layer over :class:`AppService`. It never
contains business conversion logic and never accepts arbitrary filesystem
paths for download; artifacts are addressed by database id and re-validated
against the runtime task directory.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import state
from ..application import AppService
from ..errors import CaterError
from ..runner import PollingService
from ..util import is_within, safe_filename, sha256_bytes, utc_now
from .security import (
    COOKIE_NAME,
    CSRF_FIELD,
    SessionSigner,
    ensure_web_secret,
    origin_allowed,
    request_host_allowed,
)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _short(value: Any, length: int = 80) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[: length - 1] + "…"


def _epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def create_app(
    service: AppService,
    *,
    poller: PollingService | None = None,
    signer: SessionSigner | None = None,
) -> FastAPI:
    secret = ensure_web_secret(service.config.web_secret_path) if signer is None else None
    session_signer = signer or SessionSigner(
        secret or "", max_age_seconds=service.config.web.session_max_age_seconds
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if poller is not None:
            poller.start()
        try:
            yield
        finally:
            if poller is not None:
                poller.stop()

    app = FastAPI(
        title="CATER local admin",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.poller = poller
    app.state.session_signer = session_signer

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["jsonpretty"] = _json_pretty
    templates.env.filters["shorten"] = _short
    templates.env.filters["epoch"] = _epoch
    templates.env.globals["COOKIE_NAME"] = COOKIE_NAME
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # middleware
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def session_and_headers(request: Request, call_next):
        if not request_host_allowed(request.headers.get("host")):
            return PlainTextResponse("Invalid Host header", status_code=400)
        session = session_signer.load(request.cookies.get(COOKIE_NAME))
        dirty = False
        if not session.get("csrf"):
            session = SessionSigner.new_session()
            dirty = True
        request.state.session = session
        request.state.session_dirty = dirty
        response = await call_next(request)
        if request.state.session_dirty:
            response.set_cookie(
                COOKIE_NAME,
                session_signer.dump(session),
                max_age=session_signer.max_age_seconds,
                httponly=True,
                samesite="lax",
                path="/",
            )
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; img-src 'self' data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        return response

    # ------------------------------------------------------------------
    # rendering and form helpers
    # ------------------------------------------------------------------
    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        payload = {
            "request": request,
            "service": service,
            "csrf_token": request.state.session.get("csrf", ""),
            "error": request.query_params.get("error"),
            "message": request.query_params.get("message"),
            "now": utc_now(),
        }
        payload.update(context)
        return templates.TemplateResponse(request=request, name=template, context=payload)

    def flash_redirect(
        path: str,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        params: dict[str, str] = {}
        if message:
            params["message"] = message[:800]
        if error:
            params["error"] = error[:800]
        target = f"{path}?{urlencode(params)}" if params else path
        return RedirectResponse(target, status_code=303)

    async def form_data(request: Request) -> Any:
        try:
            return await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid form submission") from exc

    def verify_csrf(request: Request, form: Any) -> None:
        origin = request.headers.get("origin")
        if origin is not None and origin.strip().lower() == "null":
            # Pages cached before the Referrer-Policy fix post with an empty
            # Origin; tell the operator exactly how to recover.
            raise HTTPException(
                status_code=403,
                detail="无法确认请求来源（Origin 为空）：浏览器可能仍使用旧版页面，请刷新页面后重新提交",
            )
        request_origin = f"{request.url.scheme}://{request.url.netloc}"
        if not origin_allowed(origin, request_origin):
            raise HTTPException(
                status_code=403, detail="跨站请求被拒绝：请从本机管理页面重新操作"
            )
        supplied = form.get(CSRF_FIELD) or request.headers.get("x-csrf-token")
        expected = request.state.session.get("csrf")
        if not session_signer.tokens_equal(expected, str(supplied or "")):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def text(form: Any, name: str, default: str = "") -> str:
        value = form.get(name)
        return default if value is None else str(value)

    def checked(form: Any, name: str) -> bool:
        return str(form.get(name) or "").strip().lower() in {"1", "true", "on", "yes"}

    def task_or_404(task_id: str) -> dict[str, Any]:
        task = service.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    def run_action(
        request: Request,
        path: str,
        action: Callable[[], Any],
        *,
        success: str,
    ) -> RedirectResponse:
        try:
            action()
        except KeyError:
            raise HTTPException(status_code=404, detail="Record not found")
        except HTTPException:
            raise
        except CaterError as exc:
            return flash_redirect(path, error=f"{exc.code}: {exc}")
        except ValueError as exc:
            return flash_redirect(path, error=str(exc))
        except Exception as exc:  # log detail, show a safe summary
            service.logger.exception("web_action_failed", exc, path=path)
            return flash_redirect(path, error=f"{type(exc).__name__}: {exc}")
        return flash_redirect(path, message=success)

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def task_list(request: Request):
        status = (request.query_params.get("status") or "").strip()
        kind = (request.query_params.get("kind") or "").strip()
        search = (request.query_params.get("search") or "").strip()
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 100), 500))
        except ValueError:
            limit = 100
        tasks = service.tasks.list(
            status=status or None,
            kind=kind or None,
            search=search or None,
            limit=limit,
        )
        return render(
            request,
            "tasks.html",
            tasks=tasks,
            counts=service.tasks.counts(),
            statuses=sorted(state.ALL_STATUSES),
            status=status,
            kind=kind,
            search=search,
            limit=limit,
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: str):
        try:
            version = request.query_params.get("version")
            detail = service.task_detail(
                task_id, version=int(version) if version else None
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Task not found")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid version")
        return render(request, "task.html", **detail)

    @app.post("/tasks/{task_id}/retry")
    async def task_retry(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.retry_task(task_id, actor="web-admin"),
            success="任务已重新处理",
        )

    @app.post("/tasks/{task_id}/reprocess")
    async def task_reprocess(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        regenerate_only = text(form, "mode") == "regenerate"
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.reprocess_task(
                task_id, actor="web-admin", regenerate_only=regenerate_only
            ),
            success="已创建新版本并重新生成" if regenerate_only else "已创建新版本并进入交付流程",
        )

    @app.post("/tasks/{task_id}/confirm-reprocess")
    async def task_confirm_reprocess(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.confirm_reprocess(task_id, actor="web-admin"),
            success="已确认重新处理",
        )

    @app.post("/tasks/{task_id}/deliver")
    async def task_deliver(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.deliver_task(task_id, actor="web-admin"),
            success="已创建交付邮件",
        )

    # ------------------------------------------------------------------
    # outbox
    # ------------------------------------------------------------------
    ACTIONS: dict[str, str] = {
        "release": "release_dry_run",
        "recheck": "recheck_unknown",
        "confirm-sent": "confirm_unknown_sent",
        "confirm-not-sent": "confirm_unknown_not_sent",
        "retry": "retry_failed",
    }

    @app.post("/outbox/{outbox_id}/{action}")
    async def outbox_action(request: Request, outbox_id: str, action: str):
        form = await form_data(request)
        verify_csrf(request, form)
        if action not in ACTIONS:
            raise HTTPException(status_code=404, detail="Unknown outbox action")
        row = service.outbox.get(outbox_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Outbox message not found")
        task_path = f"/tasks/{row['task_id']}"
        if action == "recheck":
            try:
                found = service.recheck_unknown(outbox_id, actor="web-admin")
            except CaterError as exc:
                return flash_redirect(task_path, error=f"{exc.code}: {exc}")
            except Exception as exc:
                service.logger.exception("web_recheck_failed", exc, outbox_id=outbox_id)
                return flash_redirect(task_path, error=f"{type(exc).__name__}: {exc}")
            if not found:
                return flash_redirect(
                    task_path,
                    error="已发送文件夹中未找到该邮件；请人工核对，不要直接重发",
                )
            return flash_redirect(task_path, message="已在已发送文件夹确认该邮件")
        method = getattr(service, ACTIONS[action])
        return run_action(
            request,
            task_path,
            lambda: method(outbox_id, actor="web-admin"),
            success="发件箱操作已完成",
        )

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------
    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request):
        query = (request.query_params.get("query") or "").strip()
        level = (request.query_params.get("level") or "").strip()
        event = (request.query_params.get("event") or "").strip()
        task_id = (request.query_params.get("task_id") or "").strip()
        mail_id = (request.query_params.get("mail_id") or "").strip()
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 200), 1000))
        except ValueError:
            limit = 200
        records = service.logger.search(
            task_id=task_id or None,
            mail_id=mail_id or None,
            level=level or None,
            event=event or None,
            query=query or None,
            limit=limit,
        )
        return render(
            request,
            "logs.html",
            records=records,
            query=query,
            level=level,
            event=event,
            task_id=task_id,
            mail_id=mail_id,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        effective = service.settings.current()
        return render(
            request,
            "settings.html",
            settings=effective.as_dict(),
            audit=service.settings.audit(limit=100),
            credential_presence=service.credentials.describe(service.config.mail.account),
        )

    @app.post("/settings")
    async def settings_update(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        emails = lambda raw: [
            item
            for item in re.split(r"[,;，；\s]+", raw or "")
            if item.strip()
        ]
        changes = {
            "allowlist.mode": text(form, "mode", "test"),
            "allowlist.test": emails(text(form, "test")),
            "allowlist.production": emails(text(form, "production")),
            "sending.dry_run": checked(form, "dry_run"),
            "sending.enabled": checked(form, "enabled"),
            "scan.poll_interval_seconds": text(form, "poll_interval", "60"),
        }
        reason = text(form, "reason", "web admin").strip() or "web admin"
        return run_action(
            request,
            "/settings",
            lambda: service.update_settings(
                changes,
                actor="web-admin",
                reason=reason,
                production_ack=checked(form, "production_ack"),
            ),
            success="配置已更新并记录审计",
        )

    # ------------------------------------------------------------------
    # references
    # ------------------------------------------------------------------
    @app.get("/references", response_class=HTMLResponse)
    def references_page(request: Request):
        return render(
            request,
            "references.html",
            countries=service.references.list_countries(),
            aliases=service.references.country_aliases(),
            machines=service.references.list_machines(),
        )

    @app.post("/references/alias")
    async def references_alias(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        alias = text(form, "alias").strip()
        alpha3 = text(form, "alpha3").strip().upper()
        return run_action(
            request,
            "/references",
            lambda: service.references.add_alias(
                alias, alpha3, actor="web-admin", reason="web admin"
            ),
            success=f"已添加国家别名 {alias} → {alpha3}",
        )

    @app.post("/references/machines/import")
    async def references_import_machines(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return flash_redirect("/references", error="请选择一个 xlsx 机型库文件")
        filename = safe_filename(upload.filename, fallback="machines.xlsx")
        if not filename.lower().endswith(".xlsx"):
            return flash_redirect("/references", error="机型库导入只接受 xlsx 文件")
        maximum = max(1024, int(service.config.mail.max_attachment_bytes))
        data = await upload.read(maximum + 1)
        if len(data) > maximum:
            return flash_redirect("/references", error="机型库文件超过大小限制")
        directory = service.config.paths.runtime_dir / "imports"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{sha256_bytes(data)[:16]}_{filename}"
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return run_action(
            request,
            "/references",
            lambda: service.import_machine_reference(str(target), actor="web-admin"),
            success="机型库导入完成",
        )

    # ------------------------------------------------------------------
    # health and diagnostics
    # ------------------------------------------------------------------
    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request):
        backups = []
        if service.config.backups_dir.exists():
            backups = sorted(
                (
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "modified": path.stat().st_mtime,
                    }
                    for path in service.config.backups_dir.glob("cater-*.sqlite3")
                    if path.is_file()
                ),
                key=lambda item: item["modified"],
                reverse=True,
            )[:10]
        return render(
            request,
            "health.html",
            health=service.health(),
            recovery=service.recovery_report,
            backups=backups,
        )

    @app.get("/api/health")
    def health_json():
        return JSONResponse(service.health())

    @app.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics_page(request: Request):
        files = []
        if service.config.diagnostics_dir.exists():
            files = sorted(
                (
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "modified": path.stat().st_mtime,
                    }
                    for path in service.config.diagnostics_dir.glob("cater-diagnostics-*.zip")
                    if path.is_file()
                ),
                key=lambda item: item["modified"],
                reverse=True,
            )[:10]
        return render(request, "diagnostics.html", files=files)

    @app.post("/diagnostics")
    async def diagnostics_export(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        try:
            path = service.maintenance.export_diagnostics()
        except Exception as exc:
            service.logger.exception("web_diagnostics_failed", exc)
            return flash_redirect("/diagnostics", error=f"{type(exc).__name__}: {exc}")
        resolved = path.resolve()
        if not is_within(resolved, service.config.diagnostics_dir.resolve()) or not resolved.is_file():
            raise HTTPException(status_code=500, detail="Diagnostics export produced an unsafe path")
        return FileResponse(
            resolved,
            media_type="application/zip",
            filename=resolved.name,
        )

    @app.get("/diagnostics/files/{filename}")
    def diagnostics_download(filename: str):
        if not re.fullmatch(r"cater-diagnostics-[0-9TZ]+\.zip", filename):
            raise HTTPException(status_code=404, detail="Diagnostics file not found")
        path = (service.config.diagnostics_dir / filename).resolve()
        if (
            not is_within(path, service.config.diagnostics_dir.resolve())
            or not path.is_file()
        ):
            raise HTTPException(status_code=404, detail="Diagnostics file not found")
        return FileResponse(path, media_type="application/zip", filename=filename)

    # ------------------------------------------------------------------
    # controlled artifact download
    # ------------------------------------------------------------------
    @app.get("/artifacts/{artifact_id}")
    def download_artifact(artifact_id: int):
        row = service.db.fetchone(
            "SELECT * FROM task_artifacts WHERE id = ?", (artifact_id,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = Path(str(row["path"])).resolve()
        tasks_root = service.config.tasks_dir.resolve()
        if not is_within(path, tasks_root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact is unavailable")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=safe_filename(str(row["filename"]), fallback="artifact.xlsx"),
        )

    @app.get("/favicon.ico")
    def favicon():
        return PlainTextResponse("", status_code=204)

    # ------------------------------------------------------------------
    # safety net
    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        service.logger.exception("web_unhandled_error", exc, path=request.url.path)
        return PlainTextResponse(
            "Internal error. See runtime/logs for the matching request id.", status_code=500
        )

    return app