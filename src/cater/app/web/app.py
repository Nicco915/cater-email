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
            "script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
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
        lifecycle = (request.query_params.get("lifecycle") or "active").strip()
        if lifecycle not in {"active", "archived", "trashed", "all"}:
            lifecycle = "active"
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 50), 500))
        except ValueError:
            limit = 50
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            page = 1
        offset = (page - 1) * limit
        tasks = service.tasks.list(
            status=status or None,
            kind=kind or None,
            search=search or None,
            lifecycle=lifecycle,
            limit=limit,
            offset=offset,
        )
        total_counts = service.tasks.lifecycle_counts()
        total = total_counts.get(lifecycle, 0)
        pages = max(1, (total + limit - 1) // limit)
        return render(
            request,
            "tasks.html",
            tasks=tasks,
            counts=service.tasks.counts(),
            lifecycle_counts=total_counts,
            statuses=sorted(state.ALL_STATUSES),
            status=status,
            kind=kind,
            search=search,
            lifecycle=lifecycle,
            limit=limit,
            page=page,
            pages=pages,
        )

    @app.post("/tasks/batch")
    async def task_batch(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        action = text(form, "action")
        raw_ids = form.getlist("task_ids")
        task_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        valid = {"archive", "unarchive", "delete", "restore"}
        if action not in valid or not task_ids:
            return flash_redirect(
                "/", error="请选择任务并指定有效操作"
            )
        reason = text(form, "reason") or None
        try:
            if action == "archive":
                service.batch_archive(task_ids, actor="web-admin", reason=reason)
            elif action == "unarchive":
                service.batch_unarchive(task_ids, actor="web-admin")
            elif action == "delete":
                service.batch_delete(task_ids, actor="web-admin", reason=reason)
            else:
                service.batch_restore(task_ids, actor="web-admin")
        except CaterError as exc:
            # BatchDisallowedError carries per-item reasons; show them so the
            # operator can resolve each before retrying. Nothing was applied.
            items = getattr(exc, "items", None)
            if items:
                detail = "；".join(f"{i['task_id']}：{i['reason']}" for i in items)
                return flash_redirect("/", error=f"批量操作已撤销：{detail}")
            return flash_redirect("/", error=f"{exc.code}: {exc}")
        except Exception as exc:
            service.logger.exception("web_batch_failed", exc, action=action)
            return flash_redirect("/", error=f"{type(exc).__name__}: {exc}")
        labels = {
            "archive": "归档",
            "unarchive": "取消归档",
            "delete": "删除到回收站",
            "restore": "从回收站恢复",
        }
        return flash_redirect("/", message=f"已{labels[action]} {len(task_ids)} 个任务")

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

    @app.post("/tasks/{task_id}/archive")
    async def task_archive(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.archive_task(task_id, actor="web-admin", reason=text(form, "reason") or None),
            success="任务已归档（仅隐藏，不影响处理与发信）",
        )

    @app.post("/tasks/{task_id}/unarchive")
    async def task_unarchive(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.unarchive_task(task_id, actor="web-admin"),
            success="任务已取消归档",
        )

    @app.post("/tasks/{task_id}/delete")
    async def task_delete(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.delete_task(task_id, actor="web-admin", reason=text(form, "reason") or None),
            success="任务已删除到回收站（暂停后续处理与发送，可恢复）",
        )

    @app.post("/tasks/{task_id}/restore")
    async def task_restore(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        task_or_404(task_id)
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.restore_task(task_id, actor="web-admin"),
            success="任务已从回收站恢复（未自动重发或重算，需显式操作）",
        )

    @app.get("/tasks/{task_id}/purge", response_class=HTMLResponse)
    def task_purge_preview(request: Request, task_id: str):
        try:
            preview = service.purge_task_files_preview(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Task not found")
        except CaterError as exc:
            return flash_redirect(f"/tasks/{task_id}", error=f"{exc.code}: {exc}")
        return render(request, "purge.html", purge=preview, task_id=task_id)

    @app.post("/tasks/{task_id}/purge")
    async def task_purge_confirm(request: Request, task_id: str):
        form = await form_data(request)
        verify_csrf(request, form)
        if not checked(form, "confirm"):
            return flash_redirect(f"/tasks/{task_id}/purge", error="需勾选确认才能永久清理")
        return run_action(
            request,
            f"/tasks/{task_id}",
            lambda: service.purge_task_files(task_id, actor="web-admin"),
            success="任务文件已永久清理（数据库摘要保留）",
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
        task_path = f"/tasks/{row['task_id']}" if row["task_id"] else "/"
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
        country_search = (request.query_params.get("country_search") or "").strip()
        machine_search = (request.query_params.get("machine_search") or "").strip()

        def paged(key: str) -> tuple[int, int]:
            try:
                page = max(1, int(request.query_params.get(key) or 1))
            except ValueError:
                page = 1
            return page, page

        per_page = 50
        country_page, _ = paged("country_page")
        machine_page, _ = paged("machine_page")
        country_total = service.references.count_countries(
            search=country_search or None
        )
        machine_total = service.references.count_machines(
            search=machine_search or None
        )
        country_pages = max(1, (country_total + per_page - 1) // per_page)
        machine_pages = max(1, (machine_total + per_page - 1) // per_page)
        country_page = min(country_page, country_pages)
        machine_page = min(machine_page, machine_pages)
        return render(
            request,
            "references.html",
            countries=service.references.list_countries(
                search=country_search or None,
                limit=per_page,
                offset=(country_page - 1) * per_page,
            ),
            aliases=service.references.country_aliases(),
            machines=service.references.list_machines(
                search=machine_search or None,
                limit=per_page,
                offset=(machine_page - 1) * per_page,
            ),
            machine_aliases=service.references.list_machine_aliases(),
            country_search=country_search,
            machine_search=machine_search,
            country_total=country_total,
            machine_total=machine_total,
            country_page=country_page,
            machine_page=machine_page,
            country_pages=country_pages,
            machine_pages=machine_pages,
            reference_version=service.references.reference_version(),
            authoritative=True,
        )

    @app.get("/references/machines/{model_norm}/{destination_scope}/edit", response_class=HTMLResponse)
    def references_machine_edit_page(request: Request, model_norm: str, destination_scope: str):
        machine = service.references.get_machine(model_norm, destination_scope)
        if machine is None:
            raise HTTPException(status_code=404, detail="Machine not found")
        return render(request, "machine_edit.html", machine=machine)

    @app.post("/references/machines/{model_norm}/{destination_scope}/edit")
    async def references_machine_edit(request: Request, model_norm: str, destination_scope: str):
        form = await form_data(request)
        verify_csrf(request, form)
        # model_norm + destination_scope form the stable key and are never
        # silently changed by the edit; only declaration/metadata are editable.
        return run_action(
            request,
            f"/references/machines/{model_norm}/{destination_scope}/edit",
            lambda: service.references.update_machine(
                model_norm,
                destination_scope,
                english_name=text(form, "english_name"),
                hs_code=text(form, "hs_code"),
                product_name=text(form, "product_name"),
                declaration=text(form, "declaration"),
                notes=text(form, "notes"),
                special=text(form, "special"),
                verification=text(form, "verification"),
                special_rule_confirmed=checked(form, "special_rule_confirmed"),
                actor="web-admin",
                reason=text(form, "reason") or "web edit",
            ),
            success="机型资料已更新并记录审计",
        )

    @app.post("/references/countries")
    async def references_country_create(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        alpha3 = text(form, "alpha3")
        name_zh = text(form, "name_zh")
        name_en = text(form, "name_en")
        return run_action(
            request,
            "/references",
            lambda: service.references.create_country(
                alpha3, name_zh, name_en, actor="web-admin", reason=text(form, "reason") or None
            ),
            success=f"已新增国家 {alpha3.upper()}",
        )

    @app.post("/references/countries/{alpha3}")
    async def references_country_update(request: Request, alpha3: str):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.update_country(
                alpha3,
                name_zh=text(form, "name_zh") or None,
                name_en=text(form, "name_en") or None,
                actor="web-admin",
                reason=text(form, "reason") or None,
            ),
            success=f"已更新国家 {alpha3.upper()}",
        )

    @app.post("/references/countries/{alpha3}/enabled")
    async def references_country_enabled(request: Request, alpha3: str):
        form = await form_data(request)
        verify_csrf(request, form)
        enabled = checked(form, "enabled")
        return run_action(
            request,
            "/references",
            lambda: service.references.set_country_enabled(
                alpha3, enabled, actor="web-admin", reason=text(form, "reason") or None
            ),
            success=f"国家 {alpha3.upper()} 已{'启用' if enabled else '停用'}",
        )

    @app.post("/references/countries/{alpha3}/delete")
    async def references_country_delete(request: Request, alpha3: str):
        form = await form_data(request)
        verify_csrf(request, form)
        force = checked(form, "force")
        return run_action(
            request,
            "/references",
            lambda: service.references.delete_country(
                alpha3, actor="web-admin", reason=text(form, "reason") or None, force=force
            ),
            success=f"国家 {alpha3.upper()} 已{'物理删除' if force else '停用'}",
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

    @app.post("/references/aliases/{alias_norm}/delete")
    async def references_alias_delete(request: Request, alias_norm: str):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.delete_alias(
                alias_norm, actor="web-admin", reason=text(form, "reason") or None
            ),
            success="已删除国家别名",
        )

    @app.post("/references/machines")
    async def references_machine_create(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.create_machine(
                text(form, "model"),
                text(form, "destination_scope"),
                english_name=text(form, "english_name") or None,
                hs_code=text(form, "hs_code") or None,
                product_name=text(form, "product_name") or None,
                declaration=text(form, "declaration") or None,
                notes=text(form, "notes") or None,
                special=text(form, "special") or None,
                verification=text(form, "verification") or None,
                special_rule_confirmed=checked(form, "special_rule_confirmed"),
                actor="web-admin",
                reason=text(form, "reason") or None,
            ),
            success="已新增机型记录",
        )

    @app.post("/references/machines/{model_norm}/{destination_scope}/enabled")
    async def references_machine_enabled(request: Request, model_norm: str, destination_scope: str):
        form = await form_data(request)
        verify_csrf(request, form)
        enabled = checked(form, "enabled")
        return run_action(
            request,
            "/references",
            lambda: service.references.set_machine_enabled(
                model_norm, destination_scope, enabled, actor="web-admin", reason=text(form, "reason") or None
            ),
            success=f"机型已{'启用' if enabled else '停用'}",
        )

    @app.post("/references/machines/{model_norm}/{destination_scope}/delete")
    async def references_machine_delete(request: Request, model_norm: str, destination_scope: str):
        form = await form_data(request)
        verify_csrf(request, form)
        force = checked(form, "force")
        return run_action(
            request,
            "/references",
            lambda: service.references.delete_machine(
                model_norm, destination_scope, actor="web-admin",
                reason=text(form, "reason") or None, force=force,
            ),
            success=f"机型已{'物理删除' if force else '停用'}",
        )

    @app.post("/references/aliases/{alias_norm}/enabled")
    async def references_alias_enabled(request: Request, alias_norm: str):
        form = await form_data(request)
        verify_csrf(request, form)
        enabled = checked(form, "enabled")
        return run_action(
            request,
            "/references",
            lambda: service.references.set_alias_enabled(
                alias_norm, enabled, actor="web-admin", reason=text(form, "reason") or None
            ),
            success=f"国家别名已{'启用' if enabled else '停用'}",
        )

    @app.post("/references/aliases/{alias_norm}/edit")
    async def references_alias_edit(request: Request, alias_norm: str):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.update_country_alias(
                alias_norm, text(form, "alias"), actor="web-admin", reason=text(form, "reason") or None
            ),
            success="国家别名已更新",
        )

    @app.post("/references/machine-aliases")
    async def references_machine_alias_add(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.add_machine_alias(
                text(form, "model"),
                text(form, "alias"),
                text(form, "destination_scope"),
                actor="web-admin",
                reason=text(form, "reason") or None,
            ),
            success="已新增机型别名",
        )

    @app.post("/references/machine-aliases/{alias_norm}/{destination_scope}/edit")
    async def references_machine_alias_edit(request: Request, alias_norm: str, destination_scope: str):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.update_machine_alias(
                alias_norm, destination_scope, text(form, "alias"),
                actor="web-admin", reason=text(form, "reason") or None,
            ),
            success="机型别名已更新",
        )

    @app.post("/references/machine-aliases/{alias_norm}/{destination_scope}/enabled")
    async def references_machine_alias_enabled(request: Request, alias_norm: str, destination_scope: str):
        form = await form_data(request)
        verify_csrf(request, form)
        enabled = checked(form, "enabled")
        return run_action(
            request,
            "/references",
            lambda: service.references.set_machine_alias_enabled(
                alias_norm, destination_scope, enabled,
                actor="web-admin", reason=text(form, "reason") or None,
            ),
            success=f"机型别名已{'启用' if enabled else '停用'}",
        )

    @app.post("/references/machine-aliases/{alias_norm}/{destination_scope}/delete")
    async def references_machine_alias_delete(request: Request, alias_norm: str, destination_scope: str):
        form = await form_data(request)
        verify_csrf(request, form)
        return run_action(
            request,
            "/references",
            lambda: service.references.delete_machine_alias(
                alias_norm, destination_scope, actor="web-admin", reason=text(form, "reason") or None
            ),
            success="已删除机型别名",
        )

    @app.post("/references/batch")
    async def references_batch(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        resource = text(form, "resource")
        enabled = checked(form, "enabled")
        if resource == "country":
            keys = [{"alpha3": v} for v in form.getlist("country_alpha3")]
        elif resource == "machine":
            raw = form.getlist("machine_key")
            keys = []
            for item in raw:
                parts = str(item).split("|", 1)
                if len(parts) == 2:
                    keys.append({"model": parts[0], "destination_scope": parts[1]})
        elif resource == "country_alias":
            keys = [{"alias_norm": v} for v in form.getlist("country_alias_key")]
        elif resource == "machine_alias":
            keys = []
            for item in form.getlist("machine_alias_key"):
                parts = str(item).split("|", 1)
                if len(parts) == 2:
                    keys.append({"alias_norm": parts[0], "destination_scope": parts[1]})
        else:
            keys = []
        return run_action(
            request,
            "/references",
            lambda: service.references.batch_set_enabled(
                resource, keys, enabled, actor="web-admin", reason=text(form, "reason") or None
            ),
            success=f"已{'启用' if enabled else '停用'} {len(keys)} 条资料",
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
        # Preview only: nothing is written until the operator confirms on the
        # preview page with a short-lived token bound to the reference version.
        try:
            preview = service.preview_machine_reference(str(target), actor="web-admin")
        except CaterError as exc:
            return flash_redirect("/references", error=f"{exc.code}: {exc}")
        except Exception as exc:
            service.logger.exception("web_import_preview_failed", exc)
            return flash_redirect("/references", error=f"{type(exc).__name__}: {exc}")
        params = {"token": preview["token"], "message": f"已生成预览：{preview['summary']}"}
        return RedirectResponse(
            f"/references/machines/preview?{urlencode(params)}", status_code=303
        )

    @app.get("/references/machines/preview", response_class=HTMLResponse)
    def references_import_preview_page(request: Request):
        token = (request.query_params.get("token") or "").strip()
        if not token:
            return render(request, "import_preview.html", preview=None)
        preview = service.references.get_preview(token)
        if preview is None:
            return render(
                request,
                "import_preview.html",
                preview=None,
                error="预览不存在或已失效，请重新上传",
            )
        import json as _json
        summary = _json.loads(str(preview["summary_json"]))
        payload = _json.loads(str(preview["rows_json"]))
        return render(
            request,
            "import_preview.html",
            preview=preview,
            summary=summary,
            new_rows=payload.get("new", []) if isinstance(payload, dict) else [],
            unchanged_rows=payload.get("unchanged", []) if isinstance(payload, dict) else [],
            modified_rows=payload.get("modified", []) if isinstance(payload, dict) else [],
            disabled_rows=payload.get("disabled", []) if isinstance(payload, dict) else [],
            token=token,
        )

    @app.post("/references/machines/confirm")
    async def references_import_confirm(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        token = text(form, "token")
        return run_action(
            request,
            "/references",
            lambda: service.confirm_machine_reference(token, actor="web-admin"),
            success="机型库已按预览确认写入",
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
        poller_running = bool(poller is not None and poller.running)
        return render(
            request,
            "health.html",
            health=service.health(),
            recovery=service.recovery_report,
            backups=backups,
            test_clear=service.clear_test_data_preview(poller_running=poller_running),
        )

    @app.post("/maintenance/clear-test")
    async def maintenance_clear_test(request: Request):
        form = await form_data(request)
        verify_csrf(request, form)
        if not checked(form, "confirm"):
            return flash_redirect("/health", error="需勾选二次确认才能清空测试数据")
        poller_running = bool(poller is not None and poller.running)
        if poller_running:
            return flash_redirect(
                "/health", error="邮箱轮询仍在运行：请先停止轮询（--no-poll）后再清空测试数据"
            )
        return run_action(
            request,
            "/health",
            lambda: service.clear_test_data(
                actor="web-admin", confirm=True, poller_running=poller_running
            ),
            success="测试任务数据已清空（参考资料与配置保留，已先备份）",
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