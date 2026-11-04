from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cater.app.errors import ValidationError
from cater.app.mail.message import html_to_text, parse_mail
from cater.app.mail.msg import MsgParseError, parse_msg
from helpers import FakeEngine, make_mail, make_config
from cater.app.application import AppService
from cater.app.mail.simulator import SimulatorMailGateway

REPO_ROOT = Path(__file__).resolve().parents[2]
MSG_SAMPLE = (
    REPO_ROOT
    / "input"
    / "整机"
    / "回复 Re 印尼 ATL2607022K JM28 2X512+2X520 BFWVN-VW 2X40FR 2X40FR TO JAKARTA ETDAUG 16  放舱+更改运费  AMQ0499914.msg"
)


def test_parse_plain_html_and_attachment_metadata() -> None:
    raw = make_mail(
        subject="配件",
        text="目的国：法国",
        html="<table><tr><td>型号</td><td>SEM512</td></tr></table>",
        attachments=(("明细.xlsx", b"xlsx-data"), ("图片.png", b"png")),
    )
    parsed = parse_mail(raw)
    assert parsed.sender == "tester@example.com"
    assert parsed.sender_name == "Tester"
    assert parsed.subject == "配件"
    assert "目的国：法国" in parsed.text_body
    assert "SEM512" in parsed.html_body
    assert [item.filename for item in parsed.attachments] == ["明细.xlsx", "图片.png"]
    assert parsed.attachments[0].sha256 == hashlib.sha256(b"xlsx-data").hexdigest()
    assert parsed.is_auto is False
    assert parsed.is_bounce is False


def test_html_to_text_keeps_cell_boundaries() -> None:
    text = html_to_text(
        "<p>目的国：法国</p><table><tr><td>BFWVN</td><td>JM28</td></tr></table>"
    )
    assert "目的国：法国" in text
    assert "BFWVN" in text and "JM28" in text


@pytest.mark.parametrize(
    ("raw", "attribute"),
    [
        (make_mail(auto_submitted="auto-replied"), "is_auto"),
        (make_mail(subject="Automatic reply: 配件"), "is_auto"),
        (make_mail(subject="退信：投递失败"), "is_bounce"),
        (make_mail(sender="mailer-daemon@example.com"), "is_bounce"),
    ],
)
def test_automated_and_bounce_mail_detected(raw: bytes, attribute: str) -> None:
    parsed = parse_mail(raw)
    assert getattr(parsed, attribute) is True


def test_parse_msg_real_outlook_sample() -> None:
    if not MSG_SAMPLE.is_file():
        pytest.skip("sample .msg not present")
    pytest.importorskip("extract_msg")
    parsed = parse_msg(MSG_SAMPLE)
    assert parsed.sender == "liu_haoming@cat.com"
    assert "ATL2607022K" in parsed.subject
    assert "Cust. Purch. Order" in parsed.text_body
    xlsx = [item for item in parsed.attachments if item.suffix == "xlsx"]
    assert xlsx and xlsx[0].size > 1000
    assert parsed.parse_warnings == []


def test_malformed_msg_is_rejected_before_task_creation(
    config, tmp_path: Path
) -> None:
    pytest.importorskip("extract_msg")
    bad = tmp_path / "bad.msg"
    bad.write_bytes(b"not-an-ole-file")
    service = AppService(
        config, engine=FakeEngine(), gateway=SimulatorMailGateway()
    )
    try:
        with pytest.raises((ValidationError, MsgParseError)):
            service.submit_offline_sources(
                sender="tester@example.com",
                destination="法国",
                source_paths=[bad],
            )
        assert service.tasks.list() == []
    finally:
        service.close()


def test_offline_msg_extracts_sources_for_core(tmp_path: Path) -> None:
    if not MSG_SAMPLE.is_file():
        pytest.skip("sample .msg not present")
    config = make_config(
        tmp_path,
        test_addresses=("liu_haoming@cat.com",),
    )
    engine = FakeEngine()
    service = AppService(
        config, engine=engine, gateway=SimulatorMailGateway()
    )
    try:
        service.submit_offline_sources(
            sender="liu_haoming@cat.com",
            destination="印尼",
            source_paths=[MSG_SAMPLE],
            kind="auto",
        )
        request = engine.calls[0]
        # Only the workbook attachment is passed to the core; the .msg is audit
        # evidence and its body supplied separately.
        assert len(request["sources"]) == 1
        assert request["sources"][0]["filename"].endswith(".xlsx")
        assert "Cust. Purch. Order" in request["mail"]["html"]
        inputs = service.tasks.inputs(service.tasks.list()[0]["id"], 1)
        assert {item["source_type"] for item in inputs} == {
            "offline_original",
            "offline_attachment",
        }
    finally:
        service.close()