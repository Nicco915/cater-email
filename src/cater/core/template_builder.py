"""从 output 样例构建受控模板（一次性/模板版本升级时使用）。

OOXML 级清理（template_ooxml.build_template，审核修正版）：
- 配件台账：保留 导入模板；清除第 6 行起全部旧数据（样式/行高/列宽保留）；
- 企业物料：保留 企业物料；删除手工中间 Sheet2 与历史 Sheet1 的必要结构
  （worksheet 部件、其 rels 与专属 printerSettings、workbook/rels/content-types
  对应项）；清除第 6 行起旧数据；
- 沟通表：保留 Sheet1；删除手工中间 Sheet2；清除第 3 行起旧数据及下方散落残留
  （含看似空白的空格单元格，如样例 G10）。

保真承诺：保留表的 printerSettings、批注（comments1.xml + vmlDrawing1.vml）、
styles、theme、docProps 以原始字节保留；sharedStrings 仅保留保留表引用的
<si>（原字节片段、原相对顺序、索引重映射），被删历史 sheet 数据不残留。
原始样例只读；构建后回读校验表头与数据区，并输出实际差异清单
（docs/template-audit.md §3/§4，tests/core/test_template_fidelity.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl

from .template_ooxml import build_template

REPO_ROOT = Path(__file__).resolve().parents[3]

BUILDS = [
    {
        "source": "output/配件/出口台账表体.xlsx",
        "target": "templates/parts_ledger.xlsx",
        "keep_sheets": ["导入模板"],
        "clear": {"导入模板": 6},       # 从第 6 行起清除值
        "prototype_rows": 15,
    },
    {
        "source": "output/整机/企业物料 上传.xlsx",
        "target": "templates/machine_materials.xlsx",
        "keep_sheets": ["企业物料"],
        "clear": {"企业物料": 6},
        "prototype_rows": 4,
    },
    {
        "source": "output/整机/SEM发运沟通表 上传.xlsx",
        "target": "templates/machine_comm.xlsx",
        "keep_sheets": ["Sheet1"],
        "clear": {"Sheet1": 3},
        "prototype_rows": 4,
    },
]


def build_templates(repo_root: Path = REPO_ROOT) -> list:
    built = []
    for spec in BUILDS:
        src = repo_root / spec["source"]
        dst = repo_root / spec["target"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        manifest = build_template(str(src), str(dst), spec["keep_sheets"], spec["clear"])
        _verify_built(src, dst, spec)
        built.append((str(dst), manifest))
    return built


def _verify_built(src: Path, dst: Path, spec: dict) -> None:
    wb_src = openpyxl.load_workbook(src)
    wb_dst = openpyxl.load_workbook(dst)
    if wb_dst.sheetnames != spec["keep_sheets"]:
        raise RuntimeError(f"模板工作表不符：{wb_dst.sheetnames} != {spec['keep_sheets']}")
    for sheet_name, first_data_row in spec["clear"].items():
        ws_src, ws_dst = wb_src[sheet_name], wb_dst[sheet_name]
        # 表头行值一致
        for r in range(1, first_data_row):
            for c in range(1, ws_src.max_column + 1):
                if ws_src.cell(r, c).value != ws_dst.cell(r, c).value:
                    raise RuntimeError(f"模板表头不一致：{sheet_name}!{ws_src.cell(r, c).coordinate}")
        # 数据区无残留
        for r in range(first_data_row, ws_dst.max_row + 1):
            for c in range(1, ws_dst.max_column + 1):
                v = ws_dst.cell(r, c).value
                if v not in (None, "") and not (isinstance(v, str) and not v.strip()):
                    raise RuntimeError(f"模板数据区残留：{sheet_name}!{ws_dst.cell(r, c).coordinate}={v!r}")


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT
    for path, manifest in build_templates(root):
        print("built:", path)
        print("  changed:", manifest["changed"])
        print("  removed:", manifest["removed"])
