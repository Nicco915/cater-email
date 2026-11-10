"""Command line entrypoint for offline processing, polling and administration.

Commands never send mail unless the effective settings and allowlist permit
live sending. Credentials are read from the environment or Windows Credential
Manager and are never accepted on the command line.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import __version__, state
from .application import AppService
from .config import CredentialProvider, load_config, validate_config
from .errors import CaterError, ConfigurationError, ValidationError
from .engine import CoreUnavailable
from .runner import PollingService
from .util import utc_now


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def _clean_cell(value: Any, width: int = 60) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> None:
    body = [[_clean_cell(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    pattern = "  ".join(f"{{:<{width}}}" for width in widths)
    print(pattern.format(*headers))
    print(pattern.format(*["-" * width for width in widths]))
    for row in body:
        print(pattern.format(*row))


def _print_mapping(payload: dict[str, Any], indent: int = 0) -> None:
    for key, value in payload.items():
        prefix = " " * indent
        if isinstance(value, dict):
            print(f"{prefix}{key}:")
            _print_mapping(value, indent + 2)
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            print(f"{prefix}{key}:")
            _emit_json(value)
        else:
            print(f"{prefix}{key}: {_clean_cell(value, 200)}")


# ---------------------------------------------------------------------------
# service helpers
# ---------------------------------------------------------------------------


@contextmanager
def _service(args: argparse.Namespace, **kwargs: Any) -> Iterator[AppService]:
    config = load_config(args.config, args.runtime)
    service = AppService(config, **kwargs)
    try:
        yield service
    finally:
        service.close()


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "kind": task.get("kind"),
        "version": task.get("version"),
        "owner": task.get("owner"),
        "owner_name": task.get("owner_name"),
        "destination": task.get("destination_name"),
        "destination_alpha3": task.get("destination_alpha3"),
        "subject": task.get("mail_subject"),
        "open_issues": task.get("open_issues"),
        "outbox_count": task.get("outbox_count"),
        "last_error": task.get("last_error"),
        "updated_at": task.get("updated_at"),
    }


def _task_text(payload: dict[str, Any]) -> None:
    task = payload["task"]
    print(f"任务: {task['id']}")
    print(f"状态: {task['status']}  类型: {task['kind']}  版本: v{task['version']}")
    print(f"发起人: {task.get('owner_name') or ''} <{task.get('owner')}>")
    print(f"目的国: {task.get('destination_name')} / {task.get('destination_alpha3')}")
    print(f"主题: {task.get('mail_subject') or '-'}")
    print(f"最近错误: {task.get('last_error') or '-'}")
    print(f"输出目录: {task.get('output_dir') or '-'}")
    issues = [item for item in payload.get("issues", []) if item.get("status") == "open"]
    print(f"\n待解决问题 ({len(issues)}):")
    for issue in issues:
        print(
            f"- [{issue.get('code')}] {issue.get('record_key') or '-'}."
            f"{issue.get('field') or '-'}: {issue.get('message')}"
        )
        if issue.get("required_action"):
            print(f"  需要: {issue['required_action']}")
    outbox = payload.get("outbox", [])
    print(f"\n发件箱 ({len(outbox)}):")
    for item in outbox:
        print(
            f"- {item.get('purpose')}: {item.get('status')} attempts={item.get('attempts')} "
            f"{item.get('message_id')}"
        )
        if item.get("last_error"):
            print(f"    {item['last_error']}")
    artifacts = payload.get("artifacts", [])
    print(f"\n输出文件 ({len(artifacts)}):")
    for item in artifacts:
        print(f"- [{item.get('id')}] {item.get('filename')} {item.get('size_bytes')} bytes")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config(args.config, args.runtime)
    with _service(args, recover=False) as service:
        payload = {
            "runtime_dir": str(config.paths.runtime_dir),
            "database": str(config.db_path),
            "schema_version": service.db.schema_version(),
            "logs_dir": str(config.logs_dir),
            "tasks_dir": str(config.tasks_dir),
            "config_file": str(config.paths.config_path) if config.paths.config_path else None,
            "note": "Copy config/config.example.toml to config/config.toml and review it.",
        }
    if args.json:
        _emit_json(payload)
    else:
        _print_mapping(payload)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    with _service(args) as service:
        payload = service.health()
        payload["recovery"] = service.recovery_report
    if args.json:
        _emit_json(payload)
    else:
        _print_mapping(payload)
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    with _service(args) as service:
        rows = service.tasks.list(
            status=args.status or None,
            kind=args.kind or None,
            search=args.search or None,
            limit=args.limit,
        )
        payload = [_task_summary(row) for row in rows]
    if args.json:
        _emit_json(payload)
    else:
        _print_table(
            ["任务号", "状态", "类型", "版本", "发起人", "目的国", "问题", "更新时间"],
            (
                [
                    item["id"],
                    item["status"],
                    item["kind"],
                    f"v{item['version']}",
                    item["owner"],
                    item["destination_alpha3"] or item["destination"] or "-",
                    item["open_issues"],
                    item["updated_at"],
                ]
                for item in payload
            ),
        )
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    with _service(args) as service:
        try:
            payload = service.task_detail(args.task_id, version=args.version)
        except KeyError:
            print(f"Task not found: {args.task_id}", file=sys.stderr)
            return 1
    if args.json:
        _emit_json(payload)
    else:
        _task_text(payload)
    return 0


def cmd_outbox(args: argparse.Namespace) -> int:
    with _service(args) as service:
        rows = service.outbox.list_recent(status=args.status or None, limit=args.limit)
        payload = [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "version": row["version"],
                "purpose": row["purpose"],
                "status": row["status"],
                "recipient": row["recipient"],
                "subject": row["subject"],
                "attempts": row["attempts"],
                "message_id": row["message_id"],
                "last_error": row["last_error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    if args.json:
        _emit_json(payload)
    else:
        _print_table(
            ["发件号", "任务号", "用途", "状态", "收件人", "尝试", "创建时间"],
            (
                [
                    item["id"],
                    item["task_id"],
                    item["purpose"],
                    item["status"],
                    item["recipient"],
                    item["attempts"],
                    item["created_at"],
                ]
                for item in payload
            ),
        )
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    with _service(args) as service:
        records = service.logger.search(
            task_id=args.task_id or None,
            mail_id=args.mail_id or None,
            level=args.level or None,
            event=args.event or None,
            query=args.query or None,
            limit=args.limit,
        )
    if args.json:
        _emit_json(records)
    else:
        for record in records:
            print(
                f"{record.get('ts')} {record.get('level'):<7} {record.get('event')} "
                f"task={record.get('task_id', '-')} mail={record.get('mail_id', '-')}"
            )
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    with _service(args) as service:
        cycles = 0
        while True:
            cycles += 1
            try:
                report = service.poll_once(include_history=args.history and cycles == 1)
                sent = service.send_pending(limit=20, actor="cli-poller")
                retried = service.retry_pending_tasks(limit=10, actor="cli-poller")
                maintenance = service.maintenance.maybe_daily()
                payload = {
                    "cycle": cycles,
                    "poll": report.as_dict(),
                    "sent": len(sent),
                    "retried": len(retried),
                    "maintenance": maintenance,
                }
                if args.json:
                    _emit_json(payload)
                else:
                    _print_mapping(
                        {
                            "cycle": cycles,
                            "poll": report.as_dict(),
                            "sent": len(sent),
                            "retried": len(retried),
                        }
                    )
            except Exception as exc:
                service.logger.exception("cli_poll_failed", exc)
                print(f"poll failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                if not args.loop:
                    return 1
            if not args.loop:
                return 0
            if args.max_cycles and cycles >= args.max_cycles:
                return 0
            interval = args.interval or service.settings.current().poll_interval_seconds
            time.sleep(max(5, int(interval)))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    config = load_config(args.config, args.runtime)
    host = args.host or config.web.host
    port = args.port or config.web.port
    loopback = {"127.0.0.1", "::1", "localhost"}
    if host not in loopback:
        raise ConfigurationError("Web UI must bind to a loopback address (127.0.0.1 or ::1)")
    with _service(args) as service:
        poller = None if args.no_poll else PollingService(service)
        app = create_app(service, poller=poller)
        print(f"CATER local admin: http://{host}:{port}/")
        print("Press Ctrl+C to stop.")
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=True,
            proxy_headers=False,
            # Windows local HTTP clients may reset keep-alive connections.
            # Avoid the Proactor shutdown callback without hiding exceptions.
            loop="cater.app.event_loop:new_server_loop" if sys.platform == "win32" else "auto",
        )
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    if args.eml and args.source:
        raise ValidationError("Use either --eml or positional source files, not both")
    with _service(args) as service:
        if args.eml:
            results = []
            for item in args.eml:
                result = service.process_eml_file(item, actor="cli")
                results.append(
                    {
                        "mail_id": result.mail_id,
                        "task_id": result.task_id,
                        "created": result.created,
                        "duplicate": result.duplicate,
                        "filtered": result.filtered,
                        "filter_reason": result.filter_reason,
                        "supplement": result.supplement,
                    }
                )
            if args.json:
                _emit_json(results)
            else:
                for item in results:
                    print(json.dumps(item, ensure_ascii=False))
            return 0
        result = service.submit_offline_sources(
            sender=args.sender,
            sender_name=args.sender_name,
            kind=args.kind,
            destination=args.destination,
            source_paths=args.source,
            subject=args.subject,
            body=args.body,
            actor="cli",
        )
        detail = service.task_detail(str(result["id"]))
    if args.json:
        _emit_json(detail)
    else:
        _task_text(detail)
    return 0


def cmd_preview_tickets(args: argparse.Namespace) -> int:
    """Offline multi-ticket split preview: local .eml -> JSON preview.

    Deliberately bypasses AppService: no config/.env load, no database, no
    task/outbox/cursor writes, no network, no template generation.
    """
    from .preview import PreviewError, preview_eml

    eml_path = Path(args.eml)
    try:
        preview = preview_eml(eml_path, extra_sources=args.source or ())
    except PreviewError as exc:
        print(f"preview error: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(preview, ensure_ascii=False, indent=2, default=_json_default)
    if args.output:
        out = Path(args.output)
        resolved = out.resolve()
        forbidden = {eml_path.resolve()} | {
            Path(item).resolve() for item in args.source or ()
        }
        if resolved in forbidden:
            print("preview error: --output must not overwrite an input file",
                  file=sys.stderr)
            return 1
        if out.exists() and not args.overwrite:
            print(f"preview error: {out} exists (pass --overwrite to replace it)",
                  file=sys.stderr)
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"preview written: {out} "
              f"(candidates={preview['candidate_count']} "
              f"batch_ready={preview['batch_ready']})")
        return 0
    print(text)
    return 0


def cmd_import_batch(args: argparse.Namespace) -> int:
    """显式离线导入本地 .eml 为多票批次（写库；不联网、不收发邮件）。

    与 preview-tickets 不同：本命令持久化批次/方案/材料，供 Web 人工
    确认后建票；重复导入同一邮件只返回既有批次。
    """
    with _service(args) as service:
        result = service.batches.import_eml_file(args.eml, actor="cli")
    batch = result["batch"]
    plan = result.get("plan")
    payload = {
        "batch_id": batch["id"],
        "created": result["created"],
        "status": batch["status"],
        "owner": batch["owner"],
        "subject": batch["subject"],
        "plan_version": plan["version"] if plan else None,
        "batch_ready": bool(plan["batch_ready"]) if plan else None,
    }
    if result["created"] and result.get("preview"):
        payload["candidate_count"] = result["preview"]["candidate_count"]
    if args.json:
        _emit_json(payload)
    else:
        verb = "已导入新批次" if result["created"] else "邮件已存在，返回既有批次"
        print(f"{verb}: {batch['id']} (owner={batch['owner']})")
        if plan:
            print(f"方案 v{plan['version']}  "
                  f"batch_ready={bool(plan['batch_ready'])}")
    return 0


def cmd_batches(args: argparse.Namespace) -> int:
    with _service(args) as service:
        rows = service.batches.list_batches(limit=args.limit)
    if args.json:
        _emit_json(rows)
    else:
        _print_table(
            ["批次号", "状态", "发起人", "主题", "方案数", "任务数", "导入时间"],
            (
                [
                    item["id"],
                    item["status"],
                    item["owner"],
                    _clean_cell(item.get("subject"), 30),
                    item["plan_count"],
                    item["task_count"],
                    item["created_at"],
                ]
                for item in rows
            ),
        )
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    with _service(args) as service:
        try:
            detail = service.batches.batch_detail(
                args.batch_id, plan_id=args.plan)
        except KeyError:
            print(f"Batch not found: {args.batch_id}", file=sys.stderr)
            return 1
    if args.json:
        _emit_json(detail)
        return 0
    batch = detail["batch"]
    plan = detail["plan"]
    preview = detail["preview"] or {}
    print(f"批次: {batch['id']}  状态: {batch['status']}  "
          f"发起人: {batch['owner']}")
    print(f"主题: {batch.get('subject') or '-'}  邮件哈希: {batch['mail_hash'][:16]}…")
    if plan:
        print(f"当前方案: v{plan['version']}  "
              f"指纹: {str(plan['source_fingerprint'])[:16]}…  "
              f"batch_ready={bool(plan['batch_ready'])}")
    print(f"\n候选 ({len(preview.get('candidates', []))}):")
    for cand in preview.get("candidates", []):
        identity = cand.get("identity", {})
        label = (identity.get("invoice_no") or identity.get("mso")
                 or [cand["candidate_id"][:12]])
        print(f"- {cand['candidate_id']} [{cand.get('kind')}] {label}  "
              f"needs_confirmation={cand['needs_confirmation']}")
    if preview.get("unmatched_mail_rows"):
        print(f"\n未归属正文行: {len(preview['unmatched_mail_rows'])}")
    if preview.get("unmatched_sources"):
        print(f"未归属附件: {len(preview['unmatched_sources'])}")
    blocking = [i for i in preview.get("issues", []) if i.get("blocking")]
    if blocking:
        print(f"\n阻断问题 ({len(blocking)}):")
        for issue in blocking:
            print(f"- [{issue.get('code')}] {issue.get('message')}")
    if detail["tasks"]:
        print(f"\n已建任务 ({len(detail['tasks'])}):")
        for item in detail["tasks"]:
            print(f"- {item['task_id']} <- {item['candidate_key']}  "
                  f"hold={item['execution_hold']}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    with _service(args) as service:
        try:
            detail = service.batches.batch_detail(
                args.batch_id, plan_id=args.plan)
        except KeyError:
            print(f"Batch not found: {args.batch_id}", file=sys.stderr)
            return 1
    if args.json:
        _emit_json(detail)
        return 0
    batch = detail["batch"]
    plan = detail["plan"]
    preview = detail["preview"] or {}
    print(f"批次: {batch['id']}  状态: {batch['status']}  "
          f"发起人: {batch['owner']}")
    print(f"主题: {batch.get('subject') or '-'}  邮件哈希: {batch['mail_hash'][:16]}…")
    if plan:
        print(f"当前方案: v{plan['version']}  "
              f"指纹: {str(plan['source_fingerprint'])[:16]}…  "
              f"batch_ready={bool(plan['batch_ready'])}")
    print(f"\n候选 ({len(preview.get('candidates', []))}):")
    for cand in preview.get("candidates", []):
        identity = cand.get("identity", {})
        label = (identity.get("invoice_no") or identity.get("mso")
                 or [cand["candidate_id"][:12]])
        print(f"- {cand['candidate_id']} [{cand.get('kind')}] {label}  "
              f"needs_confirmation={cand['needs_confirmation']}")
    if preview.get("unmatched_mail_rows"):
        print(f"\n未归属正文行: {len(preview['unmatched_mail_rows'])}")
    if preview.get("unmatched_sources"):
        print(f"未归属附件: {len(preview['unmatched_sources'])}")
    blocking = [i for i in preview.get("issues", []) if i.get("blocking")]
    if blocking:
        print(f"\n阻断问题 ({len(blocking)}):")
        for issue in blocking:
            print(f"- [{issue.get('code')}] {issue.get('message')}")
    if detail["tasks"]:
        print(f"\n已建任务 ({len(detail['tasks'])}):")
        for item in detail["tasks"]:
            print(f"- {item['task_id']} <- {item['candidate_key']}  "
                  f"hold={item['execution_hold']}")
    return 0


def cmd_run_batch_offline(args: argparse.Namespace) -> int:
    """已确认批次子任务的显式离线执行（阶段 3）。

    刻意不走 _service()/AppService：最小离线配置（不读 .env/凭据），
    不实例化网关/轮询/交付；只读任务/批次/参考状态，不写正式状态、
    不释放 execution_hold、不建 outbox、不写生产 outputs。
    """
    from .batch_offline import BatchOfflineRunner, load_offline_config

    config = load_offline_config(args.config, args.runtime)
    runner = BatchOfflineRunner(config)
    try:
        try:
            report = runner.run_task(
                args.task_id, args.output_dir, actor="cli-offline")
        except KeyError:
            print(f"任务不存在或不属于已确认批次任务: {args.task_id}",
                  file=sys.stderr)
            return 1
    finally:
        runner.close()
    if args.json:
        _emit_json(report)
    else:
        print(f"任务: {report['task_id']}  状态: {report['status']}")
        print(f"批次: {report.get('batch_id')}  候选: {report.get('candidate_key')}")
        print(f"快照哈希: {str(report['snapshot']['trusted_hash'])[:16]}… "
              f"(复核 {'一致' if report['snapshot']['verified_hash'] == report['snapshot']['trusted_hash'] else '不一致'})")
        print(f"参考版本: {report['references']['version']}  "
              f"参考哈希: {report['references']['sha256'][:16]}…")
        if report.get("error"):
            print(f"错误: {report['error']}")
        for artifact in report.get("artifacts", []):
            print(f"- 产物: {artifact['filename']}  "
                  f"{artifact['size_bytes']} bytes  {artifact['sha256'][:16]}…")
        blocking = [i for i in report.get("issues", []) if i.get("blocking")]
        if blocking:
            print(f"\n阻断问题 ({len(blocking)}):")
            for issue in blocking:
                print(f"- [{issue.get('code')}] {issue.get('message')}")
        print(f"\n报告: {Path(report['output_dir']) / 'offline_report.json'}")
        hold = report.get("task_state_after", {}).get("execution_hold")
        print(f"execution_hold 保持: {hold}  正式状态未变: "
              f"{report.get('state_unchanged')}")
    return 0 if report["status"] in ("ready", "waiting_input", "unsupported") else 1


def cmd_retry(args: argparse.Namespace) -> int:
    with _service(args) as service:
        task = service.retry_task(args.task_id, actor="cli")
    print(f"{task['id']} -> {task['status']}")
    return 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    with _service(args) as service:
        task = service.reprocess_task(
            args.task_id, actor="cli", regenerate_only=args.regenerate_only
        )
    print(f"{task['id']} -> {task['status']} (v{task['version']})")
    return 0


def cmd_confirm_reprocess(args: argparse.Namespace) -> int:
    with _service(args) as service:
        task = service.confirm_reprocess(args.task_id, actor="cli")
    print(f"{task['id']} -> {task['status']}")
    return 0


def cmd_confirm_layout(args: argparse.Namespace) -> int:
    with _service(args) as service:
        result = service.confirm_layout(
            args.task_id, actor="cli", version=args.version)
    print(f"{result['kind']} layout {result['fingerprint']} confirmed "
          f"(task {args.task_id} v{result['version']})")
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    with _service(args) as service:
        task = service.deliver_task(args.task_id, actor="cli")
    print(f"{task['id']} -> {task['status']}")
    return 0


def cmd_outbox_action(args: argparse.Namespace) -> int:
    with _service(args) as service:
        try:
            if args.action == "release":
                result = service.release_dry_run(args.outbox_id, actor="cli")
            elif args.action == "recheck":
                found = service.recheck_unknown(args.outbox_id, actor="cli")
                print("found_in_sent" if found else "not_found_in_sent")
                return 0
            elif args.action == "confirm-sent":
                result = service.confirm_unknown_sent(args.outbox_id, actor="cli")
            elif args.action == "confirm-not-sent":
                result = service.confirm_unknown_not_sent(args.outbox_id, actor="cli")
            elif args.action == "retry":
                result = service.delivery.retry_failed(args.outbox_id, actor="cli")
            else:
                raise ValidationError(args.action)
        except KeyError:
            print(f"Outbox message not found: {args.outbox_id}", file=sys.stderr)
            return 1
    print(f"{result['id']} -> {result['status']}")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    with _service(args) as service:
        changes: dict[str, Any] = {}
        if args.mode is not None:
            changes["allowlist.mode"] = args.mode
        if args.test_email:
            changes["allowlist.test"] = args.test_email
        if args.production_email:
            changes["allowlist.production"] = args.production_email
        if args.dry_run is not None:
            changes["sending.dry_run"] = args.dry_run
        if args.enabled is not None:
            changes["sending.enabled"] = args.enabled
        if args.poll_interval is not None:
            changes["scan.poll_interval_seconds"] = args.poll_interval
        if changes:
            effective = service.update_settings(
                changes,
                actor="cli",
                reason=args.reason or "cli",
                production_ack=args.production_ack,
            )
        else:
            effective = service.settings.current().as_dict()
    if args.json:
        _emit_json(effective)
    else:
        _print_mapping(effective)
    return 0


def cmd_add_country_alias(args: argparse.Namespace) -> int:
    with _service(args) as service:
        result = service.references.add_alias(
            args.alias, args.alpha3, actor="cli", reason=args.reason or "cli"
        )
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    return 0


def cmd_import_machines(args: argparse.Namespace) -> int:
    with _service(args) as service:
        if args.confirm:
            result = service.confirm_machine_reference(args.confirm, actor="cli")
            print(json.dumps(result, ensure_ascii=False, default=_json_default))
            return 0
        if args.preview:
            preview = service.preview_machine_reference(args.path, actor="cli")
            print(json.dumps(preview, ensure_ascii=False, default=_json_default))
            return 0
        result = service.import_machine_reference(args.path, actor="cli")
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    return 0


def cmd_task_lifecycle(args: argparse.Namespace) -> int:
    with _service(args) as service:
        action = args.lifecycle_command
        if action == "archive":
            task = service.archive_task(args.task_id, actor="cli", reason=args.reason)
        elif action == "unarchive":
            task = service.unarchive_task(args.task_id, actor="cli")
        elif action == "delete":
            task = service.delete_task(args.task_id, actor="cli", reason=args.reason)
        elif action == "restore":
            task = service.restore_task(args.task_id, actor="cli")
        else:
            raise ValidationError(action)
    print(f"{task['id']} -> {task['status']}")
    return 0


def cmd_purge_files(args: argparse.Namespace) -> int:
    with _service(args) as service:
        if not args.yes:
            preview = service.purge_task_files_preview(args.task_id)
            _emit_json(preview)
            print("Add --yes to permanently delete the listed files.", file=sys.stderr)
            return 1
        result = service.purge_task_files(args.task_id, actor="cli")
    _emit_json(result)
    return 0


def cmd_clear_test(args: argparse.Namespace) -> int:
    with _service(args) as service:
        if not args.yes:
            _emit_json(service.clear_test_data_preview())
            print("Add --yes to clear test data after reviewing the preview.", file=sys.stderr)
            return 1
        result = service.clear_test_data(actor="cli", confirm=True)
    _emit_json(result)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    with _service(args) as service:
        path = service.maintenance.backup(args.reason)
        verification = service.maintenance.verify_backup(path)
    print(
        json.dumps(
            {"backup": str(path), "verification": verification},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_verify_backup(args: argparse.Namespace) -> int:
    with _service(args) as service:
        result = service.maintenance.verify_backup(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_restore(args: argparse.Namespace) -> int:
    with _service(args) as service:
        result = service.maintenance.restore(args.path, confirm=args.yes)
        recovery = service.recover()
    print(
        json.dumps(
            {"restore": result, "recovery": recovery}, ensure_ascii=False, indent=2
        )
    )
    if not args.yes:
        return 1
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    with _service(args) as service:
        payload: dict[str, Any] = {}
        if args.logs or not args.tasks:
            payload["logs_removed"] = [str(path) for path in service.maintenance.cleanup_logs()]
        if args.tasks or not args.logs:
            payload["tasks"] = service.maintenance.cleanup_task_files()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def cmd_diagnostics(args: argparse.Namespace) -> int:
    with _service(args) as service:
        path = service.maintenance.export_diagnostics()
    print(str(path))
    return 0


def cmd_credentials(args: argparse.Namespace) -> int:
    provider = CredentialProvider()
    if args.credential_command == "status":
        payload = provider.describe(args.account or "")
        if args.json:
            _emit_json(payload)
        else:
            _print_mapping(payload)
        return 0
    if args.credential_command == "set":
        if not args.service:
            raise ValidationError("--service imap|smtp is required")
        username = args.username or ""
        secret = getpass.getpass(f"{args.service.upper()} password (input hidden): ")
        if not secret:
            raise ValidationError("Empty password was not stored")
        CredentialProvider.store_windows(args.service, username, secret)
        print(f"Stored {args.service} credential for {username or '(account)'}")
        return 0
    raise ValidationError("Use 'credentials status' or 'credentials set'.")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _add_globals(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--config",
        default=default,
        help="Path to config.toml (default: config/config.toml or CATER_CONFIG)",
    )
    parser.add_argument(
        "--runtime",
        default=default,
        help="Runtime directory (default: runtime or CATER_RUNTIME_DIR)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json",
        default=argparse.SUPPRESS if suppress else False,
        help="Emit JSON instead of tables",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cater-app",
        description="CATER local mail-driven export document application",
    )
    _add_globals(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        child = subparsers.add_parser(name, help=help_text)
        _add_globals(child, suppress=True)
        child.set_defaults(handler=handler)
        return child

    command("version", cmd_version, "Print the application version")
    command("init", cmd_init, "Create runtime directories, database and reference seed")
    command("health", cmd_health, "Show database, task and credential-presence health")

    p = command("tasks", cmd_tasks, "List tasks")
    p.add_argument("--status", choices=sorted(state.ALL_STATUSES))
    p.add_argument("--kind", choices=["parts", "machine", "auto"])
    p.add_argument("--search")
    p.add_argument("--limit", type=int, default=100)

    p = command("task", cmd_task, "Show task detail by task id")
    p.add_argument("task_id")
    p.add_argument("--version", type=int)

    p = command("outbox", cmd_outbox, "List outbox messages")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=100)

    p = command("logs", cmd_logs, "Search redacted JSONL logs")
    p.add_argument("--task-id", dest="task_id")
    p.add_argument("--mail-id", dest="mail_id")
    p.add_argument("--level")
    p.add_argument("--event")
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=200)

    p = command("poll", cmd_poll, "Scan the mailbox and dispatch due outbox rows")
    p.add_argument("--once", action="store_true", help="Run one cycle (default)")
    p.add_argument("--loop", action="store_true", help="Run continuously")
    p.add_argument("--history", action="store_true", help="Process existing history on the first cycle")
    p.add_argument("--interval", type=int)
    p.add_argument("--max-cycles", type=int)

    p = command("serve", cmd_serve, "Run the local admin web UI")
    p.add_argument("--no-poll", action="store_true", help="Do not start the mailbox poller")
    p.add_argument("--host")
    p.add_argument("--port", type=int)

    p = command("process", cmd_process, "Process an .eml file or create an offline task")
    p.add_argument("--eml", action="append", default=[], help="Read an .eml file (repeatable)")
    p.add_argument("--sender", help="Offline task sender (must be allowlisted)")
    p.add_argument("--sender-name")
    p.add_argument("--kind", default="auto", choices=["auto", "parts", "machine"])
    p.add_argument("--destination")
    p.add_argument("--subject")
    p.add_argument("--body", default="")
    p.add_argument("--source", action="append", default=[], help="xlsx/msg/eml input (repeatable)")

    p = command("preview-tickets", cmd_preview_tickets,
                "Preview multi-ticket split from a local .eml (offline, no DB)")
    p.add_argument("--eml", required=True, help="Local .eml file to inspect")
    p.add_argument("--source", action="append", default=[],
                   help="Extra workbook path to include (repeatable)")
    p.add_argument("--output", help="Also write the preview JSON to this file")
    p.add_argument("--overwrite", action="store_true",
                   help="Allow replacing an existing --output file")

    p = command("import-batch", cmd_import_batch,
                "Import a local .eml as a persisted multi-ticket batch (offline)")
    p.add_argument("--eml", required=True, help="Local .eml file to import")

    p = command("batches", cmd_batches, "List multi-ticket batches")
    p.add_argument("--limit", type=int, default=100)

    p = command("batch", cmd_batch, "Show multi-ticket batch detail")
    p.add_argument("batch_id")
    p.add_argument("--plan", help="Show a specific plan version id")

    p = command("run-batch-offline", cmd_run_batch_offline,
                "Run one confirmed batch task offline (explicit, isolated output)")
    p.add_argument("task_id")
    p.add_argument("--output-dir", required=True,
                   help="New empty directory for the isolated outputs (never overwritten)")

    p = command("retry", cmd_retry, "Retry a RETRY_PENDING or FAILED task")
    p.add_argument("task_id")

    p = command("reprocess", cmd_reprocess, "Create a new task version and reprocess")
    p.add_argument("task_id")
    p.add_argument("--regenerate-only", action="store_true")

    p = command("confirm-reprocess", cmd_confirm_reprocess, "Confirm a required reprocess")
    p.add_argument("task_id")

    p = command("confirm-layout", cmd_confirm_layout,
                "Confirm the layout used by a task version as the baseline")
    p.add_argument("task_id")
    p.add_argument("--version", type=int, default=None)

    p = command("deliver", cmd_deliver, "Create and dispatch delivery for a READY_TO_SEND task")
    p.add_argument("task_id")

    p = command("outbox-action", cmd_outbox_action, "Release/recheck/confirm/retry an outbox row")
    p.add_argument("outbox_id")
    p.add_argument(
        "action",
        choices=["release", "recheck", "confirm-sent", "confirm-not-sent", "retry"],
    )

    p = command("settings", cmd_settings, "Show or update audited runtime settings")
    p.add_argument("--mode", choices=["test", "production"])
    p.add_argument("--test-email", action="append")
    p.add_argument("--production-email", action="append")
    p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--enable", action=argparse.BooleanOptionalAction, default=None, dest="enabled")
    p.add_argument("--poll-interval", type=int, dest="poll_interval")
    p.add_argument("--production-ack", action="store_true")
    p.add_argument("--reason")

    p = command("add-country-alias", cmd_add_country_alias, "Add an explicit country alias")
    p.add_argument("alias")
    p.add_argument("alpha3")
    p.add_argument("--reason")

    p = command("import-machines", cmd_import_machines, "Import Sheet1 machine references via core")
    p.add_argument("path", nargs="?", help="xlsx path (for direct import or --preview)")
    p.add_argument("--preview", action="store_true", help="Preview the import without writing")
    p.add_argument("--confirm", metavar="TOKEN", help="Confirm a previewed import by token")

    p = command("task-lifecycle", cmd_task_lifecycle, "Archive/delete/restore a task")
    lifecycle_sub = p.add_subparsers(dest="lifecycle_command", required=True)
    for action_name in ["archive", "unarchive", "delete", "restore"]:
        sub = lifecycle_sub.add_parser(action_name)
        _add_globals(sub, suppress=True)
        sub.add_argument("task_id")
        sub.add_argument("--reason")
        sub.set_defaults(handler=cmd_task_lifecycle)

    p = command("purge-files", cmd_purge_files, "Permanently purge files of a trashed terminal task")
    p.add_argument("task_id")
    p.add_argument("--yes", action="store_true")

    p = command("clear-test", cmd_clear_test, "Clear test task data (test identity required)")
    p.add_argument("--yes", action="store_true")

    p = command("backup", cmd_backup, "Create and verify a consistent SQLite backup")
    p.add_argument("--reason", default="manual")

    p = command("verify-backup", cmd_verify_backup, "Verify a backup file")
    p.add_argument("path")

    p = command("restore", cmd_restore, "Restore a verified backup (requires --yes)")
    p.add_argument("path")
    p.add_argument("--yes", action="store_true")

    p = command("cleanup", cmd_cleanup, "Apply retention to logs and terminal task files")
    p.add_argument("--logs", action="store_true")
    p.add_argument("--tasks", action="store_true")

    command("diagnostics", cmd_diagnostics, "Export a redacted diagnostics zip")

    p = command("credentials", cmd_credentials, "Show or store mail credentials")
    credential_sub = p.add_subparsers(dest="credential_command", required=True)
    status = credential_sub.add_parser("status", help="Show credential presence (booleans)")
    _add_globals(status, suppress=True)
    status.add_argument("--account")
    status.set_defaults(handler=cmd_credentials)
    store = credential_sub.add_parser("set", help="Store a credential in Windows Credential Manager")
    _add_globals(store, suppress=True)
    store.add_argument("--service", choices=["imap", "smtp"], required=True)
    store.add_argument("--username")
    store.set_defaults(handler=cmd_credentials)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except CoreUnavailable as exc:
        print(f"core unavailable: {exc}", file=sys.stderr)
        return 3
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except CaterError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"file not found: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"invalid value: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
