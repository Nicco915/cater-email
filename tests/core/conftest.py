"""tests/core 公共夹具。

直接读取仓库 input/ 与 database/ 原始样例（只读，不修改）；
所有输出写入 pytest 临时目录。
"""
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

INPUT_PARTS = REPO_ROOT / "input" / "配件" / "1x920_BEECS-6613427.xlsx"
INPUT_MACHINE_XLSX = REPO_ROOT / "input" / "整机" / "Invoice+Packing list ----2X512+2X520 BFWVN-VW.xlsx"
MAIL_HTML_FIXTURE = Path(__file__).parent / "fixtures" / "machine_mail.html"
DB_REFERENCE = REPO_ROOT / "database" / "整机HS CODE Review 2026-03.xlsx"
TEMPLATE_DIR = REPO_ROOT / "templates"

MESSAGE_ID = "<BN0PR08MB753440A51EF5B4EFA32346CD87DC2@BN0PR08MB7534.namprd08.prod.outlook.com>"

# 数据库 Sheet1 中 SEM512/SEM520 的参考条目（生产导入行为由
# test_reference_import 单独验证，含 915F 双 scope 变体）
MACHINE_REFERENCES = [
    {
        "model": "SEM512",
        "english_name": "SEM512 SOIL COMPACTOR",
        "hs_code": "8429401900",
        "product_name": "压路机",
        "declaration": "1|0|利用机械自重、振动的方法，对被压实材料重复加载，克服材料之间的黏聚力和内摩擦力，除其内部的气体和水分，迫使材料颗粒之间产生位移，相互楔紧，增加密实度，使之达到一定的密实度和平整度|是自推进|12200KG|山工品牌、SEM品牌|SEM512|无|无|无",
        "destination_scope": "default",
    },
    {
        "model": "SEM520",
        "english_name": "SEM520 SOIL COMPACTOR",
        "hs_code": "8429401100",
        "product_name": "压路机",
        "declaration": "1|0|利用机械自重、振动的方法，对被压实材料重复加载，克服材料之间的黏聚力和内摩擦力，除其内部的气体和水分，迫使材料颗粒之间产生位移，相互楔紧，增加密实度，使之达到一定的密实度和平整度|是自推进|19600KG(机重≥18吨)|山工品牌、SEM品牌|SEM520|无|无|无",
        "destination_scope": "default",
    },
]


@pytest.fixture()
def output_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return str(d)


@pytest.fixture()
def parts_request(output_dir):
    return {
        "task_id": "TEST-PARTS-1",
        "kind": "parts",
        "destination": {"name": "多哥", "alpha3": "TGO"},
        "sources": [{"path": str(INPUT_PARTS), "filename": INPUT_PARTS.name,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
        "mail": {},
        "references": {"machines": [], "countries": [], "machine_aliases": {}},
        "output_dir": output_dir,
        "overrides": [],
    }


@pytest.fixture()
def machine_request(output_dir):
    return {
        "task_id": "TEST-MACHINE-1",
        "kind": "machine",
        "destination": {"name": "印度尼西亚", "alpha3": "IDN"},
        "sources": [{"path": str(INPUT_MACHINE_XLSX), "filename": INPUT_MACHINE_XLSX.name,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
        "mail": {"html": MAIL_HTML_FIXTURE.read_text(encoding="utf-8", errors="replace"),
                 "text": None, "message_id": MESSAGE_ID},
        "references": {"machines": MACHINE_REFERENCES, "countries": [], "machine_aliases": {}},
        "output_dir": output_dir,
        "overrides": [],
    }
