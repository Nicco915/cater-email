"""多票批次持久化、人工确认与幂等建票（阶段 2）。

链路：本地导入 .eml → 批次 + 不可变方案版本 → Web 人工归属/排除调整
（每次调整重跑核心识别，产生新方案版本）→ 显式确认所选方案并幂等创建
逐票任务。建出的任务带 execution_hold=1，本阶段任何执行/补件/发信入口
都必须拒绝（见 tasks.py 与 application.py 的拦截点）。

身份分层：
- 批次永久身份 = (account, owner, mail_hash)，重复/并发导入只建一批；
- source_fingerprint 含识别器/别名/调整状态，只标识一次识别结果，
  规则升级产生新方案版本，绝不产生新批次；
- 候选 candidate_key 为内容寻址ID，与批次ID/方案版本ID/任务ID互不等价。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
from contextlib import suppress as contextlib_suppress
from pathlib import Path
from typing import Any, Iterable

from .db import Database
from .engine import load_inspect_sources
from .errors import ValidationError
from .logging import JsonlLogger
from .mail.message import parse_mail
from .mail_store import MailStore
from .preview import (
    MAX_ATTACHMENT_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_SOURCE_COUNT,
    MAX_TOTAL_ATTACHMENT_BYTES,
)
from .tasks import TaskRepository
from .util import (
    new_id,
    normalize_email,
    safe_filename,
    sha256_bytes,
    sha256_file,
    stored_filename,
    utc_now,
)

# 建出的批次任务本阶段一律挂起的原因（用户可读）。
HOLD_REASON = "多票批次已确认建票；本阶段不开放执行（等待后续阶段释放）"

# 非业务内嵌/装饰性图片：默认作为审计材料，不强制人工排除。
_DECORATIVE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}


class _ConcurrentImport(Exception):
    """批次身份唯一冲突（UNIQUE(account, owner, mail_hash) 命中）：
    并发/重复导入，返回既有批次。任何其他唯一冲突都不得冒充此信号。"""


class _AlreadyMaterialized(Exception):
    """确认事务内发现批次已物化：由调用方做幂等比较。"""


_STAGING_LOCK = threading.Lock()
_STAGING_TMP_RE = re.compile(r"^[0-9a-f]{24}\.[0-9a-f]{6}\..*\.tmp$")

# 残留回收宽限：发布未满该窗口的未引用目录暂不报告（纯保守宽限）。
# 正确性不依赖 TTL：sweep 删除前会在同一可靠同步边界（批次确认锁 +
# BEGIN IMMEDIATE）内重新核验引用，已引用目录绝不删除。
_RESIDUE_TTL_SECONDS = 900


def _utc_timestamp() -> float:
    import time as _time
    return _time.time()


def _confirm_lock_for(batch_id: str) -> threading.Lock:
    """批次级确认锁：同一批次的确认/暂存清理互斥（线程内）。"""
    with _STAGING_LOCK:
        lock = _CONFIRM_LOCKS.get(batch_id)
        if lock is None:
            lock = threading.Lock()
            _CONFIRM_LOCKS[batch_id] = lock
        return lock


_CONFIRM_LOCKS: dict[str, threading.Lock] = {}


def _is_staging_tmp(path: Path) -> bool:
    return _STAGING_TMP_RE.match(path.name) is not None


def _canonical_candidate(candidate: dict[str, Any]) -> str:
    """候选业务内容规范化串：忽略仅展示/时间戳字段，业务字段全比对。"""
    canonical = {
        key: value for key, value in candidate.items()
        if key not in ("mail_row_refs",)
    }
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)


def _canonical_mail_rows(rows: Any) -> str:
    return json.dumps(rows or [], ensure_ascii=False, sort_keys=True, default=str)


def _write_file_atomic(path: Path, data: bytes) -> None:
    """独占唯一暂存 + fsync + os.replace 原子发布（0600）。

    暂存名带 96 位随机后缀，并发写同一目标文件互不覆盖；replace 失败即
    清理暂存，绝不留下半写文件或抢占他人 replace 的源。
    """
    temporary = path.with_name(
        f"{secrets.token_hex(12)}.{secrets.token_hex(3)}.{path.name}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _is_decorative_source(entry: dict[str, Any]) -> bool:
    """非装饰附件必须显式指派/排除；装饰性图片默认仅作审计材料。"""
    if entry.get("status") != "not_workbook":
        return False
    names = entry.get("filenames") or [entry.get("filename") or ""]
    return bool(names) and all(
        Path(str(name)).suffix.lower() in _DECORATIVE_SUFFIXES for name in names
    )


class BatchService:
    def __init__(
        self,
        db: Database,
        config: Any,
        logger: JsonlLogger,
        tasks: TaskRepository,
        mail_store: MailStore,
    ) -> None:
        self.db = db
        self.config = config
        self.logger = logger
        self.tasks = tasks
        self.mail_store = mail_store

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------
    def _root(self) -> Path:
        return Path(self.config.paths.runtime_dir) / "batches"

    def _assets_dir(self, batch_id: str) -> Path:
        return self._root() / batch_id / "assets"

    def _snapshots_dir(self, batch_id: str) -> Path:
        return self._root() / batch_id / "snapshots"

    def _publish_dir(
        self, batch_id: str, plan_id: str, candidate_key: str
    ) -> Path:
        """每次确认请求独占的不可变快照发布目录。

        路径按 ``snapshots/<plan_id>/<candidate_key>-<attempt_token>`` 生成：
        独占创建（exist_ok=False），任何其他方案/请求/重试都拿到不同目录，
        已发布且被 batch_tasks 引用的目录因此**永不**被覆盖或移除——迟到
        的过期请求（TOCTOU）根本触碰不到别人的已确认快照。确认失败或
        事务回滚留下的未引用目录由 sweep_orphan_snapshots 回收。
        """
        return (
            self._snapshots_dir(batch_id) / str(plan_id)
            / f"{candidate_key}-{secrets.token_hex(8)}"
        )

    def _confirm_lock(self, batch_id: str) -> threading.Lock:
        return _confirm_lock_for(str(batch_id))

    def _remove_batch_files(self, batch_id: str) -> None:
        """导入失败时的孤儿材料清理（仅限本批次自有目录，路径越界拒绝）。"""
        root = self._root().resolve()
        target = (root / batch_id).resolve()
        if root not in target.parents or not target.is_dir():
            return
        with contextlib_suppress(Exception):
            shutil.rmtree(target, ignore_errors=False)

    # ------------------------------------------------------------------
    # staging/verification helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _canonical_snapshot(payload: dict[str, Any]) -> bytes:
        """全内容规范化序列化：忽略时间戳等非业务元数据，业务内容任何
        差异都会改变哈希。先经 default=str 等值归一，保证写入与事后
        复核使用同一条规范化路径。"""
        strip_ts = ("created_at", "staged_at", "verified_at")
        normalized = json.loads(json.dumps(payload, ensure_ascii=False,
                                           default=str))
        canonical = {
            key: value for key, value in normalized.items()
            if key not in strip_ts
        }
        return json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")

    @staticmethod
    def _candidate_files(candidate: dict[str, Any]) -> list[dict[str, Any]]:
        """候选引用的附件清单（按 sha256 排序，确定性）。"""
        files = []
        for ref in candidate.get("source_refs", []):
            sha = str(ref.get("sha256"))
            filenames = ref.get("filenames") or [ref.get("filename") or ""]
            files.append({
                "sha256": sha,
                "filenames": sorted(str(f) for f in filenames),
            })
        files.sort(key=lambda f: f["sha256"])
        return files

    def _verify_staged_snapshot(
        self,
        batch_id: str,
        plan: dict[str, Any],
        candidate: dict[str, Any],
        *,
        base_dir: Path,
    ) -> str:
        """对落盘快照做完整业务校验（不依赖两个 ID），返回可信哈希。

        校验范围：schema/版本/指纹、候选全业务内容、正文行、附件清单
        及其逐文件哈希与路径边界。任何不符/篡改即拒绝，绝不用本地
        JSON 事实当权威。
        """
        key = str(candidate["candidate_id"])
        base = Path(base_dir).resolve()
        root = self._snapshots_dir(batch_id).resolve()
        if root not in base.parents:
            raise ValidationError(f"快照目录越界（{base_dir}），已拒绝建票")

        snapshot_path = base / "snapshot.json"
        try:
            payload = json.loads(
                snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError(
                f"快照 {snapshot_path} 不可读或已损坏，已拒绝建票：{exc}"
            ) from exc

        expected = {
            "schema": 1,
            "batch_id": str(batch_id),
            "plan_id": str(plan["id"]),
            "plan_version": int(plan["version"]),
            "source_fingerprint": str(plan["source_fingerprint"]),
        }
        problems: list[str] = []
        for field, want in expected.items():
            got = payload.get(field)
            if got != want:
                problems.append(f"{field}={got!r}（应为 {want!r}）")
        cand_payload = payload.get("candidate")
        if not isinstance(cand_payload, dict):
            problems.append("candidate 缺失或不是对象")
        else:
            if str(cand_payload.get("candidate_id")) != key:
                problems.append(
                    "candidate.candidate_id 与本票不一致："
                    f"{cand_payload.get('candidate_id')!r}")
            if _canonical_candidate(cand_payload) != \
                    _canonical_candidate(candidate):
                problems.append("candidate 业务内容与本方案不一致")
        if _canonical_mail_rows(payload.get("mail_rows")) != \
                _canonical_mail_rows(candidate.get("mail_row_refs", [])):
            problems.append("mail_rows 与本方案不一致")

        files_payload = payload.get("assets")
        if not isinstance(files_payload, list):
            problems.append("assets 缺失或不是列表")
        else:
            want_files = self._candidate_files(candidate)
            got_files = [
                {"sha256": str(f.get("sha256")),
                 "filenames": sorted(
                     str(n) for n in (f.get("filenames")
                                      or [f.get("filename") or ""]))}
                for f in files_payload
            ]
            got_files.sort(key=lambda f: f["sha256"])
            if got_files != want_files:
                problems.append("附件清单与本方案不一致")
            else:
                for entry in files_payload:
                    raw_path = str(entry.get("path") or "")
                    if not raw_path:
                        problems.append("附件缺少路径")
                        continue
                    entry_path = Path(raw_path)
                    entry_dir = entry_path.parent.resolve() if \
                        entry_path.is_absolute() else base
                    if entry_dir != base:
                        problems.append(f"附件路径越界：{entry_path}")
                        continue
                    if not entry_path.is_file() or \
                            sha256_file(entry_path) != str(entry["sha256"]):
                        problems.append(
                            f"附件哈希不符：{entry_path.name}（缺失或篡改）")
        if problems:
            raise ValidationError(
                f"快照 {snapshot_path.name} 校验失败：{'；'.join(problems)}")
        return sha256_bytes(self._canonical_snapshot(payload))

    # ------------------------------------------------------------------
    # repository helpers
    # ------------------------------------------------------------------
    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return _row_dict(
            self.db.fetchone("SELECT * FROM mail_batches WHERE id = ?", (batch_id,))
        )

    def require_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        return batch

    def list_batches(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT b.*,
                   (SELECT COUNT(*) FROM batch_tasks t WHERE t.batch_id = b.id) AS task_count,
                   (SELECT COUNT(*) FROM batch_plan_versions p WHERE p.batch_id = b.id) AS plan_count
              FROM mail_batches b
             ORDER BY b.created_at DESC
             LIMIT ? OFFSET ?
            """,
            (max(1, min(int(limit), 500)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        return _row_dict(
            self.db.fetchone(
                "SELECT * FROM batch_plan_versions WHERE id = ?", (plan_id,)
            )
        )

    def require_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    def plans(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM batch_plan_versions WHERE batch_id = ? ORDER BY version",
            (batch_id,),
        )
        return [dict(row) for row in rows]

    def candidates(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM batch_candidates WHERE plan_id = ? ORDER BY candidate_key",
            (plan_id,),
        )
        return [dict(row) for row in rows]

    def assets(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM batch_assets WHERE batch_id = ? ORDER BY role, filename",
            (batch_id,),
        )
        return [dict(row) for row in rows]

    def events(self, batch_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM batch_events WHERE batch_id = ? ORDER BY id DESC LIMIT ?",
            (batch_id, max(1, min(int(limit), 1000))),
        )
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "null")
            except json.JSONDecodeError:
                item["detail"] = item.pop("detail_json")
            result.append(item)
        return result

    def batch_tasks(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT bt.*, t.status AS task_status, t.kind AS task_kind
              FROM batch_tasks bt JOIN tasks t ON t.id = bt.task_id
             WHERE bt.batch_id = ? ORDER BY bt.candidate_key
            """,
            (batch_id,),
        )
        return [dict(row) for row in rows]

    def asset_for_download(self, batch_id: str, asset_id: str) -> dict[str, Any]:
        """下载校验：资产必须属于该批次且路径在批次目录内、哈希一致。"""
        row = _row_dict(
            self.db.fetchone(
                "SELECT * FROM batch_assets WHERE id = ? AND batch_id = ?",
                (asset_id, batch_id),
            )
        )
        if row is None:
            raise KeyError(asset_id)
        path = Path(str(row["path"])).resolve()
        root = self._assets_dir(batch_id).resolve()
        if root not in path.parents or not path.is_file():
            raise ValidationError("批次资产不可用（路径越界或文件缺失）")
        return {**row, "resolved_path": path}

    def batch_detail(
        self, batch_id: str, *, plan_id: str | None = None
    ) -> dict[str, Any]:
        batch = self.require_batch(batch_id)
        plans = self.plans(batch_id)
        current_id = plan_id or batch.get("current_plan_id")
        plan = next((p for p in plans if p["id"] == current_id), None)
        if plan is None and plans:
            plan = plans[-1]
        preview = json.loads(plan["preview_json"]) if plan else None
        candidate_rows = self.candidates(plan["id"]) if plan else []
        return {
            "batch": batch,
            "plans": plans,
            "plan": plan,
            "preview": preview,
            "assignments": json.loads(plan["assignments_json"]) if plan else [],
            "candidate_rows": candidate_rows,
            "assets": self.assets(batch_id),
            "events": self.events(batch_id),
            "tasks": self.batch_tasks(batch_id),
        }

    def _event_conn(
        self,
        conn,
        batch_id: str,
        plan_id: str | None,
        action: str,
        *,
        actor: str,
        detail: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO batch_events (batch_id, plan_id, action, actor, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                plan_id,
                action,
                actor,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                now or utc_now(),
            ),
        )

    # ------------------------------------------------------------------
    # inspect on stored, hash-verified materials
    # ------------------------------------------------------------------
    def _limit_issue(self, code: str, message: str, filename: str) -> dict:
        return {
            "code": code,
            "message": message,
            "record_key": None,
            "field": None,
            "sources": [{"type": "workbook", "filename": filename}],
            "required_action": "请压缩或拆分附件后重试",
            "blocking": True,
        }

    def _verified_attachment_assets(
        self, batch_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """读取批次附件资产并逐哈希校验；被篡改即拒绝，绝不静默重读新数据。"""
        attachment_assets = []
        raw_asset = None
        for asset in self.assets(batch_id):
            path = Path(str(asset["path"]))
            if not path.is_file() or sha256_file(path) != str(asset["sha256"]):
                raise ValidationError(
                    f"批次材料 {asset.get('filename')} 校验失败（缺失或被篡改），"
                    "已拒绝继续；请恢复原始材料后重试"
                )
            if asset["role"] == "raw_mail":
                raw_asset = asset
            elif asset["role"] == "attachment":
                attachment_assets.append(asset)
        if raw_asset is None:
            raise ValidationError("批次缺少邮件原件资产")
        return attachment_assets, raw_asset

    def _run_inspect(
        self,
        batch_id: str,
        *,
        assignments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在已持久化、已哈希校验的材料上重跑核心识别（纯计算，无写入）。"""
        attachment_assets, raw_asset = self._verified_attachment_assets(batch_id)
        raw = Path(str(raw_asset["path"])).read_bytes()
        parsed = parse_mail(raw, max_attachment_bytes=MAX_ATTACHMENT_BYTES)

        limit_issues: list[dict] = []
        sources: list[dict[str, str]] = []
        total = 0
        for asset in attachment_assets:
            filename = str(asset["filename"])
            size = int(asset["size_bytes"])
            if len(sources) >= MAX_SOURCE_COUNT:
                limit_issues.append(self._limit_issue(
                    "ATTACHMENT_COUNT_EXCEEDED",
                    f"附件数量超过 {MAX_SOURCE_COUNT} 上限，{filename} 未纳入识别",
                    filename))
                continue
            if size > MAX_ATTACHMENT_BYTES:
                limit_issues.append(self._limit_issue(
                    "ATTACHMENT_TOO_LARGE",
                    f"附件 {filename} 超过单附件上限，未纳入识别", filename))
                continue
            if total + size > MAX_TOTAL_ATTACHMENT_BYTES:
                limit_issues.append(self._limit_issue(
                    "ATTACHMENTS_TOTAL_TOO_LARGE",
                    f"附件累计超过上限，{filename} 未纳入识别", filename))
                continue
            total += size
            sources.append({"path": str(asset["path"]), "filename": filename})
        for warning in parsed.parse_warnings:
            if warning.startswith("attachment_too_large:"):
                name = warning.split(":", 1)[1]
                if not any(i["sources"][0]["filename"] == name for i in limit_issues):
                    limit_issues.append(self._limit_issue(
                        "ATTACHMENT_TOO_LARGE",
                        f"附件 {name} 超过单附件上限，未纳入识别", name))

        request: dict[str, Any] = {
            "sources": sources,
            "mail": {
                "html": parsed.html_body or "",
                "text": parsed.text_body or "",
                "subject": parsed.subject or "",
                "message_id": parsed.message_id,
            },
        }
        if assignments:
            request["assignments"] = assignments
        preview = load_inspect_sources()(request)
        if limit_issues:
            preview["issues"].extend(limit_issues)
            preview["batch_ready"] = False
        return preview

    # ------------------------------------------------------------------
    # import
    # ------------------------------------------------------------------
    def _next_offline_uid(self, account: str) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(MAX(uid), 0) FROM mail_messages "
            "WHERE account = ? AND folder = 'OFFLINE-BATCH'",
            (account,),
        )
        return int(value or 0) + 1

    def import_eml_file(self, path: str | Path, *, actor: str) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        size = source.stat().st_size
        if size > MAX_MESSAGE_BYTES:
            raise ValidationError(
                f"邮件超过 {MAX_MESSAGE_BYTES // (1024 * 1024)}MB 上限（{size} 字节）")
        return self._import(source.read_bytes(), filename=source.name, actor=actor)

    def import_eml_bytes(
        self, data: bytes, *, filename: str, actor: str
    ) -> dict[str, Any]:
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValidationError(
                f"邮件超过 {MAX_MESSAGE_BYTES // (1024 * 1024)}MB 上限（{len(data)} 字节）")
        if not data:
            raise ValidationError("上传内容为空")
        return self._import(data, filename=filename, actor=actor)

    def _import(self, raw: bytes, *, filename: str, actor: str) -> dict[str, Any]:
        mail_hash = sha256_bytes(raw)
        parsed = parse_mail(raw, max_attachment_bytes=MAX_ATTACHMENT_BYTES)
        owner = normalize_email(parsed.sender) or (parsed.sender or "").strip()
        if not owner:
            raise ValidationError("邮件缺少可识别的发件人，无法建立批次归属")
        account = self.config.mail.account or "offline@localhost"
        existing = _row_dict(
            self.db.fetchone(
                "SELECT * FROM mail_batches WHERE account = ? AND owner = ? AND mail_hash = ?",
                (account, owner, mail_hash),
            )
        )
        if existing is not None:
            self.logger.info(
                "batch_import_duplicate", batch_id=existing["id"], actor=actor)
            return self._dedupe_result(existing, actor=actor)

        batch_id = new_id("batch")
        assets_dir = self._assets_dir(batch_id)
        assets_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now()

        # 材料按内容哈希去重：同一附件以不同文件名重复出现只落盘/入库
        # 一份，原名并入 aliases 审计；绝不因附件唯一冲突误判重复导入。
        stored: list[dict[str, Any]] = []
        by_sha: dict[tuple[str, str], dict[str, Any]] = {}
        raw_name = safe_filename(filename, fallback="original.eml")
        raw_path = assets_dir / stored_filename(mail_hash, raw_name)
        _write_file_atomic(raw_path, raw)
        stored.append({
            "role": "raw_mail", "filename": raw_name, "aliases": [],
            "sha256": mail_hash, "size_bytes": len(raw),
            "path": str(raw_path),
        })
        by_sha[("raw_mail", mail_hash)] = stored[-1]
        for att in parsed.attachments:
            if att.is_inline:
                continue  # 内嵌图片等仅留原件审计，不作为独立材料
            target = assets_dir / stored_filename(att.sha256, att.filename)
            if not target.exists():
                _write_file_atomic(target, att.data)
            canonical = by_sha.get(("attachment", att.sha256))
            if canonical is None:
                canonical = {
                    "role": "attachment", "filename": att.filename,
                    "aliases": [], "sha256": att.sha256,
                    "size_bytes": att.size, "path": str(target),
                }
                by_sha[("attachment", att.sha256)] = canonical
                stored.append(canonical)
            alias = att.filename
            if alias != canonical["filename"] and alias not in canonical["aliases"]:
                canonical["aliases"].append(alias)

        # legacy mail_messages 行：批次任务 origin_mail_id 指向父邮件；
        # task_id 保持 NULL + filter_reason，多票归属只走 batch_tasks，
        # 同内容邮件之后到达生产轮询时按 duplicate_raw_content 过滤。
        saved_raw = self.mail_store.save_raw(raw, mail_hash)
        mail_row, _ = self.mail_store.record(
            account=account,
            folder="OFFLINE-BATCH",
            uid_validity=0,
            uid=self._next_offline_uid(account),
            raw_hash=mail_hash,
            parsed=parsed,
            raw_path=str(saved_raw),
            size_bytes=len(raw),
        )
        self.mail_store.set_task(
            str(mail_row["id"]), None, "multi_ticket_batch_import")

        try:
            try:
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO mail_batches (
                            id, mail_id, account, owner, owner_name, mail_hash,
                            subject, message_id, status, current_plan_id,
                            created_at, created_by
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?)
                        """,
                        (
                            batch_id, str(mail_row["id"]), account, owner,
                            parsed.sender_name, mail_hash, parsed.subject,
                            parsed.message_id, now, actor,
                        ),
                    )
                    for asset in stored:
                        conn.execute(
                            """
                            INSERT INTO batch_assets (
                                id, batch_id, role, filename, sha256, size_bytes,
                                path, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                new_id("asset"), batch_id, asset["role"],
                                asset["filename"], asset["sha256"],
                                int(asset["size_bytes"]), asset["path"], now,
                            ),
                        )
                    self._event_conn(
                        conn, batch_id, None, "batch_imported", actor=actor,
                        detail={
                            "filename": filename, "mail_hash": mail_hash,
                            "attachment_count": len(stored) - 1,
                            "aliases": {
                                a["filename"]: a["aliases"] for a in stored
                                if a["aliases"]}},
                        now=now)
            except sqlite3.IntegrityError:
                # 只有确认为"批次身份"唯一冲突（同一 mail_hash 已被其他
                # 并发/重复请求建立）才按重复导入处理并返回既有批次；
                # 其余 IntegrityError 必须作为明确失败抛出并清理本次孤儿
                # 材料（绝不静默吞掉、绝不误判重复）。
                with contextlib_suppress():
                    self._remove_batch_files(batch_id)
                identity_conflict = _row_dict(
                    self.db.fetchone(
                        "SELECT id FROM mail_batches WHERE account = ? "
                        "AND owner = ? AND mail_hash = ?",
                        (account, owner, mail_hash),
                    )
                )
                if identity_conflict is not None:
                    raise _ConcurrentImport() from None
                raise
            # 材料已持久化：在已落盘资产上跑首轮识别（DB 提交后纯计算）。
            preview = self._run_inspect(batch_id)
            with self.db.transaction() as conn:
                created_plan_id = self._insert_initial_plan(
                    conn, batch_id=batch_id, preview=preview,
                    actor=actor, now=utc_now())
                if created_plan_id is not None:
                    self._event_conn(
                        conn, batch_id, created_plan_id, "plan_created",
                        actor=actor,
                        detail={"version": 1,
                                "candidate_count": preview["candidate_count"],
                                "batch_ready": preview["batch_ready"]})
        except _ConcurrentImport:
            existing = _row_dict(
                self.db.fetchone(
                    "SELECT * FROM mail_batches WHERE account = ? AND owner = ? AND mail_hash = ?",
                    (account, owner, mail_hash),
                )
            )
            if existing is None:  # pragma: no cover - 防御：唯一冲突但行不可见
                raise
            self.logger.info(
                "batch_import_duplicate", batch_id=existing["id"], actor=actor)
            return self._dedupe_result(existing, actor=actor)
        except Exception:
            # 批次行已建但首轮识别失败：批次与材料保留，记录事件后抛出；
            # 重试走 _dedupe_result 的可重入首轮恢复（recover_initial_plan）。
            self.logger.exception(
                "batch_import_plan_failed", Exception("initial plan failed"),
                batch_id=batch_id)
            raise
        self.logger.info(
            "batch_imported", batch_id=batch_id, actor=actor,
            candidate_count=preview["candidate_count"],
            batch_ready=preview["batch_ready"])
        batch = self.require_batch(batch_id)
        return {"batch": batch, "created": True,
                "plan": self._current_plan(batch), "preview": preview}

    def _current_plan(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        plan_id = batch.get("current_plan_id")
        return self.get_plan(str(plan_id)) if plan_id else None

    def _dedupe_result(self, batch: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """重复/并发导入：返回既有批次；规则升级只产生新方案版本，不建新批次。

        首轮识别曾失败（current_plan_id 为空）时先可重入恢复 v1——
        绝不把"无方案"当成功返回，也绝不产生重复版本或孤儿材料。
        """
        plan = self._current_plan(batch)
        if plan is None and batch["status"] == "open":
            plan = self.recover_initial_plan(str(batch["id"]), actor=actor)
            batch = self.require_batch(str(batch["id"]))
        if plan is not None and batch["status"] == "open":
            preview = self._run_inspect(
                str(batch["id"]),
                assignments=self._assignments_request(
                    json.loads(str(plan["assignments_json"]))),
            )
            if (preview["inspector_version"] != plan["inspector_version"]
                    or preview["source_fingerprint"] != plan["source_fingerprint"]):
                plan = self._add_plan_version(
                    batch, base=plan, preview=preview,
                    assignments=json.loads(str(plan["assignments_json"])),
                    actor=actor, reason="识别规则/材料变化，重新识别")
                batch = self.require_batch(str(batch["id"]))
        return {"batch": batch, "created": False, "plan": plan}

    def _add_plan_version(
        self,
        batch: dict[str, Any],
        *,
        base: dict[str, Any],
        preview: dict[str, Any],
        assignments: list[dict[str, Any]],
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """在开放批次上追加一个不可变方案版本（单事务）。"""
        now = utc_now()
        plan_id = new_id("plan")
        version = int(base["version"]) + 1
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT status, current_plan_id FROM mail_batches WHERE id = ?",
                (str(batch["id"]),),
            ).fetchone()
            if row is None:
                raise KeyError(str(batch["id"]))
            if row["status"] != "open":
                raise ValidationError("批次已确认建票，方案已锁定，不能再调整")
            if str(row["current_plan_id"]) != str(base["id"]):
                raise ValidationError(
                    "该方案版本已过期（已有更新版本），请刷新后基于最新方案调整")
            self._insert_plan_conn(
                conn, batch_id=str(batch["id"]), plan_id=plan_id,
                version=version, preview=preview, assignments=assignments,
                derived_from=str(base["id"]), reason=reason, actor=actor,
                now=now)
            self._event_conn(
                conn, str(batch["id"]), plan_id, "plan_created", actor=actor,
                detail={"from_plan_id": str(base["id"]), "version": version,
                        "reason": reason,
                        "candidate_count": preview["candidate_count"],
                        "batch_ready": preview["batch_ready"]}, now=now)
        self.logger.info(
            "batch_plan_created", batch_id=batch["id"], plan_id=plan_id,
            version=version, actor=actor, reason=reason)
        return self.require_plan(plan_id)

    # ------------------------------------------------------------------
    # initial-plan recovery（可重入）
    # ------------------------------------------------------------------
    def recover_initial_plan(self, batch_id: str, *, actor: str) -> dict[str, Any]:
        """首轮识别失败后恢复 v1 方案（幂等可重入）。

        - 已有方案（并发初始恢复/重复导入先到）：直接返回既有方案；
        - 否则在已持久化、哈希校验过的材料上重跑首轮识别并落 v1；
        - INSERT 唯一冲突按"取既有"处理，绝不产生重复版本。
        """
        batch = self.require_batch(batch_id)
        plan = self._current_plan(batch)
        if plan is not None:
            return plan
        if batch["status"] != "open":
            raise ValidationError(f"批次状态 {batch['status']} 不允许恢复首轮识别")
        preview = self._run_inspect(batch_id)
        with self.db.transaction() as conn:
            created_plan_id = self._insert_initial_plan(
                conn, batch_id=batch_id, preview=preview,
                actor=actor, now=utc_now())
            if created_plan_id is not None:
                self._event_conn(
                    conn, batch_id, created_plan_id, "plan_created",
                    actor=actor,
                    detail={"version": 1, "recovered": True,
                            "candidate_count": preview["candidate_count"],
                            "batch_ready": preview["batch_ready"]})
        self.logger.info(
            "batch_initial_plan_recovered", batch_id=batch_id, actor=actor)
        return self.require_plan(self.require_batch(batch_id)["current_plan_id"])

    def _insert_plan_conn(
        self,
        conn,
        *,
        batch_id: str,
        plan_id: str,
        version: int,
        preview: dict[str, Any],
        assignments: list[dict[str, Any]],
        derived_from: str | None,
        reason: str,
        actor: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO batch_plan_versions (
                id, batch_id, version, source_fingerprint, inspector_version,
                alias_marker, preview_json, assignments_json, derived_from,
                batch_ready, reason, created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id, batch_id, version, preview["source_fingerprint"],
                preview["inspector_version"], preview.get("alias_marker"),
                json.dumps(preview, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(assignments, ensure_ascii=False, default=str),
                derived_from, 1 if preview.get("batch_ready") else 0,
                reason, now, actor,
            ),
        )
        for cand in preview.get("candidates", []):
            conn.execute(
                """
                INSERT INTO batch_candidates (
                    id, plan_id, batch_id, candidate_key, kind, preview_json,
                    decision
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    new_id("bcand"), plan_id, batch_id, cand["candidate_id"],
                    cand.get("kind"),
                    json.dumps(cand, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
        # 版本指针最后更新：指针永远只指向完整写好的方案版本（崩溃/
        # 重试恢复时不会指向半截方案）。
        conn.execute(
            "UPDATE mail_batches SET current_plan_id = ? WHERE id = ?",
            (plan_id, batch_id),
        )

    def _insert_initial_plan(
        self,
        conn,
        *,
        batch_id: str,
        preview: dict[str, Any],
        actor: str,
        now: str,
    ) -> str | None:
        """首轮识别落库（可重入）：批次无当前方案时创建 v1。

        已存在 v1 时按幂等返回既有方案（并发导入/崩溃重试可能重复走到
        这里）；INSERT 的唯一冲突按"取既有"处理，绝不抛错。
        """
        plan = _row_dict(
            conn.execute(
                "SELECT * FROM batch_plan_versions "
                "WHERE batch_id = ? AND version = 1",
                (batch_id,),
            ).fetchone()
        )
        if plan is not None:
            if plan["batch_ready"] != (1 if preview.get("batch_ready") else 0):
                conn.execute(
                    "UPDATE batch_plan_versions SET batch_ready = ? WHERE id = ?",
                    (1 if preview.get("batch_ready") else 0, plan["id"]))
            conn.execute(
                "UPDATE mail_batches SET current_plan_id = ? WHERE id = ?",
                (plan["id"], batch_id))
            return None
        plan_id = new_id("plan")
        try:
            self._insert_plan_conn(
                conn, batch_id=batch_id, plan_id=plan_id, version=1,
                preview=preview, assignments=[], derived_from=None,
                reason="初始导入识别", actor=actor, now=now)
        except sqlite3.IntegrityError:
            # 并发初始恢复抢到先：按其版本为当前方案即可（幂等）。
            existing = conn.execute(
                "SELECT id FROM batch_plan_versions "
                "WHERE batch_id = ? AND version = 1",
                (batch_id,),
            ).fetchone()
            if existing is None:
                raise
            conn.execute(
                "UPDATE mail_batches SET current_plan_id = ? WHERE id = ?",
                (existing["id"], batch_id))
            return None
        return plan_id

    # ------------------------------------------------------------------
    # human adjustments -> new immutable plan version
    # ------------------------------------------------------------------
    @staticmethod
    def _op_key(op: dict[str, Any]) -> tuple:
        if op.get("type") == "mail_row":
            return ("mail_row", int(op["table_index"]), int(op["row"]))
        return ("source", str(op["sha256"]).lower())

    def _validate_ops(
        self, preview: dict[str, Any], accumulated: dict[tuple, dict[str, Any]],
        ops: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """校验并合并调整操作；所有目标必须来自服务端存储的当前方案版本。"""
        candidate_ids = {c["candidate_id"] for c in preview.get("candidates", [])}
        unmatched_rows = {
            (int(r["table_index"]), int(r["row"]))
            for r in preview.get("unmatched_mail_rows", [])
        }
        unmatched_sources = {
            str(s.get("sha256")).lower(): s
            for s in preview.get("unmatched_sources", []) if s.get("sha256")
        }
        merged = dict(accumulated)
        changed: list[dict[str, Any]] = []
        for raw_op in ops:
            if not isinstance(raw_op, dict):
                raise ValidationError("调整操作格式无效")
            op_type = raw_op.get("type")
            action = raw_op.get("action")
            reason = str(raw_op.get("reason") or "").strip()
            if op_type not in {"mail_row", "source"}:
                raise ValidationError("调整类型必须是 mail_row 或 source")
            if action not in {"assign", "exclude", "clear"}:
                raise ValidationError("调整动作必须是 assign / exclude / clear")
            if op_type == "mail_row":
                try:
                    key = ("mail_row", int(raw_op["table_index"]),
                           int(raw_op["row"]))
                except (KeyError, TypeError, ValueError):
                    raise ValidationError("正文行调整缺少有效定位") from None
                if action == "clear":
                    if key not in merged:
                        raise ValidationError("该正文行没有可撤销的调整")
                    del merged[key]
                    changed.append({"type": "mail_row", "table_index": key[1],
                                    "row": key[2], "action": "clear"})
                    continue
                if (key[1], key[2]) not in unmatched_rows:
                    raise ValidationError(
                        "该正文行不在当前方案的未归属列表中（可能已被处理或"
                        "方案已变化）")
            else:
                sha = str(raw_op.get("sha256") or "").strip().lower()
                if not sha:
                    raise ValidationError("附件调整缺少内容哈希")
                key = ("source", sha)
                if action == "clear":
                    if key not in merged:
                        raise ValidationError("该附件没有可撤销的调整")
                    del merged[key]
                    changed.append({"type": "source", "sha256": sha,
                                    "action": "clear"})
                    continue
                if sha not in unmatched_sources:
                    raise ValidationError(
                        "该附件不在当前方案的未归属列表中（可能已被处理或"
                        "方案已变化）")
            op: dict[str, Any] = dict(raw_op)
            op["reason"] = reason
            if action == "assign":
                candidate_id = str(raw_op.get("candidate_id") or "")
                if candidate_id not in candidate_ids:
                    raise ValidationError(
                        f"指派目标候选 {candidate_id or '(空)'} 不在当前方案中")
                if op_type == "source" and \
                        unmatched_sources[key[1]].get("status") != "empty":
                    raise ValidationError(
                        "只有已读取但未识别业务身份的工作簿可以指派到候选；"
                        "其他附件请用排除并说明证据角色")
                op["candidate_id"] = candidate_id
            else:  # exclude
                if not reason:
                    raise ValidationError("排除必须填写可审计的理由")
                op.pop("candidate_id", None)
            merged[key] = op
            changed.append(op)
        if not changed:
            raise ValidationError("没有有效的调整操作")
        return list(merged.values()), changed

    @staticmethod
    def _assignments_request(assignments: list[dict[str, Any]]) -> dict[str, Any]:
        rows, sources = [], []
        for op in assignments:
            if op.get("type") == "mail_row":
                rows.append({
                    "table_index": int(op["table_index"]),
                    "row": int(op["row"]),
                    "action": op["action"],
                    "candidate_id": op.get("candidate_id"),
                    "reason": op.get("reason") or "",
                })
            else:
                sources.append({
                    "sha256": str(op["sha256"]).lower(),
                    "action": op["action"],
                    "candidate_id": op.get("candidate_id"),
                    "reason": op.get("reason") or "",
                })
        return {"mail_rows": rows, "sources": sources}

    def adjust_plan(
        self,
        batch_id: str,
        *,
        base_plan_id: str,
        ops: list[dict[str, Any]],
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """应用人工调整并产生新的不可变方案版本（乐观锁：必须基于当前方案）。"""
        batch = self.require_batch(batch_id)
        if batch["status"] != "open":
            raise ValidationError("批次已确认建票，方案已锁定，不能再调整")
        base = self.require_plan(base_plan_id)
        if str(base["batch_id"]) != batch_id:
            raise KeyError(base_plan_id)
        if str(base["id"]) != str(batch.get("current_plan_id")):
            raise ValidationError(
                "该方案版本已过期（已有更新版本），请刷新后基于最新方案调整")
        preview = json.loads(str(base["preview_json"]))
        accumulated = {
            self._op_key(op): op
            for op in json.loads(str(base["assignments_json"]))
        }
        merged_ops, changed = self._validate_ops(preview, accumulated, ops)
        request_assignments = self._assignments_request(merged_ops)
        # 纯计算先于写库：在已持久化材料上重跑识别
        new_preview = self._run_inspect(
            batch_id, assignments=request_assignments)
        plan = self._add_plan_version(
            batch, base=base, preview=new_preview, assignments=merged_ops,
            actor=actor, reason=reason or "人工归属调整")
        self._event_after(plan, changed, actor=actor, reason=reason)
        return self.batch_detail(batch_id)

    def _event_after(
        self,
        plan: dict[str, Any],
        changed: list[dict[str, Any]],
        *,
        actor: str,
        reason: str | None,
    ) -> None:
        with self.db.transaction() as conn:
            self._event_conn(
                conn, str(plan["batch_id"]), str(plan["id"]), "plan_adjusted",
                actor=actor,
                detail={"version": int(plan["version"]), "ops": changed,
                        "reason": reason})

    # ------------------------------------------------------------------
    # confirm + idempotent materialization
    # ------------------------------------------------------------------
    def _publish_snapshot_dirs(
        self,
        conn,
        batch_id: str,
        plan: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """在确认事务内为每票发布独占不可变快照目录并做完整业务校验。

        - 目录按 :meth:`_publish_dir` 独占创建（``exist_ok=False``）：本
          请求私有，其他方案/请求/重试绝不共享，已引用目录永不覆盖；
        - 本票附件按哈希从批次资产复制（资产逐文件校验，缺失/篡改即
          拒绝）；snapshot.json 与 :meth:`_verify_staged_snapshot` 做
          全量业务校验（schema/版本/指纹、候选全业务内容、正文行、附
          件清单+逐文件哈希+路径边界；时间戳等非业务元数据规范化），
          返回可信哈希；
        - 任一步失败：清理本目录后抛出，事务整体回滚；本目录若在事务
          后续步骤失败时已被写出，则作为未引用残留由 sweep 回收。

        必须在已通过状态复核的确认事务内调用（先复核、后落盘）。
        """
        asset_rows = conn.execute(
            "SELECT * FROM batch_assets WHERE batch_id = ? AND role = 'attachment'",
            (batch_id,),
        ).fetchall()
        asset_by_sha = {str(a["sha256"]): dict(a) for a in asset_rows}
        published: dict[str, dict[str, Any]] = {}
        for cand in sorted(candidates, key=lambda c: c["candidate_id"]):
            key = str(cand["candidate_id"])
            publish_dir = self._publish_dir(batch_id, str(plan["id"]), key)
            publish_dir.mkdir(parents=True, exist_ok=False)
            try:
                files = []
                for ref in cand.get("source_refs", []):
                    sha = str(ref["sha256"])
                    asset = asset_by_sha.get(sha)
                    if asset is None:
                        raise ValidationError(
                            f"候选 {key} 引用的材料 {sha[:12]} 不在批次资产中")
                    src = Path(str(asset["path"]))
                    if not src.is_file() or sha256_file(src) != sha:
                        raise ValidationError(
                            f"批次材料 {asset['filename']} 校验失败（缺失或"
                            "被篡改），已拒绝建票")
                    dest = publish_dir / stored_filename(
                        sha, str(asset["filename"]))
                    _write_file_atomic(dest, src.read_bytes())
                    files.append({
                        "sha256": sha,
                        "filename": asset["filename"],
                        "path": str(dest),
                        "size_bytes": int(asset["size_bytes"]),
                        "assigned": bool(ref.get("assigned")),
                        "regions": ref.get("regions") or [],
                    })
                snapshot = {
                    "schema": 1,
                    "batch_id": batch_id,
                    "plan_id": str(plan["id"]),
                    "plan_version": int(plan["version"]),
                    "source_fingerprint": str(plan["source_fingerprint"]),
                    "candidate": cand,
                    "assets": files,
                    "mail_rows": cand.get("mail_row_refs", []),
                    "created_at": utc_now(),
                }
                _write_file_atomic(
                    publish_dir / "snapshot.json",
                    json.dumps(snapshot, ensure_ascii=False, indent=2,
                               default=str).encode("utf-8"))
                trusted_hash = self._verify_staged_snapshot(
                    batch_id, plan, cand, base_dir=publish_dir)
            except BaseException:
                shutil.rmtree(publish_dir, ignore_errors=True)
                raise
            published[key] = {
                "dir": str(publish_dir),
                "snapshot_path": str(publish_dir / "snapshot.json"),
                "trusted_hash": trusted_hash,
            }
        return published

    def _materialized_result(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch": self.require_batch(batch_id),
            "tasks": self.batch_tasks(batch_id),
        }

    def _idempotent_or_conflict(
        self,
        batch: dict[str, Any],
        *,
        plan_id: str,
        fingerprint: str,
        selected: list[str],
    ) -> dict[str, Any]:
        existing = self.batch_tasks(str(batch["id"]))
        same = (
            str(batch.get("confirmed_plan_id")) == str(plan_id)
            and str(batch.get("confirmed_fingerprint")) == str(fingerprint)
            and {str(t["candidate_key"]) for t in existing} == set(selected)
        )
        if same:
            return {**self._materialized_result(str(batch["id"])),
                    "idempotent": True}
        raise ValidationError(
            "该批次已按其他方案版本/选票组合确认建票；已物化批次本阶段不可"
            "再次改方案或额外建票"
        )

    def confirm_and_create(
        self,
        batch_id: str,
        *,
        plan_id: str,
        fingerprint: str,
        selected: list[str],
        exclusions: dict[str, str],
        actor: str,
    ) -> dict[str, Any]:
        """“确认并创建任务”单一操作：校验 → 同步边界内复核 → 发布独占
        快照 → 单事务建票。

        重复点击/并发/重启后相同请求只返回既有同一组任务；任何失败要么
        完整创建同一组任务，要么零任务零确认，绝不半批。

        防 TOCTOU：状态复核（open/当前方案/指纹）在**改动任何已发布文件
        之前**、于同一可靠同步边界内完成——同进程靠批次确认锁，跨进程
        靠 BEGIN IMMEDIATE（SQLite 写锁）。过期请求（其方案已被更新的
        方案取代后才恢复）在边界内读到已物化/已过期状态即被拒绝，绝不
        先删除或覆盖别人已确认引用的快照目录。快照目录每次请求独占不
        可变（``snapshots/<plan_id>/<cand>-<token>``），已引用目录永不
        被覆盖；确认失败的私有目录作为未引用残留由
        sweep_orphan_snapshots 在同一同步边界内复核引用后回收。
        """
        batch = self.require_batch(batch_id)
        if batch["status"] == "materialized":
            return self._idempotent_or_conflict(
                batch, plan_id=plan_id, fingerprint=fingerprint,
                selected=sorted(set(selected)))
        if batch["status"] != "open":
            raise ValidationError(f"批次状态 {batch['status']} 不允许确认")
        plan = self.require_plan(plan_id)
        if str(plan["batch_id"]) != batch_id:
            raise KeyError(plan_id)
        if str(plan["id"]) != str(batch.get("current_plan_id")):
            raise ValidationError(
                "确认的方案版本已过期：请刷新页面基于最新方案确认")
        if str(plan["source_fingerprint"]) != str(fingerprint):
            raise ValidationError(
                "方案指纹不匹配：页面内容已过期或材料已变化，请刷新后重新核对")
        preview = json.loads(str(plan["preview_json"]))

        blockers = [i for i in preview.get("issues", []) if i.get("blocking")]
        if blockers:
            codes = ", ".join(sorted({str(i.get("code")) for i in blockers}))
            raise ValidationError(
                f"方案仍存在 {len(blockers)} 个阻断问题（{codes}），不能确认；"
                "数值/身份冲突必须依据材料重新核对，不能一键忽略")
        if preview.get("unmatched_mail_rows"):
            raise ValidationError(
                "仍有未归属的正文业务行：请先逐行指派到唯一候选或有理由排除")
        undecided_sources = [
            s for s in preview.get("unmatched_sources", [])
            if not _is_decorative_source(s)
        ]
        if undecided_sources:
            raise ValidationError(
                "仍有未处理的非装饰附件：请先指派到唯一候选或有理由排除"
                "（无法读取的 Excel 必须显式处理，不能默认忽略）")

        candidates = {c["candidate_id"]: c for c in preview.get("candidates", [])}
        chosen = sorted({str(k) for k in selected if str(k) in candidates})
        unknown_selected = sorted({str(k) for k in selected} - set(candidates))
        if unknown_selected:
            raise ValidationError(
                f"所选候选不存在于当前方案：{', '.join(unknown_selected)}")
        unknown_excluded = sorted(
            {str(k) for k in exclusions} - set(candidates))
        if unknown_excluded:
            raise ValidationError(
                f"排除的候选不存在于当前方案：{', '.join(unknown_excluded)}")
        overlap = sorted(set(chosen) & {str(k) for k in exclusions})
        if overlap:
            raise ValidationError(
                f"候选同时被选入和排除：{', '.join(overlap)}")
        uncovered = sorted(
            set(candidates) - set(chosen) - {str(k) for k in exclusions})
        if uncovered:
            raise ValidationError(
                f"以下候选既未选入也未排除（不能悄悄丢票）："
                f"{', '.join(uncovered)}")
        if not chosen:
            raise ValidationError("所选候选不能为空：至少选入一票才能建任务")
        for key in chosen:
            cand = candidates[key]
            blocking = [i for i in cand.get("issues", []) if i.get("blocking")]
            if cand.get("needs_confirmation") or blocking:
                raise ValidationError(
                    f"候选 {key} 仍存在未解决问题，不能选入建票；请先调整"
                    "或改为有理由排除")
        cleaned_exclusions = {str(k): str(v).strip()
                              for k, v in exclusions.items()}
        for key, why in cleaned_exclusions.items():
            if not why:
                raise ValidationError(f"排除候选 {key} 必须填写可审计的理由")

        # 1) 同一可靠同步边界内先复核状态（同进程确认锁 + 跨进程
        #    BEGIN IMMEDIATE）：任何过期/冲突/幂等请求在改动任何已发布
        #    文件之前被拦下，绝不触碰他人已确认的快照目录。
        # 2) 复核通过后发布本请求独占不可变快照目录（全量业务校验，
        #    可信哈希随任务关联持久化）；提交前再全量复核一次。
        # 3) 单事务：确认 + 建票 + 关联 + 物化 + 事件。
        selected_cands = [candidates[key] for key in chosen]
        now = utc_now()
        with self._confirm_lock(batch_id):
            try:
                with self.db.transaction() as conn:
                    row = conn.execute(
                        "SELECT * FROM mail_batches WHERE id = ?", (batch_id,)
                    ).fetchone()
                    if row is None:
                        raise KeyError(batch_id)
                    if row["status"] == "materialized":
                        raise _AlreadyMaterialized()
                    if row["status"] != "open":
                        raise ValidationError(
                            f"批次状态 {row['status']} 不允许确认")
                    if str(row["current_plan_id"]) != str(plan["id"]):
                        raise ValidationError(
                            "确认的方案版本已过期：请刷新页面基于最新方案确认")
                    plan_row = conn.execute(
                        "SELECT source_fingerprint FROM batch_plan_versions"
                        " WHERE id = ?",
                        (plan_id,),
                    ).fetchone()
                    if plan_row is None or str(
                            plan_row["source_fingerprint"]) != str(fingerprint):
                        raise ValidationError(
                            "方案指纹不匹配：页面内容已过期或材料已变化，"
                            "请刷新后重新核对")
                    published = self._publish_snapshot_dirs(
                        conn, batch_id, plan, selected_cands)
                    # 提交前最终复核：发布目录内快照仍与本方案完全一致
                    # （含逐文件哈希与路径边界），且与可信哈希一致。
                    for cand in selected_cands:
                        key = str(cand["candidate_id"])
                        digest = self._verify_staged_snapshot(
                            batch_id, plan, cand,
                            base_dir=Path(published[key]["dir"]))
                        if digest != published[key]["trusted_hash"]:
                            raise ValidationError(
                                f"快照 {published[key]['snapshot_path']} 提交前"
                                "复核不一致（疑似篡改），已拒绝建票")
                    for key in chosen:
                        conn.execute(
                            """
                            UPDATE batch_candidates
                               SET decision = 'selected', decided_by = ?, decided_at = ?
                             WHERE plan_id = ? AND candidate_key = ?
                            """,
                            (actor, now, plan_id, key),
                        )
                    for key, why in cleaned_exclusions.items():
                        conn.execute(
                            """
                            UPDATE batch_candidates
                               SET decision = 'excluded', decision_reason = ?,
                                   decided_by = ?, decided_at = ?
                             WHERE plan_id = ? AND candidate_key = ?
                            """,
                            (why, actor, now, plan_id, key),
                        )
                    self._event_conn(
                        conn, batch_id, plan_id, "batch_confirmed", actor=actor,
                        detail={"fingerprint": fingerprint, "selected": chosen,
                                "excluded": cleaned_exclusions}, now=now)
                    task_ids: list[str] = []
                    for cand in sorted(selected_cands,
                                       key=lambda c: c["candidate_id"]):
                        key = cand["candidate_id"]
                        kind = cand.get("kind") if cand.get("kind") in {
                            "machine", "parts"} else "auto"
                        task_id = new_id("task")
                        self.tasks._insert_task_conn(
                            conn,
                            task_id=task_id,
                            owner=str(batch["owner"]),
                            owner_name=batch.get("owner_name"),
                            kind=kind,
                            origin_mail_id=str(batch["mail_id"]),
                            status="RECEIVED",
                            actor=actor,
                            now=now,
                            event_detail={"batch_id": batch_id,
                                          "candidate_key": key,
                                          "plan_id": plan_id},
                        )
                        conn.execute(
                            """
                            INSERT INTO batch_tasks (
                                id, batch_id, plan_id, candidate_key, task_id,
                                snapshot_path, snapshot_hash, execution_hold,
                                hold_reason, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                new_id("btask"), batch_id, plan_id, key, task_id,
                                published[key]["snapshot_path"],
                                published[key]["trusted_hash"], HOLD_REASON, now,
                            ),
                        )
                        task_ids.append(task_id)
                    conn.execute(
                        """
                        UPDATE mail_batches
                           SET status = 'materialized', confirmed_plan_id = ?,
                               confirmed_fingerprint = ?, confirmed_at = ?,
                               confirmed_by = ?, materialized_at = ?
                         WHERE id = ? AND status = 'open'
                        """,
                        (plan_id, fingerprint, now, actor, now, batch_id),
                    )
                    self._event_conn(
                        conn, batch_id, plan_id, "batch_materialized",
                        actor=actor,
                        detail={"task_ids": task_ids, "selected": chosen},
                        now=now)
            except _AlreadyMaterialized:
                fresh = self.require_batch(batch_id)
                return self._idempotent_or_conflict(
                    fresh, plan_id=plan_id, fingerprint=fingerprint,
                    selected=chosen)
        self.logger.info(
            "batch_materialized", batch_id=batch_id, plan_id=plan_id,
            task_count=len(task_ids), actor=actor)
        return {**self._materialized_result(batch_id), "idempotent": False}

    def verify_batch_task_snapshot(self, task_id: str) -> dict[str, Any]:
        """按 batch_tasks 持久化的可信哈希复核任务快照（防篡改/漂移）。

        基于已物化任务的 snapshot_path + snapshot_hash：重新读取快照、
        规范化（忽略时间戳等非业务元数据）、比对可信哈希；并对附件做
        逐文件哈希与路径边界校验。绝不把本地 JSON 事实当权威。
        """
        row = _row_dict(
            self.db.fetchone(
                "SELECT * FROM batch_tasks WHERE task_id = ?", (task_id,)
            )
        )
        if row is None:
            raise KeyError(task_id)
        batch_id = str(row["batch_id"])
        plan = self.require_plan(str(row["plan_id"]))
        candidate_row = _row_dict(
            self.db.fetchone(
                "SELECT preview_json FROM batch_candidates "
                "WHERE plan_id = ? AND candidate_key = ?",
                (str(row["plan_id"]), str(row["candidate_key"])),
            )
        )
        candidate = (json.loads(str(candidate_row["preview_json"]))
                     if candidate_row else {"candidate_id": str(row["candidate_key"])})
        trusted = str(row["snapshot_hash"])
        snap_dir = Path(str(row["snapshot_path"])).resolve()
        if snap_dir.is_file():
            snap_dir = snap_dir.parent
        digest = self._verify_staged_snapshot(
            batch_id, plan, candidate, base_dir=snap_dir)
        return {"task_id": task_id, "snapshot_path": str(snap_dir),
                "trusted_hash": trusted, "verified_hash": digest,
                "ok": digest == trusted}

    # ------------------------------------------------------------------
    # orphan snapshot cleanup（崩溃恢复策略）
    # ------------------------------------------------------------------
    def orphan_snapshot_report(self) -> dict[str, Any]:
        """找出未被任何 batch_tasks 引用的快照目录（崩溃残留/失败私有目录）。

        确认在发布独占目录后才提交任务关联；进程若在两者之间崩溃，或事务
        中途失败回滚，会留下未引用目录。这些目录不驱动任何执行，可安全
        清理（sweep_orphan_snapshots）。TTL 只是保守宽限，正确性由
        sweep 删除前在同一可靠同步边界（批次确认锁 + BEGIN IMMEDIATE）
        内重新核验引用保证。被引用目录自身及其祖先目录绝不会被报告。
        """
        orphans: list[dict[str, str]] = []
        root = self._root()
        if not root.is_dir():
            return {"orphans": [], "count": 0}
        referenced = {
            str(Path(str(row["snapshot_path"])).parent)
            for row in self.db.fetchall("SELECT snapshot_path FROM batch_tasks")
        }
        now_ts = _utc_timestamp()
        for batch_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            snaps = batch_dir / "snapshots"
            if not snaps.is_dir():
                continue
            # 同进程正在确认中的批次跳过（其私有目录发布后即可能被引用，
            # 交由 sweep 的同步边界复核兜底）；跨进程由 sweep 的
            # BEGIN IMMEDIATE 与引用复核覆盖。
            lock = self._confirm_lock(str(batch_dir.name))
            if not lock.acquire(blocking=False):
                continue  # 该批次正在确认：目录是活的，跳过
            try:
                lock.release()
            except RuntimeError:
                pass
            protected = set(referenced)
            for ref in list(referenced):
                protected.update(str(p) for p in Path(ref).parents)
            for cand_dir in sorted(
                    p for p in snaps.rglob("*")
                    if p.is_dir() and not _is_staging_tmp(p)):
                if str(cand_dir) in protected:
                    continue
                # 只报告含文件的叶子残留（快照目录/遗留暂存）；空的方案
                # 中间目录由 sweep 的 rmdir 顺带清理，不算快照残留。
                if not any(p.is_file() for p in cand_dir.iterdir()):
                    continue
                try:
                    mtime = cand_dir.stat().st_mtime
                except OSError:
                    continue
                if now_ts - mtime < _RESIDUE_TTL_SECONDS:
                    continue  # 纯宽限： newborn 目录留给确认/复核完成
                orphans.append({
                    "batch_id": batch_dir.name,
                    "candidate_key": cand_dir.name,
                    "path": str(cand_dir),
                })
        return {"orphans": orphans, "count": len(orphans)}

    def sweep_orphan_snapshots(self) -> dict[str, Any]:
        """安全清理未被引用的快照目录（仅未引用目录，绝不碰批次资产）。

        删除前在同一可靠同步边界内重新核验：先取批次确认锁（同进程与
        确认互斥），再在 BEGIN IMMEDIATE 事务（跨进程与确认/其他清扫
        互斥）内重新读取 batch_tasks 引用——若目录（或其子孙）刚被确认
        引用即放弃删除。因为发布路径每次请求独占（含随机令牌），清扫
        目标绝不可能在复核后被他人重新引用，复核-删除之间无竞态。
        已引用目录因此**绝不**被改写或删除。只回收含文件的叶子残留；
        空的方案中间目录/遗留容器本阶段**保留不动**（无实际危害，且
        锁外 rmdir 会误删活跃事务刚 mkdir 的在制品目录）。
        """
        report = self.orphan_snapshot_report()
        removed: list[str] = []
        root = self._root().resolve()
        for item in report["orphans"]:
            batch_id = item["batch_id"]
            path = Path(item["path"])
            with self._confirm_lock(batch_id):
                with self.db.transaction() as conn:
                    referenced = {
                        str(Path(str(r["snapshot_path"])).parent)
                        for r in conn.execute(
                            "SELECT snapshot_path FROM batch_tasks"
                        ).fetchall()
                    }
                    if str(path) in referenced or any(
                            str(r).startswith(str(path) + os.sep)
                            for r in referenced):
                        continue  # 刚被确认引用：放弃删除
                    try:
                        resolved = path.resolve()
                    except OSError:
                        continue
                    if root not in resolved.parents or not path.is_dir():
                        continue
                    shutil.rmtree(path)
                    removed.append(str(path))
        # 本阶段不清理空的方案中间目录/遗留 .staging 容器：空目录无实际
        # 危害，而任何锁外 rmdir 都可能删掉其他进程（持活跃事务）刚
        # mkdir 尚未写文件的在制品目录（_publish_snapshot_dirs 的
        # mkdir→首写窗口）。若未来要清理，必须在同一批次确认锁 +
        # BEGIN IMMEDIATE 边界内重新核验后进行。
        if removed:
            self.logger.warning(
                "batch_orphan_snapshots_removed", count=len(removed))
        return {"removed": removed, "count": len(removed)}
