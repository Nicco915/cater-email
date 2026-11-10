"""已物化批次子任务的显式目的国确认（阶段 4）。

本模块只服务一种人工动作：为**已确认建票且仍挂起**的多票批次子任务，逐票
明确目的国。安全边界：

- 批次身份只认 ``mail_batches.status='materialized'`` 的
  ``confirmed_plan_id``；未物化、方案伪造/过期一律拒绝；
- 每票必须同时满足：``batch_tasks.batch_id``/``plan_id`` 与该批次确认方案
  一致、``execution_hold=1``、任务未删除/未归档、状态仍为未开放执行的
  ``RECEIVED``；跨批次任务绝不接受；
- 国家只来自既有参考服务：``ReferenceService.resolve``（含启用别名）后再
  用 ``get_country`` 复核国家启用状态；绝不从收件地址或前端中文名/alpha3
  对推断，也不信任客户端提供的名称；
- 乐观修订使用该票最新 ``task_events.code='batch_destination_confirmed'``
  的真实事件 id，没有记录为 0；不依赖秒级 ``updated_at``；表单携带的
  ``expected_revision`` 与参考版本都在 **BEGIN IMMEDIATE 事务内**复核；
- 先完成全部输入/归属/状态/修订/国家校验，再统一写所选票的当前
  ``tasks``/``task_versions`` 目的国并追加逐票审计；任何一项非法都整体
  回滚（零写入），未选票保持原样；
- 同值且修订匹配为无操作（不追加审计、不改行）；改值才写当前版本目的国，
  并记录前后值、理由、操作者、参考版本与本次选择范围；
- 不改变任务状态/版本/重试/执行挂起/outbox/快照，不读写父邮件正文，不
  访问网络或凭据；get 展示的票号只取服务器已保存候选 preview JSON 的身份。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from . import state
from .db import Database
from .errors import ValidationError
from .logging import JsonlLogger
from .reference import ReferenceService
from .tasks import TaskRepository
from .util import utc_now

# 目的国确认事件：revision 的唯一来源。只使用真实事件 id，绝不使用
# 秒级 updated_at 或客户端自报版本。
DESTINATION_EVENT_CODE = "batch_destination_confirmed"
DESTINATION_EVENT_STAGE = "destination"

_UPDATE_FIELDS = frozenset({"task_id", "country", "expected_revision"})


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是非空字符串")
    text = value.strip()
    if not text:
        raise ValidationError(f"{field} 不能为空")
    return text


def _require_revision(value: Any, field: str) -> int:
    # bool 是 int 的子类，必须显式排除，避免 True/False 被当成 1/0。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是非负整数")
    if value < 0:
        raise ValidationError(f"{field} 必须是非负整数")
    return int(value)


class BatchDestinationService:
    """逐票目的国读取与显式确认（组合既有 Database/参考/任务仓储）。"""

    def __init__(
        self,
        db: Database,
        references: ReferenceService,
        tasks: TaskRepository,
        logger: JsonlLogger,
    ) -> None:
        self.db = db
        self.references = references
        self.tasks = tasks
        self.logger = logger

    # ------------------------------------------------------------------
    # 输入校验（全部先于任何数据库读取/写入）
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_updates(updates: Any) -> list[dict[str, Any]]:
        if isinstance(updates, (str, bytes, dict)) or not isinstance(
                updates, Iterable):
            raise ValidationError("updates 必须是非空列表")
        items = list(updates)
        if not items:
            raise ValidationError("updates 不能为空：至少选择一票")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            label = f"第 {index} 项"
            if not isinstance(raw, dict):
                raise ValidationError(f"{label}更新必须是对象")
            unknown = set(raw) - _UPDATE_FIELDS
            if unknown:
                raise ValidationError(
                    f"{label}包含不允许的字段："
                    f"{', '.join(sorted(str(key) for key in unknown))}")
            missing = _UPDATE_FIELDS - set(raw)
            if missing:
                raise ValidationError(
                    f"{label}缺少字段：{', '.join(sorted(missing))}")
            task_id = _require_text(raw["task_id"], f"{label} task_id")
            if task_id in seen:
                raise ValidationError(f"task_id 重复：{task_id}")
            seen.add(task_id)
            country = _require_text(raw["country"], f"{label} country")
            revision = _require_revision(
                raw["expected_revision"], f"{label} expected_revision")
            normalized.append({
                "task_id": task_id,
                "country": country,
                "expected_revision": revision,
            })
        return normalized

    @staticmethod
    def _validate_optional_reference_version(value: Any) -> int | None:
        if value is None:
            return None
        return _require_revision(value, "expected_reference_version")

    # ------------------------------------------------------------------
    # 读：逐票当前值与确认记录
    # ------------------------------------------------------------------
    def get_batch_destinations(self, batch_id: str) -> dict[str, Any]:
        """返回批次已物化票的目的国视图（只读，不写任何行）。

        ``plan_id`` 固定为该批次 ``confirmed_plan_id``；未物化批次拒绝。
        票号只从服务器已保存的候选 preview JSON 的身份字段生成，不接受
        调用方提供的前端事实；返回值不包含父邮件正文或 owner 数据。
        """
        batch_id = _require_text(batch_id, "batch_id")
        batch_row = self.db.fetchone(
            "SELECT status, confirmed_plan_id FROM mail_batches WHERE id = ?",
            (batch_id,))
        if batch_row is None:
            raise KeyError(batch_id)
        if str(batch_row["status"]) != "materialized" or \
                not batch_row["confirmed_plan_id"]:
            raise ValidationError(
                "批次尚未完成确认建票（未物化），不能确认逐票目的国")
        plan_id = str(batch_row["confirmed_plan_id"])
        plan_row = self.db.fetchone(
            "SELECT id FROM batch_plan_versions WHERE id = ? AND batch_id = ?",
            (plan_id, batch_id))
        if plan_row is None:
            raise ValidationError("批次的确认方案不存在或与本批次不一致")

        task_rows = self.db.fetchall(
            """
            SELECT bt.task_id, bt.candidate_key, bt.execution_hold,
                   t.status, t.destination_name, t.destination_alpha3
              FROM batch_tasks bt JOIN tasks t ON t.id = bt.task_id
             WHERE bt.batch_id = ? AND bt.plan_id = ?
             ORDER BY bt.candidate_key
            """,
            (batch_id, plan_id),
        )
        task_ids = [str(row["task_id"]) for row in task_rows]

        latest: dict[str, dict[str, Any]] = {}
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            events = self.db.fetchall(
                f"""
                SELECT task_id, id, actor, created_at
                  FROM task_events
                 WHERE code = ? AND task_id IN ({placeholders})
                 ORDER BY id
                """,
                (DESTINATION_EVENT_CODE, *task_ids),
            )
            for event in events:  # ORDER BY id：同一票最后一条即最新
                latest[str(event["task_id"])] = dict(event)

        previews: dict[str, str] = {}
        for candidate in self.db.fetchall(
                "SELECT candidate_key, preview_json FROM batch_candidates "
                "WHERE plan_id = ?", (plan_id,)):
            previews[str(candidate["candidate_key"])] = str(
                candidate["preview_json"] or "")

        rows: list[dict[str, Any]] = []
        for row in task_rows:
            task_id = str(row["task_id"])
            event = latest.get(task_id)
            rows.append({
                "task_id": task_id,
                "candidate_key": str(row["candidate_key"]),
                "ticket_no": self._ticket_no(
                    previews.get(str(row["candidate_key"]), ""),
                    str(row["candidate_key"])),
                "destination_name": row["destination_name"],
                "destination_alpha3": row["destination_alpha3"],
                "revision": int(event["id"]) if event else 0,
                "confirmed_by": event["actor"] if event else None,
                "confirmed_at": event["created_at"] if event else None,
                "task_status": str(row["status"]),
                "execution_hold": int(row["execution_hold"]),
            })

        countries = [
            {"name_zh": country["name_zh"], "alpha3": country["alpha3"]}
            for country in self.references.list_countries(
                enabled_only=True, limit=500)
        ]
        return {
            "batch_id": batch_id,
            "plan_id": plan_id,
            "reference_version": self.references.reference_version(),
            "countries": countries,
            "rows": rows,
        }

    @staticmethod
    def _ticket_no(preview_json: str, candidate_key: str) -> str:
        """从已保存候选 preview 的身份字段取展示票号。

        preview_json 是服务器在识别时落库的不可变事实，不是请求参数；
        解析失败时退回 candidate_key，绝不猜测或补造票号。
        """
        try:
            preview = json.loads(preview_json or "{}")
        except (TypeError, ValueError):
            return candidate_key
        identity = preview.get("identity") if isinstance(preview, dict) else None
        if not isinstance(identity, dict):
            return candidate_key
        for field in ("invoice_no", "order_code", "mso", "serial"):
            values = identity.get(field)
            if isinstance(values, str):
                values = [values]
            if isinstance(values, (list, tuple)):
                cleaned = [str(value).strip() for value in values
                           if str(value).strip()]
                if cleaned:
                    return ", ".join(cleaned)
        return candidate_key

    # ------------------------------------------------------------------
    # 写：显式确认（全成或全不成）
    # ------------------------------------------------------------------
    def confirm_destinations(
        self,
        batch_id: str,
        *,
        plan_id: str,
        updates: list[dict],
        actor: str,
        reason: str,
        expected_reference_version: int | None = None,
    ) -> dict[str, Any]:
        """在一个 BEGIN IMMEDIATE 事务内确认所选票目的国。

        先完整校验并解析所有选择（批次/确认方案归属、hold、生命周期、
        RECEIVED 状态、国家启用、参考版本、逐票修订），全部通过后才写；
        任何一项非法即整体回滚，未选票不变。同值且修订匹配为无操作。
        返回值在 :meth:`get_batch_destinations` 基础上增加 ``changes``、
        ``changed_task_ids`` 与 ``unchanged_task_ids``；``changes[].revision``
        是新审计事件的真实 id（或未变票的原修订）。
        """
        batch_id = _require_text(batch_id, "batch_id")
        plan_id = _require_text(plan_id, "plan_id")
        items = self._validate_updates(updates)
        actor = _require_text(actor, "actor")
        reason = _require_text(reason, "reason")
        expected_reference = self._validate_optional_reference_version(
            expected_reference_version)

        changes: list[dict[str, Any]] = []
        changed_ids: list[str] = []
        unchanged_ids: list[str] = []
        recorded_reference = 0

        with self.db.transaction() as conn:
            batch_row = conn.execute(
                "SELECT status, confirmed_plan_id FROM mail_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch_row is None:
                raise KeyError(batch_id)
            if str(batch_row["status"]) != "materialized" or \
                    not batch_row["confirmed_plan_id"]:
                raise ValidationError(
                    "批次尚未完成确认建票（未物化），不能确认逐票目的国")
            confirmed_plan_id = str(batch_row["confirmed_plan_id"])
            if confirmed_plan_id != plan_id:
                raise ValidationError(
                    "目的国确认方案已过期：请刷新后基于当前已确认方案重试")
            plan_row = conn.execute(
                "SELECT id FROM batch_plan_versions WHERE id = ? AND batch_id = ?",
                (plan_id, batch_id),
            ).fetchone()
            if plan_row is None:
                raise ValidationError("批次的确认方案不存在或与本批次不一致")

            # 参考版本必须在事务内复核（BEGIN IMMEDIATE 后不再有参考写者）。
            recorded_reference = self.references.reference_version()
            if expected_reference is not None and \
                    recorded_reference != expected_reference:
                raise ValidationError(
                    f"参考数据版本已变化（当前 {recorded_reference}，"
                    f"表单 {expected_reference}），请刷新后重试")

            # 第一遍：只校验/解析，绝不写。任何一项非法都整体拒绝。
            resolved: list[dict[str, Any]] = []
            for item in items:
                task_id = item["task_id"]
                link = conn.execute(
                    "SELECT batch_id, plan_id, candidate_key, execution_hold "
                    "FROM batch_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if link is None or str(link["batch_id"]) != batch_id:
                    raise ValidationError(
                        f"任务 {task_id} 不属于该已物化批次，已拒绝")
                if str(link["plan_id"]) != plan_id:
                    raise ValidationError(
                        f"任务 {task_id} 不属于该批次确认方案，已拒绝")
                if int(link["execution_hold"]) != 1:
                    raise ValidationError(
                        f"任务 {task_id} 未处于批次挂起状态，不能确认目的国")
                task = conn.execute(
                    "SELECT id, status, version, destination_name, "
                    "destination_alpha3, archived_at, deleted_at "
                    "FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise ValidationError(f"任务 {task_id} 不存在，已拒绝")
                if task["deleted_at"] is not None:
                    raise ValidationError(f"任务 {task_id} 已删除，不能确认目的国")
                if task["archived_at"] is not None:
                    raise ValidationError(f"任务 {task_id} 已归档，不能确认目的国")
                if str(task["status"]) != state.RECEIVED:
                    raise ValidationError(
                        f"任务 {task_id} 当前状态 {task['status']} 不可确认；"
                        "只允许尚未开放执行的 RECEIVED 票")
                country = self.references.resolve(item["country"])
                if country is None:
                    raise ValidationError(
                        f"国家 {item['country']!r} 不可用（未知、已停用或别名已停用）")
                alpha3 = str(country["alpha3"])
                country_row = self.references.get_country(alpha3)
                if country_row is None or not country_row["enabled"]:
                    raise ValidationError(
                        f"国家 {alpha3} 已停用，不能作为目的国")
                current_revision = int(conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM task_events "
                    "WHERE task_id = ? AND code = ?",
                    (task_id, DESTINATION_EVENT_CODE),
                ).fetchone()[0])
                if current_revision != item["expected_revision"]:
                    raise ValidationError(
                        f"任务 {task_id} 的目的国确认修订已过期"
                        f"（当前 {current_revision}，表单 "
                        f"{item['expected_revision']}），请刷新后重试")
                resolved.append({
                    "item": item,
                    "task": dict(task),
                    "candidate_key": str(link["candidate_key"]),
                    "country": country,
                })

            # 第二遍：全部合法后才写所选票；同值无操作。
            now = utc_now()
            event_details: list[dict[str, Any]] = []
            for entry in resolved:
                item = entry["item"]
                task = entry["task"]
                country = entry["country"]
                task_id = str(task["id"])
                name = str(country["name_zh"])
                alpha3 = str(country["alpha3"])
                previous_name = task.get("destination_name")
                previous_alpha3 = task.get("destination_alpha3")
                previous_revision = item["expected_revision"]
                if str(previous_alpha3 or "") == alpha3 and \
                        str(previous_name or "") == name:
                    unchanged_ids.append(task_id)
                    changes.append({
                        "task_id": task_id,
                        "candidate_key": entry["candidate_key"],
                        "changed": False,
                        "previous_revision": previous_revision,
                        "revision": previous_revision,
                        "previous_destination_name": previous_name,
                        "previous_destination_alpha3": previous_alpha3,
                        "destination_name": name,
                        "destination_alpha3": alpha3,
                        "requested_country": item["country"],
                    })
                    continue

                version = int(task["version"])
                conn.execute(
                    "UPDATE tasks SET destination_name = ?, "
                    "destination_alpha3 = ?, updated_at = ? WHERE id = ?",
                    (name, alpha3, now, task_id))
                version_update = conn.execute(
                    "UPDATE task_versions SET destination_name = ?, "
                    "destination_alpha3 = ? WHERE task_id = ? AND version = ?",
                    (name, alpha3, task_id, version))
                if version_update.rowcount != 1:
                    raise ValidationError(
                        f"任务 {task_id} 的当前版本不存在，已整体回滚")

                detail = {
                    "batch_id": batch_id,
                    "plan_id": plan_id,
                    "requested_country": item["country"],
                    "previous_destination_name": previous_name,
                    "previous_destination_alpha3": previous_alpha3,
                    "destination_name": name,
                    "destination_alpha3": alpha3,
                    "before": {
                        "destination_name": previous_name,
                        "destination_alpha3": previous_alpha3,
                    },
                    "after": {
                        "destination_name": name,
                        "destination_alpha3": alpha3,
                    },
                    "reference_version": recorded_reference,
                    "reason": reason,
                    "expected_revision": previous_revision,
                    "selection_scope": [i["task_id"] for i in items],
                }
                event = conn.execute(
                    """
                    INSERT INTO task_events (
                        task_id, version, stage, from_status, to_status,
                        code, detail_json, actor, created_at
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        version,
                        DESTINATION_EVENT_STAGE,
                        DESTINATION_EVENT_CODE,
                        json.dumps(detail, ensure_ascii=False),
                        actor,
                        now,
                    ),
                )
                revision = int(event.lastrowid)
                changed_ids.append(task_id)
                event_details.append({
                    **detail,
                    "task_id": task_id,
                    "version": version,
                    "revision": revision,
                })
                changes.append({
                    "task_id": task_id,
                    "candidate_key": entry["candidate_key"],
                    "changed": True,
                    "previous_revision": previous_revision,
                    "revision": revision,
                    "previous_destination_name": previous_name,
                    "previous_destination_alpha3": previous_alpha3,
                    "destination_name": name,
                    "destination_alpha3": alpha3,
                    "requested_country": item["country"],
                })

            if changed_ids:
                # 单条批次审计承载本次选择范围与逐票前后值；逐票细节同时
                # 保存在各自 task_events。
                conn.execute(
                    """
                    INSERT INTO batch_events (
                        batch_id, plan_id, action, actor, detail_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        plan_id,
                        DESTINATION_EVENT_CODE,
                        actor,
                        json.dumps({
                            "reason": reason,
                            "reference_version": recorded_reference,
                            "selected_task_ids": [i["task_id"] for i in items],
                            "changed_task_ids": changed_ids,
                            "updates": event_details,
                        }, ensure_ascii=False),
                        now,
                    ),
                )

        result = self.get_batch_destinations(batch_id)
        result["changes"] = changes
        result["changed_task_ids"] = changed_ids
        result["updated_task_ids"] = list(changed_ids)
        result["unchanged_task_ids"] = unchanged_ids
        self.logger.info(
            "batch_destinations_confirmed",
            batch_id=batch_id,
            plan_id=plan_id,
            actor=actor,
            changed=len(changed_ids),
            unchanged=len(unchanged_ids),
            reference_version=recorded_reference,
        )
        return result
