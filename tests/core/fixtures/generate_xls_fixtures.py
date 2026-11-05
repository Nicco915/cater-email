"""生成 tests/core/fixtures/*.xls 合成样例（开发一次性使用，pytest 不运行本脚本）。

用法：python3 tests/core/fixtures/generate_xls_fixtures.py
依赖：xlwt（仅生成夹具用，运行期与项目均不依赖；xlrd 为项目运行依赖）。

所有数据均为合成/去敏值（FAKE MSO、SYN 发票号），不包含真实商业资料。
"""
from pathlib import Path

import xlwt

HERE = Path(__file__).parent


def _write(ws, row1: int, col1: int, value):
    """1 基坐标写入。"""
    if value is None:
        return
    ws.write(row1 - 1, col1 - 1, value)


def build_flexible_machine(path: Path) -> None:
    """多结构整机样例（模仿 2X656D BIHWE-WF.xls 的布局结构，数据全合成）：
    - CI 表：机器信息表头位于第 56 行（深表头），SERIAL/PIN/ENGINE 列位不同；
    - PL 表：双行表头（TOTAL WEIGHT/KGS + GROSS/NET 子表头），
      描述采用 "CMOPS MSO#: FAKE001/FAKE002" 形式；
    - Sheet2：含另一张“机器信息表”（历史数据，FAKEH1），必须被排除。
    """
    book = xlwt.Workbook()

    ci = book.add_sheet("CI")
    _write(ci, 9, 12, "INVOICE NO.: SYN-TEST-0001")
    _write(ci, 34, 3, "CMOPS MSO#: FAKE001/FAKE002\nSEMTEST WHEEL LOADER")
    _write(ci, 34, 13, 47141.75)   # 发票单价：绝不可冒充 Total Sales Price
    _write(ci, 35, 1, "INLAND  FREIGHT CHARGE")
    _write(ci, 35, 13, 800.0)
    _write(ci, 36, 1, "SEA FREIGHT CHARGE")
    _write(ci, 36, 13, 4343.0)
    _write(ci, 53, 1, "NET WEIGHT: 24000 KG")
    _write(ci, 55, 1, "MACHINE INFORMATION:")
    for col, title in ((1, "ITEM#"), (2, "MODEL"), (5, "SERIAL #"),
                       (7, "ENGINE #"), (11, "PIN #"), (15, "MSO")):
        _write(ci, 56, col, title)
    _write(ci, 57, 1, 1.0)
    _write(ci, 57, 2, "SEMTEST")
    _write(ci, 57, 5, "SYN000001A")
    _write(ci, 57, 7, "SYNENG001")
    _write(ci, 57, 11, "SYNPIN0000001")
    _write(ci, 57, 15, "FAKE001")
    _write(ci, 58, 1, 2.0)
    _write(ci, 58, 2, "SEMTEST")
    _write(ci, 58, 5, "SYN000002B")
    _write(ci, 58, 7, "SYNENG002")
    _write(ci, 58, 11, "SYNPIN0000002")
    _write(ci, 58, 15, "FAKE002")

    pl = book.add_sheet("PL")
    _write(pl, 9, 14, "INVOICE NO.: SYN-TEST-0001")
    _write(pl, 28, 1, "DESCRIPTION OF GOODS")
    _write(pl, 28, 13, "QTY")
    _write(pl, 28, 16, "TOTAL WEIGHT/KGS")
    _write(pl, 28, 21, "DIMENSION")
    _write(pl, 29, 16, "GROSS")
    _write(pl, 29, 19, "NET")
    _write(pl, 30, 1, "CMOPS MSO#: FAKE001/FAKE002\nSEMTEST WHEEL LOADER\nSN: SYN000001A/SYN000002B")
    _write(pl, 30, 13, 2.0)
    _write(pl, 30, 14, "UNITS")
    _write(pl, 30, 16, 24000.0)
    _write(pl, 30, 19, 24000.0)
    _write(pl, 31, 1, "TOTAL")
    _write(pl, 31, 13, 2.0)
    _write(pl, 31, 16, 24000.0)
    _write(pl, 31, 19, 24000.0)

    sheet2 = book.add_sheet("Sheet2")
    _write(sheet2, 3, 1, "HISTORICAL RECORD (must be ignored)")
    for col, title in ((1, "ITEM#"), (2, "MODEL"), (3, "MSO#"), (4, "LOADER #")):
        _write(sheet2, 5, col, title)
    _write(sheet2, 6, 1, 1.0)
    _write(sheet2, 6, 2, "SEMOLD")
    _write(sheet2, 6, 3, "FAKEH1")
    _write(sheet2, 6, 4, "SYNH00001")

    bl = book.add_sheet("BL ")
    _write(bl, 2, 1, "BILL OF LADING (historical, must be ignored)")
    _write(bl, 4, 1, "MODEL")
    _write(bl, 4, 2, "QTY")
    _write(bl, 5, 1, "SEMOLD")
    _write(bl, 5, 2, 9.0)

    book.save(str(path))


if __name__ == "__main__":
    build_flexible_machine(HERE / "flexible_machine.xls")
    print("fixtures written to", HERE)
