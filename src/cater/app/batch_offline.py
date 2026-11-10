"""已确认批次子任务的显式离线执行（阶段 3）。

本模块提供 ``BatchOfflineRunner``：对**已人工确认并物化**的多票批次
子任务，基于经可信哈希复核的独立快照做显式本地转换。安全边界：

- 先经 ``BatchService.verify_batch_task_snapshot(task_id)`` 复核（必须
  ``ok is True``），再读取该快照；**实际消费的 payload 还必须以与
  verify 相同的规范化口径重算哈希并绑定数据库持久化 trusted_hash**
  （verify 之后文件被替换的 TOCTOU 在创建输出目录前即拒绝）；篡改/
  漂移/越界一律拒绝，绝不读取父邮件 EML、批次原件目录或别票材料；
- 请求由 ``confirmed_input.build_confirmed_request``（阶段 3 公共契约
  A）生成；references 只来自既有参考服务，目的国只来自该任务自身，
  缺失保持 ``waiting_input``，绝不猜测或编造；
- 只读任务/批次/参考数据库行：**不**改变正式任务状态/版本/next_retry/
  confirmation，**不**释放 execution_hold，**不**建 outbox，**不**写
  生产 outputs（runtime/tasks 下任何位置）；**不**补种/改写参考数据
  （不调用 ensure_seeded；空参考库按现状读取，核心如实 waiting_input），
  数据库文件不存在时直接拒绝而绝不初始化新库；
- 输出目录每次独占创建（``exist_ok=False``）：拒绝覆盖输入、快照与
  既有输出；并发同路径只有一方成功；失败如实写失败报告，绝不把旧
  文件展示为成功，也绝不删除既有目录；
- 不实例化网关/凭据/轮询/交付等任何会收发邮件的自动流程；
  ``load_offline_config`` 只读 config.toml 的 runtime.dir 与
  core.header_aliases，绝不读取 .env / OS 环境变量 / 钥匙串。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tomllib
from pathlib import Path
from typing import Any, Callable

from .batches import BatchService
from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_RUNTIME_DIR,
    AppConfig,
    CoreConfig,
    PathsConfig,
    ensure_runtime_dirs,
    validate_config,
)
from .db import Database
from .engine import CoreProcessEngine
from .errors import ConfigurationError, ValidationError
from .logging import JsonlLogger
from .mail_store import MailStore
from .reference import ReferenceService
from .response import validate_core_response
from .tasks import TaskRepository
from .util import utc_now

REPORT_NAME = "offline_report.json"
REPORT_SCHEMA = 1


def load_offline_config(
    path: str | Path | None = None,
    runtime_dir: str | Path | None = None,
) -> AppConfig:
    """离线执行专用最小配置：只读 config.toml 的 runtime.dir 与
    core.header_aliases，绝不读取 .env / OS 环境变量 / 凭据。

    发送相关全部为出厂离线默认（dry_run、禁用、空白名单、无邮件账户），
    因此据此构建的服务在结构上不可能收发邮件。
    """
    chosen_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if chosen_path.exists():
        try:
            with chosen_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Cannot read configuration: {exc}") from exc
    elif path is not None:
        raise ConfigurationError(f"Configuration file not found: {chosen_path}")
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a table")

    runtime_raw = raw.get("runtime", {})
    if not isinstance(runtime_raw, dict):
        raise ConfigurationError("runtime must be a table")
    if runtime_dir is not None:
        chosen_runtime = Path(runtime_dir)
    else:
        chosen_runtime = Path(str(runtime_raw.get("dir") or DEFAULT_RUNTIME_DIR))

    core_raw = raw.get("core", {})
    if not isinstance(core_raw, dict):
        raise ConfigurationError("core must be a table")
    header_aliases: dict[str, list[str]] = {}
    aliases_raw = core_raw.get("header_aliases", {})
    if aliases_raw:
        if not isinstance(aliases_raw, dict):
            raise ConfigurationError("core.header_aliases must be a table")
        for field_name, names in aliases_raw.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ConfigurationError("core.header_aliases keys must be field names")
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ConfigurationError(
                    f"core.header_aliases.{field_name} must be a list of strings")
            header_aliases[field_name.strip()] = [n.strip() for n in names if n.strip()]

    config = AppConfig(
        paths=PathsConfig(
            runtime_dir=chosen_runtime,
            config_path=chosen_path if chosen_path.exists() else None,
        ),
        core=CoreConfig(header_aliases=header_aliases),
    )
    validate_config(config)
    return config


class BatchOfflineRunner:
    """已确认批次子任务的离线执行器（最小服务构建，只读正式状态）。

    仅组合 Database / BatchService / ReferenceService / TaskRepository /
    MailStore 与核心 ``process_job``；不创建网关、凭据提供器、轮询器或
    交付服务，结构上不可能收发邮件。除 JSONL 运行日志与显式隔离输出目
    录外不写任何文件，不改任何数据库行（不补种参考数据；数据库文件不
    存在时拒绝构造，绝不初始化新库）。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        logger: JsonlLogger | None = None,
        process_job: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        db_path = Path(config.db_path)
        if not db_path.is_file():
            raise ConfigurationError(
                f"数据库不存在（{db_path}）：离线执行绝不初始化新库；"
                "请先启动一次应用完成初始化后再执行")
        ensure_runtime_dirs(config)
        self.config = config
        self.logger = logger or JsonlLogger(
            config.logs_dir,
            max_bytes=config.logging.max_bytes,
            retention_days=config.logging.retention_days,
        )
        self.db = Database(config.db_path)
        self.references = ReferenceService(
            self.db, self.logger, tasks_dir=config.tasks_dir
        )
        # 绝不调用 ensure_seeded：离线执行不写参考数据。参考库按现状读取，
        # 为空时核心如实 waiting_input，绝不猜测或自动补种。
        self.tasks = TaskRepository(self.db, self.logger)
        self.mail_store = MailStore(self.db, config)
        self.batches = BatchService(
            self.db, config, self.logger, self.tasks, self.mail_store
        )
        self._process_job = process_job

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _engine_process_job(self) -> Callable[[dict[str, Any]], dict[str, Any]]:
        if self._process_job is not None:
            return self._process_job
        engine = CoreProcessEngine()
        return engine.process_job

    def _prepare_output_dir(self, output_dir: str | Path,
                            snapshot_dir: Path) -> Path:
        """校验并独占创建隔离输出目录；任何冲突即拒绝，绝不覆盖/删除。"""
        raw = Path(str(output_dir)).expanduser()
        if not str(raw).strip() or "\x00" in str(raw):
            raise ValidationError("输出目录路径非法")
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        resolved = raw.resolve(strict=False)

        snap = snapshot_dir.resolve()
        if resolved == snap or snap in resolved.parents or resolved in snap.parents:
            raise ValidationError(
                "输出目录与已确认快照目录重叠，已拒绝（绝不改写快照）")
        forbidden = (
            self.config.tasks_dir,
            self.config.mail_dir,
            self.config.logs_dir,
            Path(self.config.paths.runtime_dir) / "batches",
        )
        for root in forbidden:
            root_resolved = root.resolve()
            if resolved == root_resolved or root_resolved in resolved.parents:
                raise ValidationError(
                    f"输出目录位于受保护区域（{root.name}）内，已拒绝；"
                    "离线执行不写生产 outputs/批次材料/原始邮件/日志目录")
        if resolved.exists() or resolved.is_symlink():
            raise ValidationError(
                f"输出目录已存在（{resolved}），拒绝覆盖既有内容；"
                "请指定一个新的空目录")
        try:
            os.makedirs(resolved, exist_ok=False)
        except FileExistsError:
            # 并发同路径：只有独占创建成功的一方继续，另一方明确拒绝，
            # 两票/两次执行绝不混写同一目录。
            raise ValidationError(
                f"输出目录 {resolved} 已被并发创建，本次已拒绝（不会混写）"
            ) from None
        except OSError as exc:
            raise ValidationError(
                f"无法创建输出目录 {resolved}：{type(exc).__name__}: {exc}"
            ) from exc
        return resolved

    @staticmethod
    def _write_report(out_dir: Path, report: dict[str, Any]) -> Path:
        """原子写入执行报告（独占暂存 + replace；目录本次独占新建，报告
        必为本目录首份，不存在覆盖旧报告的可能）。"""
        target = out_dir / REPORT_NAME
        temporary = target.with_name(
            f"{secrets.token_hex(12)}.{REPORT_NAME}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(json.dumps(
                    report, ensure_ascii=False, indent=2, default=str
                ).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return target

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------
    def _destination_confirmation(
        self, batch_id: str, task_id: str
    ) -> dict[str, Any]:
        """通过 F 公共读取接口取得当前票的目的国确认来源（只读）。

        报告必须反映下一次离线运行实际采用的目的国及其最近确认审计，
        因此这里读取 ``BatchDestinationService`` 的当前值，而不是自行
        猜测或重放信任 client 提供的国家名/alpha3。
        """
        from .batch_destinations import BatchDestinationService

        data = BatchDestinationService(
            self.db, self.references, self.tasks, self.logger
        ).get_batch_destinations(batch_id)
        row = next(
            (item for item in data.get("rows") or []
             if str(item.get("task_id")) == task_id),
            None,
        )
        if row is None:
            raise ValidationError(
                "该票不在批次目的国确认列表中：拒绝离线执行，未创建任何输出")
        revision = row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValidationError(
                "目的国确认修订标识无效：拒绝离线执行，未创建任何输出")
        alpha3 = str(row.get("destination_alpha3") or "").strip() or None
        name = str(row.get("destination_name") or "").strip() or None
        if alpha3 and not name:
            name = alpha3
        return {
            "name": name,
            "alpha3": alpha3,
            "revision": revision,
            "confirmed_by": row.get("confirmed_by"),
            "confirmed_at": row.get("confirmed_at"),
        }

    def run_task(
        self,
        task_id: str,
        output_dir: str | Path,
        *,
        actor: str = "cli-offline",
    ) -> dict[str, Any]:
        """对一个已确认批次子任务执行离线转换并返回报告（同时落盘）。

        拒绝（ValidationError/KeyError）发生在创建任何输出之前；核心处理
        异常则如实写 ``failed`` 报告后返回。无论成败都不触碰任务状态、
        execution_hold、outbox 与生产 outputs。
        """
        started_at = utc_now()
        task_id = str(task_id).strip()
        if not task_id:
            raise ValidationError("task_id 缺失")
        task = self.tasks.require(task_id)  # KeyError：任务不存在

        # 1) 可信边界：必须属于已确认 batch_tasks 且快照复核 ok。
        link_row = self.db.fetchone(
            "SELECT * FROM batch_tasks WHERE task_id = ?", (task_id,))
        batch_link = dict(link_row) if link_row is not None else {}
        hold = self.tasks.batch_hold(task_id)
        verify = self.batches.verify_batch_task_snapshot(task_id)  # KeyError/篡改拒绝
        if not verify.get("ok"):
            raise ValidationError(
                "快照复核哈希与任务可信哈希不一致（疑似篡改/漂移），"
                "已拒绝离线执行")
        snapshot_dir = Path(str(verify["snapshot_path"])).resolve()
        snapshot_file = snapshot_dir / "snapshot.json"
        try:
            snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError(
                f"快照文件不可读或已损坏，已拒绝离线执行：{exc}") from exc

        # 可信绑定（防 verify→read 之间的 TOCTOU 替换）：实际将要传给
        # 构建器与核心的 payload，必须以与 verify 完全相同的规范化口径
        # （忽略时间戳等非业务元数据）重算哈希，并等于数据库持久化的
        # trusted_hash——不是再信 verify 布尔，也不是只比对两次读文件相等。
        # 不一致即拒绝，发生在创建任何输出目录之前；此后消费的即此内存
        # payload，构建器内的文件一致性/资产校验照常保留。
        trusted_hash = str(verify.get("trusted_hash") or "")
        payload_hash = hashlib.sha256(
            BatchService._canonical_snapshot(snapshot)).hexdigest()
        if not trusted_hash or payload_hash != trusted_hash:
            self.logger.warning(
                "batch_offline_payload_binding_failed", task_id=task_id,
                actor=actor)
            raise ValidationError(
                "快照内容与数据库可信哈希不一致（复核后被替换/漂移，疑似"
                "TOCTOU 篡改），已拒绝离线执行；未创建任何输出")

        batch_link = hold or {}
        # 目的国来源只取 F 公共读取结果（当前票确认审计 + 当前值）；缺失
        # 时如实为空，由核心按既有规则保持 waiting_input，绝不猜测。
        destination_confirmation = self._destination_confirmation(
            str(batch_link["batch_id"]), task_id)
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "task_id": task_id,
            "batch_id": batch_link.get("batch_id"),
            "plan_id": batch_link.get("plan_id"),
            "candidate_key": batch_link.get("candidate_key"),
            "actor": actor,
            "started_at": started_at,
            "destination": destination_confirmation,
            "snapshot": {
                "path": str(snapshot_file),
                "trusted_hash": verify.get("trusted_hash"),
                "verified_hash": verify.get("verified_hash"),
                "payload_hash": payload_hash,
            },
            "task_state_before": {
                "status": task.get("status"),
                "version": task.get("version"),
                "execution_hold": hold is not None,
            },
        }

        # 2) 目的国只取该票当前确认来源；references 只取既有参考服务。
        #    缺失不猜：destination=None 由核心按既有规则如实 waiting_input。
        destination = None
        if destination_confirmation["alpha3"]:
            destination = {
                "name": destination_confirmation["name"]
                or destination_confirmation["alpha3"],
                "alpha3": destination_confirmation["alpha3"],
            }
        references = self.references.core_references()
        report["references"] = {
            "version": self.references.reference_version(),
            "sha256": hashlib.sha256(json.dumps(
                references, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")).hexdigest(),
        }

        # 3) 由公共契约 A 生成核心请求（纯只读；含快照二次校验）。
        try:
            from .confirmed_input import build_confirmed_request
        except ImportError as exc:
            raise ValidationError(
                f"已确认请求构建组件不可用：{exc}") from exc
        out_dir = self._prepare_output_dir(output_dir, snapshot_dir)
        report["output_dir"] = str(out_dir)
        try:
            request = build_confirmed_request(
                snapshot,
                snapshot_path=snapshot_dir,
                task_id=task_id,
                destination=destination,
                references=references,
                output_dir=out_dir,
                header_aliases=self.config.core.header_aliases or None,
            )
        except ValidationError as exc:
            report["status"] = "failed"
            report["error"] = f"已确认请求构建被拒绝：{exc}"
            report["finished_at"] = utc_now()
            self._write_report(out_dir, report)
            self.logger.warning("batch_offline_refused", task_id=task_id,
                                actor=actor, error=str(exc))
            return report

        # 4) 核心转换（仅此一步会写业务产物，且只写本次独占目录）。
        try:
            response_raw = self._engine_process_job()(request)
            response = validate_core_response(response_raw, output_dir=out_dir)
            report["status"] = str(response["status"])
            report["kind"] = str(response["kind"])
            report["issues"] = response["issues"]
            report["records"] = len(response["records"])
            report["content_hash"] = response.get("content_hash")
            report["business_identity"] = response.get("business_identity")
            # validate_core_response 已核验产物路径边界与逐文件哈希。
            report["artifacts"] = [
                {
                    "filename": artifact["filename"],
                    "path": artifact["path"],
                    "sha256": artifact["sha256"],
                    "size_bytes": artifact["size_bytes"],
                    "kind": artifact.get("kind"),
                }
                for artifact in response["artifacts"]
            ]
        except Exception as exc:  # 技术失败：如实 failed，不伪造成功
            report["status"] = "failed"
            report["error"] = f"{type(exc).__name__}: {exc}"
            report.setdefault("artifacts", [])
            self.logger.exception("batch_offline_failed", exc,
                                  task_id=task_id, actor=actor)

        # 5) 收尾：复核正式状态未被本执行改变（断言式留证，不做任何写）。
        final_task = self.tasks.require(task_id)
        final_hold = self.tasks.batch_hold(task_id)
        report["task_state_after"] = {
            "status": final_task.get("status"),
            "version": final_task.get("version"),
            "execution_hold": final_hold is not None,
        }
        report["state_unchanged"] = (
            report["task_state_after"] == report["task_state_before"])
        report["finished_at"] = utc_now()
        self._write_report(out_dir, report)
        self.logger.info(
            "batch_offline_completed", task_id=task_id, actor=actor,
            status=report["status"],
            artifacts=len(report.get("artifacts", [])))
        return report
