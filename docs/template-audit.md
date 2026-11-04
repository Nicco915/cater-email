# 模板清理清单与保真验证报告

> 日期：2026-11-04（审核修正版）。模板构建：`PYTHONPATH=src python -m cater.core.template_builder`
> （从 output/ 样例生成，原始样例不改动）。

## 1. 模板清单

| 模板 | 来源样例 | 保留工作表 | 删除工作表 | 清理范围 |
|---|---|---|---|---|
| templates/parts_ledger.xlsx | output/配件/出口台账表体.xlsx | 导入模板 | 无 | 第 6～20 行旧数据（15 行原型保留样式），含 Z16 残留订单号 |
| templates/machine_materials.xlsx | output/整机/企业物料 上传.xlsx | 企业物料 | Sheet2（手工中间表）、Sheet1（历史数据） | 第 6～9 行旧数据 |
| templates/machine_comm.xlsx | output/整机/SEM发运沟通表 上传.xlsx | Sheet1 | Sheet2（手工中间表） | 第 3～6 行旧数据及下方散落残留（含 G10 空格单元格） |

说明行（台账/企业物料第 1～5 行、沟通表第 1～2 行）全部保留，不删除。

## 2. 模板契约（与 plan.md §13 一致）

| 输出文件 | 保留工作表 | 保护行 | 数据起始行 | 样例行数 |
|---|---|---|---|---|
| 出口台账表体 | 导入模板 | 1～5 | 6 | 15 |
| 企业物料 | 企业物料 | 1～5 | 6 | 4 |
| SEM发运沟通表 | Sheet1 | 1～2 | 3 | 4 |

## 3. 模板构建阶段：OOXML 限定清理（审核修正版，2026-11-04）

模板构建不再使用 openpyxl 全量另存，改为 zip/XML 级限定清理
（`src/cater/core/template_ooxml.py`）。原始样例 → 模板的**实际差异清单**
（测试锁定，见 tests/core/test_template_fidelity.py）：

| 模板 | 改动部件（必要结构与数据清理） | 移除部件（被删 sheet 专属） |
|---|---|---|
| parts_ledger.xlsx | sheet1.xml（清数据值）、sharedStrings.xml（仅保留保留表引用的条目） | 无 |
| machine_materials.xlsx | sheet1.xml、sharedStrings.xml、workbook.xml、workbook.xml.rels、[Content_Types].xml | sheet2.xml、sheet3.xml、sheet2 rels、printerSettings2.bin |
| machine_comm.xlsx | sheet1.xml、sharedStrings.xml、workbook.xml、workbook.xml.rels、[Content_Types].xml | sheet2.xml、sheet2 rels、printerSettings2.bin |

**原字节保留**（逐部件 sha256 比对测试锁定）：保留表的
`printerSettings1.bin`、`comments1.xml`、`vmlDrawing1.vml`（沟通表批注）、
`styles.xml`、`theme1.xml`、`docProps/*`。

说明：

- sharedStrings 仅保留保留表仍引用的 `<si>`：每个 `<si>` 为原字节片段、
  相对顺序不变、索引重映射；被删历史/中间 sheet 的字符串不残留
  （如 GMMCO LIMITED、土库曼斯坦 等已验证不存在）。
- workbook.xml 仅删除被删 sheet 的 `<sheet>` 项与其 definedName；
  autofilter、mergeCells、pageSetup 的打印机引用（r:id）原样保留。
- 早前 openpyxl 另存导致的 printerSettings 丢失、批注 VML/几何重写、
  sharedStrings 布局重写、docProps 时间戳变化，**已全部消除**。
- 已记录的非阻塞限制（协调者确认，无需改写）：`docProps/app.xml` 未重写，
  其 `TitlesOfParts` 仍列出被删 sheet 名。该部件属非格式文档元数据（不影响
  单元格样式、打印设置、批注与已批准的 sheet 删除），Excel 打开与读取均正常；
  如需彻底同步可在后续版本另行处理，不构成交付阻塞。

## 4. 输出写入阶段：OOXML 替换，未修改部件字节不变（审核修正）

审核意见：格式严格不变，不得自行判定丢失可接受。修复后输出写入不再经过
openpyxl 全量另存，改为 zip/XML 级替换（`src/cater/core/ooxml_writer.py`）：

- 仅重写目标工作表的 `xl/worksheets/sheetN.xml` 数据区行；包内**所有其他部件**
  （批注、styles、theme、docProps、[Content_Types].xml 等）以原始字节复制；
- 写入后回读校验（openpyxl）：表头不变、写入值正确、数据区外无残留；
- 另做逐部件 sha256 比对测试：输出与模板之间仅目标 sheet XML 一个部件不同。
  证据：`test_only_target_sheet_part_modified`（三份模板全路径）。

### 写入规则

- 仅写数据区白名单列（template_writer.TemplateSpec.columns）。
- **数值列按数值存储**（`<v>`：金额/数量/重量，Decimal → Excel 安全值 int/float）；
  **标识符列按文本存储**（inlineStr：SO、HS 编码、料号、MSO、币种、国家、名称、
  规格、申报要素、发票号、订单号）。白名单见 TemplateSpec.numeric_columns。
- 行数超过原型行数时，复制最后一个原型行（含其全部样式单元格）；
  未写到的原型行整行删除，旧值不残留。
- 零条有效记录不生成文件（TemplateWriteError）。
- autofilter 保持模板原样，不随数据行数扩张（与人工样例沟通表行为一致）。

## 5. 与人工样例的已确认差异（测试断言锁定）

| 位置 | 样例 | 本系统输出 | 依据 |
|---|---|---|---|
| 台账 Z16 订单号 | CBS20250423-219355A | 留空 | plan.md §5 用户决定 |
| 台账 J8 净重 | 2.8000000000000003 | 2.8（数值） | Decimal 5 位精度归一（plan.md §11） |
| 企业物料 D6/D7（SEM520 申报要素） | 20000KG（手工改写过） | 19600KG(机重≥18吨)（数据库原文） | plan.md §13 已确认预期差异 |

说明：旧版“数值格按文本写入、与样例类型不同”的差异已消除——审核修正后数值列
按数值存储，与人工样例的 int/float 类型一致。

## 6. 验证命令与证据

- 全量：`PYTHONPATH=src:tests/core python -m pytest tests/core -q`（81 passed）；
  整树含 app：`PYTHONPATH=src python -m pytest tests -q`（158 passed, 3 skipped，skip 为 pwsh 未安装）。
  - 模板构建保真白名单：test_template_fidelity.py（6 项，样例→模板→输出全链路）
  - 逐格一致：test_generated_matches_sample_cells、test_generated_machine_matches_sample_cells
  - 未修改部件字节一致：test_only_target_sheet_part_modified
  - 存储类型契约：test_storage_types_numeric_vs_text（数值列 int/float、标识符文本）
  - 样式保真：test_style_fidelity_header_and_data；扩展行样式：test_single_and_extended_rows
  - 批注保留：test_comm_comments_preserved
- Excel 实际打开验证：本环境无 Excel/Windows，以 openpyxl 回读 + zip 结构校验代替；
  建议在阶段 B/C 于 Windows 用 Excel 打开三份生成文件确认无修复提示、检查打印布局
  与超长申报要素。
