# 逐列字段映射表

> 版本：2026-11-04。依据 plan.md §5 已确认规则与 P0 样例核查（input/ 与 output/ 当前样例）。
> 2026-11-05 修订：列位自本版起为**语义定位**（规范化表头别名），上表字母
> 为旧样例的实际落位；新样例列位已移动（如配件最终表 数量=F、单价=H、
> 净重=K，箱单合计 件数=H、毛重=K；整机 CI 表头在第 56 行、PL 为双行表头）。
> 多结构输入、`.xls` 支持、核对规则 ID 与布局确认流见 docs/flexible-input.md。
> 实现位置：`src/cater/core/`（解析 → 核对 → 转换 → 模板填充）。
> 价格字段语义不可混同：沟通表 Machine Price 只取邮件业务表 Total Sales Price；
> 发票单价（Invoice U'PRICE）/ Net Value / Dealer Net Net$ 不参与任何输出。

## 1. 配件（一对一：一张台账）

数据来源：`CI-For Dealer (2)` 最终调整表（样例行 19～33，起止行按表头与料号识别，
禁止硬编码），`Packing List` 整票合计。

### 1.1 选表规则

| 规则 | 处理 |
|---|---|
| 存在唯一 `CI-For Dealer (n)` 且结构合格 | 选为最终表 |
| 结构合格校验 | 表头行含 B=Part Code、H=TOTAL QTY、I=UNIT PRICE；数据区存在 M/N/O 补充字段与价格 |
| 多个合格最终表 | `AMBIGUOUS_FINAL_SHEET`，暂停追问 |
| 仅原表 `CI-For Dealer` | `FINAL_SHEET_NOT_FOUND`，暂停（原表不能自动替代） |
| 最终表存在但缺补充字段/价格 | `FINAL_SHEET_INVALID`，暂停 |

### 1.2 台账列映射（模板 `导入模板`，第 6 行起写）

| 列 | 字段 | 来源 | 说明 |
|---|---|---|---|
| B 商品料号 | part_code | 最终表 B | 文本 |
| D 申报数量 | qty | 最终表 H | Decimal，最多 5 位小数 |
| G 币制 | currency | 固定 | `USD` |
| H 申报单价 | unit_price | 最终表 I | Decimal，最多 4 位小数；样例 15 个单价与最终表一致 |
| I 申报总价 | — | 留空 | 已确认仅填单价 |
| J 净重 | net_weight | 最终表 L | 直接取总净重，不除以数量；核对 H×K（容差 4 位小数），不一致 `WEIGHT_MISMATCH` |
| K 毛重 | gross_weight | Packing List TOTAL 行 J | 仅首条填写（样例 317） |
| L 件数 | packages | Packing List TOTAL 行 G | 仅首条填写（样例 2） |
| M 原产国 | coo | 最终表 E | 经可配置三位码映射（US→USA、CN→CHN）；未知 `UNKNOWN_COUNTRY` |
| N 最终目的国 | destination | 本次用户输入 | 目的国中文名/别名 → 三位码（样例 TGO） |
| T 商品编码 | hs_code | 最终表 M | 与 D 冲突以 M 为准，登记非阻塞提示 `HS_CODE_DVERSUS_M` |
| U 商品名称 | product_name | 最终表 N | |
| V 规格型号 | spec | 最终表 O | 仅开头 `1||`→`1|`（已确认两种变体 `1||3|`、`1||不适合进口报关`，各删开头一个空项）；已是 `1|` 的不动，中部连续竖线原样保留，不做通用 collapse |
| Z 订单号 | order_no | 留空 | 已确认本期留空，禁止复制旧值（样例残留 CBS20250423-219355A 不沿用） |
| AA 发票号 | invoice_no | 最终表 E10 区域 `INVOICE NO.:` | 仅首条填写（样例 CQLUIL260812HCB-BEECS） |
| 其他列 | — | 留空 | 未使用列保持空白，不插入审计列 |

### 1.3 数值精度与存储类型（审核修正）

- **金额/数量/重量按数值存储**（Decimal 量化后转 Excel 安全值：整数存 int、
  小数存 float）；**标识符按文本存储**：料号、SO、HS 编码、币种、国家、
  名称、规格型号、申报要素、发票号、订单号、MSO、机型。
- Decimal 量化：单价 4 位、净重/毛重/数量 5 位小数并去尾零；
  Excel 二进制尾差（如 `2.8000000000000003`）归一为 `2.8`（与旧样例的唯一数值差异）。
- 公式单元格只读缓存值；缓存缺失登记 `FORMULA_CACHE_MISSING`，不当零处理。

## 2. 整机（一对二：企业物料 + 沟通表）

数据来源：邮件正文业务表（非引用区）+ 附件 `Invoice` / `Packing list`。
按 MSO 关联；序列号辅助核对；禁止凭行号或仅凭机型拼接。

### 2.1 双来源核对

| 标准字段 | 邮件业务表列 | 工作簿列 | 冲突处理 |
|---|---|---|---|
| dealer_code | Dealer（JM28） | — | 单一来源 |
| dealer_order | Dealer Order | — | 单一来源 |
| order(SO) | Order | — | 单一来源 |
| material | Material | Invoice/箱单 MODEL | 两源不一致 `FIELD_CONFLICT` |
| serial_number | Serial Number | Invoice/箱单 LOADER # | 两源不一致 `FIELD_CONFLICT`；同序列号多 MSO `DUPLICATE_SERIAL` |
| total_sales_price | Total Sales Price | —（发票单价是不同字段，不比较） | 单一来源 |
| pin / engine | — | PIN# / ENGINE # | 仅 provenance，不写输出 |
| net_weight | — | 箱单组净重÷组数量 | 组 MSO 清单关联；数量不符 `WEIGHT_GROUP_QTY_MISMATCH`；除法超 5 位小数 `WEIGHT_PRECISION` |

- 仅一处有值取该值（互补不算缺失）；两处一致合并保留双来源；不一致列出两处原值与位置，整票暂停。
- 合并后仍缺必需字段（dealer_code/dealer_order/order/material/serial_number/total_sales_price/net_weight）→ `MISSING_FIELD`。
- 非引用区多版业务表内容不同 → `MULTIPLE_BUSINESS_TABLES`；引用区（blockquote/lark quote）旧版忽略（样例旧版 69,847.84 不采用）。
- 比较前仅做已确认规范化（千分位/货币符号清理、SO 补前导零、Material 显式别名标准化），不模糊匹配。
- **金额相等性（审核修正）**：`total_sales_price` 两来源相等仅当规范化后完全相同、
  或差异小于相对 1e-9（双精度浮点尾差量级）；分位级真实差异必须 `FIELD_CONFLICT`，
  禁止用 4 位小数容差掩盖。
- **Order(SO) 同义（审核修正）**：比较前补前导零，`620186623` ≡ `0620186623`；
  合并存储值一律为规范化补零形式；真实不同 SO 仍冲突。
- **Material 别名同义（审核修正）**：经机型库显式别名可判同（`SEM520高配` ≡ `SEM520`），
  比较做大小写/多余空白规范化，但不做模糊匹配。

### 2.2 企业物料列映射（模板 `企业物料`，第 6 行起写）

| 列 | 字段 | 来源 |
|---|---|---|
| A 商品料号 | MSO | 合并记录 |
| B 商品编码 | hs_code | 机型库 Sheet1 D 列（8429401100/8429401900） |
| C 商品名称 | product_name | 机型库 Sheet1 E 列（压路机） |
| D 规格型号(申报要素) | declaration | 机型库 Sheet1 F 列**原样提取**（SEM520 含 19600KG 原文，不因箱单改写） |
| E 申报计量单位 | 固定 `台` | |
| G 英文商品名称 | english_name | 机型库 Sheet1 C 列 |
| H 货品类型 | 固定 `成品` | |
| I 企业计量单位 | 固定 `台` | |
| L 法定比例 | 固定 `1` | 数值存储 |
| M 第二比例 | 箱单组净重÷数量 | SEM520=40000÷2=20000；SEM512=24400÷2=12200；数值存储 |
| N 单颗料净重 | 同 M | 数值存储 |
| P 币制 | 固定 `USD` | |
| Q 原产国 | 固定 `CHN` | |
| U 机型 | 标准机型名 | 输入 Material 经机型库/显式别名标准化（未知 `UNKNOWN_MODEL`） |

### 2.3 SEM 发运沟通表列映射（模板 `Sheet1`，第 3 行起写）

| 列 | 字段 | 来源 |
|---|---|---|
| A Dealer Code | dealer_code | 邮件 Dealer（JM28） |
| C Dealer Order | dealer_order | 邮件 Dealer Order |
| D MSO | MSO | 合并记录键 |
| E Material | MSO | 已确认该列取 MSO（样例 E3=BFWVU） |
| F SO | order | 无前导零补一个，已有保留；按文本存储（620186599→0620186599） |
| G Model | material 标准化 | 输入 Material（SEM520/SEM512） |
| H Serial Number | serial_number | 邮件/箱单核对合并 |
| I Machine Price | total_sales_price | 邮件 Total Sales Price（68,822.84），非发票单价 66,757.84；**数值存储** |
| V 国家 | destination.name | 本次用户国家中文名（印度尼西亚） |
| 其余列 | 留空 | 已确认 |

### 2.4 行序

按邮件业务表中的 MSO 顺序（样例 BFWVU、BFWVW、BFWVK、BFWVN）；邮件缺失时按工作簿顺序。

## 3. 参考数据与身份

- 机型库导入：`import_machine_reference(path)` 仅读 Sheet1；B→机型、C→英文品名、D→商品编码、
  E→商品名称、F→申报要素，A/G 保留为 special/verification 备注，另附 source_row 溯源。
  每条含 **destination_scope**：A 列为国家标记（如 巴西）时映射为该国 alpha3，否则 `default`；
  同一机型可保留多条不同 scope 的冲突记录（用户已确认：915F 目的国 BRA 用 A=巴西 的
  123KW 记录、其余国家用 A 空的 110KW 记录），同一 (机型, scope) 内申报资料仍不同才抛错。
  匹配由 core 按 request.destination.alpha3 完成，app 只需透传全部条目。
- **未确认特殊规则（审核修正）**：特殊规则仅 915F 获用户确认；其他带 A 列标记的机型
  （当前 SEM919BRZ）原样保留条目（special_rule_confirmed=False），匹配时暂停并登记
  `UNCONFIRMED_SPECIAL_RULE`，不推广猜测规则。
- 国家：内置常见国家表（src/cater/core/countries.py）+ references.countries 显式别名；
  别名冲突拒绝，旧映射不静默覆盖。
- business_identity：配件=发票号；整机=排序 MSO 集合+发票号
  （样例 `BFWVK|BFWVN|BFWVU|BFWVW|CQLTSU260811M`）。
- content_hash：kind+目的国三位码+规范化记录的规范化 JSON 的 SHA-256。
