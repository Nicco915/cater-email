# 灵活输入：多结构工作簿、语义定位与布局确认

> 版本：2026-11-05。对应任务 plan/tasks/claude-flexible-input.md（五阶段全部落地）。
> 实现位置：`src/cater/core/parsing/`（workbook.py / headers.py / parts.py /
> machine_wb.py / mail_html.py）、`src/cater/core/reconcile.py`、
> `src/cater/app/`（intake.py / application.py / delivery.py / tasks.py /
> migrations.py / cli.py / web/）。

## 1. 统一只读工作簿接口

- `load_workbook_pair(path)` 返回 `(values_wb, formula_wb)`，1 基行列坐标、
  A1 定位（`ws.cell(r, c).coordinate`），`cell_value()` 读值并区分公式缓存状态。
- **同时支持 `.xlsx`（openpyxl）与真正 BIFF `.xls`（xlrd 2.0.2，纯 Python）**。
  `.xls` 的公式视图即值视图（xlrd 不暴露公式），因此 `.xls` 从不触发
  `FORMULA_CACHE_MISSING`；`.xlsx` 公式无缓存时登记该问题，不当零处理。
- 安全限制（附件入口同样生效）：非 OLE/OOXML 签名拒绝（把 zip 改名 `.xls`
  会被 `UnsupportedSourceError` 拦下）；zip 成员数 >2000、解压总量 >500MB、
  单表 >300 万单元格均拒绝，不解压炸弹。
- 不依赖 Excel/LibreOffice 外部进程，Windows 可直接运行（xlrd 为纯 Python
  wheel）。xlwt 仅用于生成测试夹具（tests/core/fixtures/generate_xls_fixtures.py），
  不是项目依赖。

## 2. 表区与语义字段定位

### 2.1 原则

- **内容优先、表名辅助**：先按内容（语义表头）定位表区；sheet 名只做
  倾向（发票↔CI/invoice，箱单↔PL/packing list），不单独决定选表。
- 有效扫描区允许**表头在第 56 行**这类深表头，也允许一张 sheet 里多个表块
  （如 CI 表 56 行机器信息表 + 34 行费用区）。
- 表头规范化：NFKC + 大小写折叠 + 去除所有非词/非 CJK 字符
  （`U'weight/kg`≡`U' WEIGHT / KG`，`SERIAL #`→`serial`，`QTY.`→`qty`）。
- **双行（组合）表头**：主表头行定位业务列；`U'weight/kg` 等重量子表头
  只在次表头行搜索（`SUBHEADER_FIELDS`）。
- **可配置别名**：`process_job` 请求键 `header_aliases = {字段: [别名]}`，
  与内置别名并集生效（`merged_aliases`）。别名也是规范化后匹配。
  本地配置入口：`config.toml` 的 `[core.header_aliases]`（见
  `config/config.example.toml`），由 `AppService` 在构建请求时注入并随
  版本快照持久化——重试/重发不依赖当时的本地配置；配件与整机解析共用。
  别名集合的差异也计入布局指纹（标记 `default-v1` 或别名集的哈希），
  别名变化会作为“布局变化”呈现，不会被静默吞掉。
- **已知结构锚点**：无标题补充列（商品编码/中文名/规格型号）按表头区
  最右非空列 +1 起连续三列定位；格式不合法（HS 非 8–13 位数字等）→
  `SUPPLEMENTARY_COLUMNS_INVALID`。

- **有界全有效区扫描**：语义定位默认扫描 `SCAN_ROW_LIMIT = 5000` 行
  （覆盖第 56 行深表头，同时给 xls BIFF 虚高维度（可能报 65536 行）一个有
  界截止）；TOTAL 行按语义锚定（`find_total_anchor_rows`/`select_total_row`：
  exact `total` 与前缀 `TOTAL (IN WORDS)`/`TOTAL AMOUNT` 等，优先语义列带
  数值的锚点），不再固定假设 A 列/固定行号。首个锚点即本表 TOTAL（业务表
  由 TOTAL 行终止，位置自由）；同一块内存在多个无法分隔的合计行 → 暂停
  （`MULTIPLE_TOTAL_ROWS`）；完全空行分隔的后续合计行属于其他表块，不影响。
- **表头候选枚举**：一行覆盖全部必需字段（唯一或多候选均算覆盖）即表头
  候选行；**多个候选行**（同 sheet 多张业务表）→ `multiple_regions` 暂停，
  登记 `FINAL_SHEET_INVALID`/`PACKING_TOTALS_INVALID`/`MACHINE_TABLE_INVALID`/
  `WEIGHT_TABLE_INVALID`，**禁止因首区字段歧义而静默落到后一张表**。
- **次表头行只在确实命中子表头字段时占用**：否则它可能是数据行，不得前移
  导致首条明细被跳过。

- **配件明细行不可静默丢弃**：行有 part_code 但 qty/unit_price 缺失或
  非法（非数字）→ 登记阻塞问题 `INVALID_DETAIL_ROW`（record_key=料号、
  field、来源单元格、所需行动），整票暂停；无 part_code 的显式费用/说明行
  仍按费用行规则排除，不被误报。
- **必需字段声明与访问一致**：配件发票必需字段含 `coo`（原产国）——声明
  缺失即 `FINAL_SHEET_INVALID`（缺失字段原因），绝不因 `columns['coo']`
  KeyError 退化为技术失败；整机 `serial` 等可选列一律经 `columns.get()`
  防御访问，同类键错已排查修复。

- **机器信息表数据区有界**：明细行在首个完全空行（含纯空白串）处终止
  （业务表是连续块）；紧邻出现重复表头行（已定位列落回表头别名文本）说明
  存在多张表块且不构成完整候选 → 暂停（`MACHINE_TABLE_INVALID`），绝不把
  第二表块的头/数据行吞成 literal MSO/MODEL 记录。

### 2.2 禁止猜测

- 多候选（两个单价语义列、两个合格最终表、两个内容候选机器表、多张业务
  表头区、同块多张合计行）→ 暂停，登记 `FINAL_SHEET_INVALID`（附缺失字段/
  多候选/多个表头区原因）/ `AMBIGUOUS_FINAL_SHEET` / `AMBIGUOUS_MACHINE_TABLE`
  / `AMBIGUOUS_WEIGHT_TABLE` / `MULTIPLE_TOTAL_ROWS`。
- 未配置的新表头不放行（如 `Settle Amount`），不因数值巧合猜语义列
  （测试覆盖 qty=1 时金额与单价相等 + 相邻巧合列）。
- 配件**最终调整表优先**：唯一 `CI-For Dealer (n)` 结构合格即采用；仅原表
  → `FINAL_SHEET_NOT_FOUND`，禁止回退原价格表。
- 历史 sheet（`Sheet1`/`Sheet2`/`BL` 等无业务名且含旧数据的表）不参与
  选表；无业务名且多个内容候选 → 暂停。

### 2.3 核对规则 ID（日志与布局报告携带，正文不展示）

| 规则 ID | 含义 |
|---|---|
| `PARTS_WEIGHT_QTY_X_UNIT` | 配件每行 净重=数量×单件重 |
| `PARTS_DETAIL_NET_TOTAL` | 配件明细净重合计 vs TOTAL 行 |
| `PARTS_HS_TITLED_VS_SUPP` | 表头 HS 列 vs 补充编码列不一致（非阻塞，以补充列为准） |
| `MACHINE_WEIGHT_GROUP_QTY` | 箱单组数量 vs 组内 MSO 数 |
| `MACHINE_PACKING_TOTAL_ROW` | 箱单 TOTAL 行 vs 明细合计 |
| `MACHINE_CI_TEXT_VS_PL_NET` | CI 文本净重 vs PL 净重交叉核对 |

未执行的核对绝不标“通过”：任一侧数据缺失（如表头 HS 列存在但为空）时，
该核对结果记 `skipped`（detail 注明“…为空，HS 比对未执行”），交付正文渲染
为“未执行（条件不足，跳过）”；仅两侧齐全才比较并记 `pass`/`fail`。
未确定/冲突的问题不被“人工重点核对建议”替代。

新增暂停问题码：`INVALID_DETAIL_ROW`（配件明细行缺 qty/单价等，阻塞）、
`MULTIPLE_TOTAL_ROWS`（同块多张合计行，阻塞）。
合计核对（`MACHINE_PACKING_TOTAL_ROW`）在 TOTAL 行数量/净重均空白时记
`skipped`（“TOTAL 行数量/净重为空，合计核对未执行”），绝不冒称通过。

追问正文可读性（`delivery._describe_issues`）：上述输入结构类问题
（含 `FINAL_SHEET_*`/`SUPPLEMENTARY_COLUMNS_INVALID`/`MACHINE_TABLE_INVALID`/
`WEIGHT_TABLE_INVALID`/`AMBIGUOUS_*`/`MISSING_SOURCE`/`UNSUPPORTED_FILE`）
渲染为面向操作者的中文引导——给出工作表/单元格定位、实际缺失/多候选
字段（Python 列表/元组/行号字面量翻译为可读中文，如 `['coo']`→原产国、
`[('qty', [6, 10])]`→数量第6、10列均命中）与可执行动作（补齐该行数量/
单价后重发附件、仅保留/明确最终业务表、唯一 PACKAGE/GROSS WEIGHT 列等），
不 dump 内部码、不一律“联系处理人员”。整票级 `MISSING_FIELD`
（发票号/毛重/件数）优先展示 `required_action`。

## 3. 整机业务规则保持

- 净重 = 箱单组净重 ÷ 组数量（按组内 MSO 清单关联，禁止仅凭机型拼接）；
  除法超 5 位小数 `WEIGHT_PRECISION`。
- 邮件业务表与附件**两边都提取**，按 MSO 关联、序列号辅助；冲突/缺失整票
  暂停（`FIELD_CONFLICT` / `MISSING_FIELD`），不静默覆盖。
- **发票单价（如 CI 中 47,141.75）绝不允许冒充 Total Sales Price**；
  二者是不同业务字段，不互相覆盖。
- 费用行（内陆运费/海运费）不计入机器价格；历史特殊规则（915F 目的国
  scope、未确认特殊规则暂停）原样保留；冻结的参考快照重试仍生效；
  模板 OOXML 往返保真不变（xlrd 只读，不写 .xls 模板）。

## 4. 布局指纹与确认流

- 每次处理生成**结构指纹**（`canonical_structure` 投影的 SHA-256 前 20 位）。
  投影按**语义角色**键控（`parts_invoice` / `packing_total` /
  `machine_table#i` / `weight_table#i`），只含工作表名、表头行、次表头行、
  列位与无标题补充列位置，外加别名集合标记（`default-v1` 或自定义别名集
  的哈希）。**不含**任何实际数值、record_key、逐条核对证据，也不含由记录
  条数衍生的 data_end/TOTAL 行——同一模板换一票货（不同 MSO/数量/重量、
  不同记录条数）指纹不变；真正的结构变化（工作表重命名、表头行/列位移动、
  别名集变化）指纹即变。
- 完整布局（含逐条核对证据与真实值）仍随版本持久化
  （`task_versions.layout_json`，迁移 11），用于正文渲染与人工核对，但不
  参与指纹。
- 布局随版本持久化（`task_versions.layout_json`，迁移 11），任务事件
  `layout_seen` 只记录 kind/fingerprint/confirmed/sheets/check 规则 ID。
- **首次使用**（指纹未确认）的交付正文给出“人工重点核对建议”；已确认
  布局不重复告警。
- **基线是已确认布局**（不是上一封邮件，也不是未确认的一次性修正）。
- 显式确认：`cli confirm-layout task_xxx [--version N]` 或网页任务页
  “确认表格布局”按钮（`POST /tasks/{id}/confirm-layout`，CSRF + 审计事件
  `layout_confirmed`）。确认写入 `confirmed_layouts(kind, fingerprint)`，
  成为该类业务结构变化对比的基准。
- 相对基线的结构变化**按语义角色配对对比**（不按 sheet 名配对）：sheet
  重命名（“工作表 CI→Commercial Invoice（重命名/换表）”）、新增表
  （“新增箱单重量表”）、移除表（“移除箱单重量表”）、表头行/列位变化均
  显式列出；与实际修正（按回复执行的 overrides）在成功正文中**分列**展示；
  正常成功仍保持简短。
- **字段列位独立列出**：无论是否有基线/差异，成功正文都给出“字段列位
  （本版采用）”——每张语义表的表头行、各字段→列（含次表头行与补充列），
  使数量/单价/重量的取值位置可追溯，不依赖差异对比。
- **自动窄规范化可见**：规格型号开头 ``1||`` → ``1|``（删除多余空项）等
  自动清洗在“实际修正”节与按回复执行的修正同列展示，记录料号、前后完整
  值（保留内部竖线）、来源单元格与可读摘要；未发生变化的记录不产生条目。

## 5. 已验证与未验证

已验证（`python3 -m pytest tests -q`，本仓库 macOS + Python 3.13）：

- 真实旧/新配件样本、真实新整机 `.xls`（配合**显式合成邮件**——新邮件样本
  未在本地提供，测试不宣称真实新邮件已通过）、合成 `.xls` 多结构样例。
- 表/列/行移动、双行表头、多候选拒绝、错列数字巧合、公式无缓存、
  费用行与历史表排除、`.xls` 附件全链（intake → 任务 → 交付正文）、
  异常邮件可读性与正常邮件不过度提示、开头 `1||` 窄修复、模板 OOXML 保真、
  布局确认流（CLI/网页/服务级）。
- 第二轮审计回归（`tests/core/test_audit_round2.py` 15 例 +
  `tests/app/test_audit_round2_app.py` 9 例，共 354 例全绿）：缺 COO 表头
  结构化暂停、明细行缺 qty/单价阻塞（费用行不误报）、HS 单侧缺失记
  skipped、同 sheet 多表头区/首区歧义不静默落到后表、第 56+ 行深表头、
  TOTAL 行移位、同块多张合计行暂停、空行分隔的第二表块不误伤、结构指纹
  对数值/记录条数不变而对表头/列位移动敏感、本地 `[core.header_aliases]`
  配置注入请求快照并作用于配件与整机。
- 第三轮复核回归（`tests/core/test_audit_round3.py` 7 例 +
  `tests/app/test_audit_round3_app.py` 3 例）：整机 SERIAL 列缺失不
  KeyError、机器信息表在空行/重复表头处终止（第二表块不吞成 literal
  记录，紧邻块暂停）、箱单 TOTAL 数量/净重空白记 skipped、clean_spec
  1|| 窄规范化带来源进入 layout 修正记录、首次使用正文列出字段列位、
  自动规范化在“实际修正”节可见且保留内部竖线。全量 364 例通过。
- 合成夹具说明：`tests/core/fixtures/flexible_machine.xls` 由
  `generate_xls_fixtures.py`（xlwt，一次性生成）产出，全部为 FAKE/SYN 值。

未验证项（如实报告）：

- **Windows 实机未验收**：xlrd 为纯 Python、理论上可直接运行，但
  `.xls` 解析与 PowerShell 脚本一样未在真实 Windows/PowerShell 5.1 上跑过。
- 真实新整机邮件未在本地，端到端以合成邮件隔离验证；待真实邮件到达后
  建议先人工核对一版再 `confirm-layout`。
- 布局确认目前按 kind 全局基线，不按机型/客户细分；若后续出现同业务
  多形态布局，需要引入二级键。
