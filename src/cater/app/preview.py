"""离线多票分票预览服务（阶段 1，plan/tasks/claude-multi-ticket-preview.md）。

输入本地 .eml（及可选显式附件路径），输出核心 ``inspect_sources`` 的
JSON 预览。刻意不经过 AppService：

- 不加载应用配置 / .env / 钥匙串（安全上限在此处按 MailConfig 默认值
  硬编码，避免任何配置读取）；
- 不初始化数据库、不跑迁移、不创建任务、不写外发箱、不动 IMAP 游标；
- 不联网、不收发邮件、不生成模板；
- 附件落盘到自动清理的隔离临时目录，文件名经 ``safe_filename`` 清洗并
  加内容哈希前缀，杜绝路径穿越与重名覆盖；输入文件只读。
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from .engine import load_inspect_sources
from .mail.message import parse_mail
from .util import safe_filename

# 与 config.MailConfig 默认值一致的安全上限（离线路径不读取应用配置）。
MAX_MESSAGE_BYTES = 60 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_SOURCE_COUNT = 64


class PreviewError(RuntimeError):
    """预览输入层面的可预期错误（缺文件、超限等）。"""


def _limit_issue(code: str, message: str, filename: str) -> dict:
    return {
        "code": code,
        "message": message,
        "record_key": None,
        "field": None,
        "sources": [{"type": "workbook", "filename": filename}],
        "required_action": "请压缩或拆分附件后重试",
        "blocking": True,
    }


def _stage_attachment(directory: Path, data: bytes, original: str,
                      sha256: str) -> dict:
    """附件落盘到隔离临时目录：哈希前缀 + 清洗名，防穿越/防覆盖。"""
    name = safe_filename(original, fallback=f"attachment-{sha256[:12]}.bin")
    dest = directory / f"{sha256[:16]}-{name}"
    dest.write_bytes(data)
    return {"path": str(dest), "filename": original}


def preview_eml(eml_path: Any, *,
                extra_sources: Iterable[Any] = (),
                header_aliases: Optional[dict] = None) -> dict:
    """解析本地 .eml 并返回分票预览 dict（JSON 可序列化）。

    超限/损坏附件不阻断整批：转为顶层阻断问题并标记 batch_ready=False，
    其余附件照常识别。
    """
    path = Path(eml_path)
    if not path.is_file():
        raise PreviewError(f"邮件文件不存在或不是普通文件：{eml_path}")
    size = path.stat().st_size
    if size > MAX_MESSAGE_BYTES:
        raise PreviewError(
            f"邮件超过 {MAX_MESSAGE_BYTES // (1024 * 1024)}MB 上限（{size} 字节）")
    raw = path.read_bytes()
    mail = parse_mail(raw, max_attachment_bytes=MAX_ATTACHMENT_BYTES)

    limit_issues: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="cater-preview-") as tmp:
        stage_dir = Path(tmp)
        sources: list[dict] = []
        total = 0
        for att in mail.attachments:
            if att.is_inline:
                continue  # 内嵌图片等不参与分票
            if len(sources) >= MAX_SOURCE_COUNT:
                limit_issues.append(_limit_issue(
                    "ATTACHMENT_COUNT_EXCEEDED",
                    f"附件数量超过 {MAX_SOURCE_COUNT} 上限，{att.filename} 未纳入预览",
                    att.filename))
                continue
            if att.size > MAX_ATTACHMENT_BYTES:
                limit_issues.append(_limit_issue(
                    "ATTACHMENT_TOO_LARGE",
                    f"附件 {att.filename} 超过单附件 "
                    f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 上限，未纳入预览",
                    att.filename))
                continue
            if total + att.size > MAX_TOTAL_ATTACHMENT_BYTES:
                limit_issues.append(_limit_issue(
                    "ATTACHMENTS_TOTAL_TOO_LARGE",
                    f"附件累计超过 {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)}MB "
                    f"上限，{att.filename} 未纳入预览",
                    att.filename))
                continue
            total += att.size
            sources.append(_stage_attachment(stage_dir, att.data,
                                             att.filename, att.sha256))
        for extra in extra_sources:
            epath = Path(extra)
            if not epath.is_file():
                raise PreviewError(f"显式附件不存在或不是普通文件：{extra}")
            if len(sources) >= MAX_SOURCE_COUNT:
                limit_issues.append(_limit_issue(
                    "ATTACHMENT_COUNT_EXCEEDED",
                    f"附件数量超过 {MAX_SOURCE_COUNT} 上限，{epath.name} 未纳入预览",
                    epath.name))
                continue
            esize = epath.stat().st_size
            if esize > MAX_ATTACHMENT_BYTES:
                limit_issues.append(_limit_issue(
                    "ATTACHMENT_TOO_LARGE",
                    f"附件 {epath.name} 超过单附件上限，未纳入预览", epath.name))
                continue
            if total + esize > MAX_TOTAL_ATTACHMENT_BYTES:
                limit_issues.append(_limit_issue(
                    "ATTACHMENTS_TOTAL_TOO_LARGE",
                    f"附件累计超过上限，{epath.name} 未纳入预览", epath.name))
                continue
            data = epath.read_bytes()
            total += esize
            sources.append(_stage_attachment(stage_dir, data, epath.name,
                                             hashlib.sha256(data).hexdigest()))
        for warning in mail.parse_warnings:
            if warning.startswith("attachment_too_large:"):
                name = warning.split(":", 1)[1]
                if not any(i["sources"][0]["filename"] == name
                           for i in limit_issues):
                    limit_issues.append(_limit_issue(
                        "ATTACHMENT_TOO_LARGE",
                        f"附件 {name} 超过单附件上限，未纳入预览", name))

        request: dict[str, Any] = {
            "sources": sources,
            "mail": {
                "html": mail.html_body or "",
                "text": mail.text_body or "",
                "subject": mail.subject or "",
                "message_id": mail.message_id,
            },
        }
        if header_aliases:
            request["header_aliases"] = header_aliases
        inspect_sources = load_inspect_sources()
        preview = inspect_sources(request)
    if limit_issues:
        preview["issues"].extend(limit_issues)
        preview["batch_ready"] = False
    return preview
