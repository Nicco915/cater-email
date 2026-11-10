"""preview-tickets CLI 与离线预览服务测试。

验证：真正调用核心 inspect_sources；绝不触碰数据库/配置/.env/任务/外发箱/
游标；绝不联网；输入文件只读；输出覆盖语义显式；超限附件可见且阻断批次。
"""
from __future__ import annotations

import hashlib
import io
import json
import socket
from pathlib import Path

import openpyxl
import pytest

from cater.app import cli
from cater.app import preview as preview_mod
from cater.app.preview import PreviewError, preview_eml

from helpers import make_mail


def _machine_workbook_bytes(invoice_no: str = "INV-P-001") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.cell(1, 1, f"INVOICE NO.: {invoice_no}")
    ws.cell(3, 1, "MODEL")
    ws.cell(3, 2, "MSO#")
    ws.cell(3, 3, "SERIAL#")
    ws.cell(4, 1, "TESTMODEL")
    ws.cell(4, 2, "MSOP1")
    ws.cell(4, 3, "S002001")
    ws.cell(6, 1, "TOTAL AMOUNT (IN WORDS): SAY USD ONLY")
    ws.cell(6, 7, 1000)
    pl = wb.create_sheet("Packing list")
    pl.cell(2, 1, "DESCRIPTION OF GOODS")
    pl.cell(2, 2, "QTY")
    pl.cell(2, 3, "NET WEIGHT (KGS)")
    pl.cell(2, 4, "GROSS WEIGHT (KGS)")
    pl.cell(3, 1, "MACHINE")
    pl.cell(3, 2, 1)
    pl.cell(3, 3, 100)
    pl.cell(3, 4, 110)
    pl.cell(4, 1, "TOTAL")
    pl.cell(4, 2, 1)
    pl.cell(4, 3, 100)
    pl.cell(4, 4, 110)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def eml_file(tmp_path: Path) -> Path:
    raw = make_mail(
        subject="preview test",
        text="see attached",
        attachments=(("invoice-a.xlsx", _machine_workbook_bytes()),),
    )
    path = tmp_path / "sample.eml"
    path.write_bytes(raw)
    return path


def test_service_returns_core_preview(eml_file: Path):
    result = preview_eml(eml_file)
    assert result["schema_version"] == 1
    assert result["candidate_count"] == 1
    cand = result["candidates"][0]
    assert cand["kind"] == "machine"
    assert cand["identity"]["invoice_no"] == ["INV-P-001"]
    assert cand["identity"]["mso"] == ["MSOP1"]
    assert cand["facts"]["amount"]["value"] == "1000"
    assert result["batch_ready"] is True
    # 无秘密字段：指纹仅由内容哈希构成
    assert set(result["source_fingerprint"]) <= set("0123456789abcdef")


def test_missing_eml_rejected(tmp_path: Path):
    with pytest.raises(PreviewError):
        preview_eml(tmp_path / "missing.eml")


def test_oversize_attachment_visible_and_blocks(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(preview_mod, "MAX_ATTACHMENT_BYTES", 64)
    raw = make_mail(
        subject="big",
        text="",
        attachments=(("big.xlsx", _machine_workbook_bytes()),),
    )
    path = tmp_path / "big.eml"
    path.write_bytes(raw)
    result = preview_eml(path)
    assert result["candidate_count"] == 0
    assert result["batch_ready"] is False
    assert any(i["code"] == "ATTACHMENT_TOO_LARGE" for i in result["issues"])


def test_path_traversal_filename_sanitized(tmp_path: Path):
    raw = make_mail(
        subject="traversal",
        text="",
        attachments=(("../../../evil.xlsx", _machine_workbook_bytes()),),
    )
    path = tmp_path / "trav.eml"
    path.write_bytes(raw)
    result = preview_eml(path)
    # 不穿越、不报错：附件被纳入识别（落盘名已清洗，原件文件名保留溯源）
    assert result["candidate_count"] == 1
    assert not (tmp_path.parent / "evil.xlsx").exists()


def test_cli_smoke_stdout_json(eml_file: Path, capsys):
    rc = cli.main(["preview-tickets", "--eml", str(eml_file)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["identity"]["invoice_no"] == ["INV-P-001"]


def test_cli_never_touches_config_or_db(eml_file: Path, monkeypatch, tmp_path: Path,
                                        capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("preview-tickets must not load config/AppService")

    monkeypatch.setattr(cli, "load_config", _boom)
    monkeypatch.setattr(cli, "AppService", _boom)
    monkeypatch.chdir(tmp_path)  # 空目录：若建库必留痕
    rc = cli.main(["preview-tickets", "--eml", str(eml_file)])
    assert rc == 0
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == [eml_file]


def test_cli_never_opens_network(eml_file: Path, monkeypatch, capsys):
    def _no_net(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_net)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    rc = cli.main(["preview-tickets", "--eml", str(eml_file)])
    assert rc == 0
    capsys.readouterr()


def test_cli_input_file_untouched(eml_file: Path, capsys):
    before = hashlib.sha256(eml_file.read_bytes()).hexdigest()
    rc = cli.main(["preview-tickets", "--eml", str(eml_file)])
    assert rc == 0
    capsys.readouterr()
    assert hashlib.sha256(eml_file.read_bytes()).hexdigest() == before


def test_cli_output_overwrite_semantics(eml_file: Path, tmp_path: Path, capsys):
    out = tmp_path / "preview.json"
    rc = cli.main(["preview-tickets", "--eml", str(eml_file),
                   "--output", str(out)])
    assert rc == 0
    first = out.read_text(encoding="utf-8")
    assert json.loads(first)["candidate_count"] == 1
    # 已存在且无 --overwrite：拒绝且不改动
    rc = cli.main(["preview-tickets", "--eml", str(eml_file),
                   "--output", str(out)])
    assert rc == 1
    assert out.read_text(encoding="utf-8") == first
    # 显式 --overwrite 才替换
    rc = cli.main(["preview-tickets", "--eml", str(eml_file),
                   "--output", str(out), "--overwrite"])
    assert rc == 0
    capsys.readouterr()


def test_cli_output_must_not_clobber_input(eml_file: Path, capsys):
    before = eml_file.read_bytes()
    rc = cli.main(["preview-tickets", "--eml", str(eml_file),
                   "--output", str(eml_file), "--overwrite"])
    assert rc == 1
    assert eml_file.read_bytes() == before
    capsys.readouterr()


def test_cli_missing_eml_exit_code(tmp_path: Path, capsys):
    rc = cli.main(["preview-tickets", "--eml", str(tmp_path / "nope.eml")])
    assert rc == 1
    capsys.readouterr()
