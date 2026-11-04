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
        result = service.import_machine_reference(args.path, actor="cli")
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
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

    p = command("retry", cmd_retry, "Retry a RETRY_PENDING or FAILED task")
    p.add_argument("task_id")

    p = command("reprocess", cmd_reprocess, "Create a new task version and reprocess")
    p.add_argument("task_id")
    p.add_argument("--regenerate-only", action="store_true")

    p = command("confirm-reprocess", cmd_confirm_reprocess, "Confirm a required reprocess")
    p.add_argument("task_id")

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
    p.add_argument("path")

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