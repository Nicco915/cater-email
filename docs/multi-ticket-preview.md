# 多票分票预览（阶段 1）

一封来邮可能携带多票（多张发票）业务附件。本功能在**不建任务、不写库、
不联网**的前提下，离线回答一个问题：这封邮件实际包含哪几票、每票由哪些
附件与正文行组成、有哪些冲突必须人工确认。

## 架构

```
本地 .eml ──► app/preview.py ──► core/api.inspect_sources ──► JSON 预览
              （离线服务）         （core/split_preview.py）
```

- **`cater.core.split_preview.inspect_sources(request)`**：核心入口。
  识别每份工作簿的业务区域（整机发票/箱单重量表/机器信息表、配件
  Health Care Kit 发票页/应用表/箱单/BL 页）与身份（发票号、MSO、
  Order Code、序列号），把附件归并为候选票并关联正文业务行。
  与 `process_job` 共用同一套只读工作簿加载（`load_workbook_pair`，
  含 zip 炸弹上限、xls/xlsx 统一接口）、语义列定位（`resolve_columns`）
  与 Source/Issue 溯源模型；**不改动 `process_job` 单票契约**。
- **`cater.app.preview.preview_eml(path)`**：离线服务。解析 .eml，
  把非内嵌附件落盘到自动清理的隔离临时目录（`safe_filename` 清洗 +
  内容哈希前缀，防路径穿越与重名覆盖），随后调用核心入口。
  **刻意不经过 AppService**：不加载配置/.env/钥匙串（安全上限按
  MailConfig 默认值硬编码）、不初始化数据库、不建任务、不写外发箱、
  不动 IMAP 游标、不联网、不生成模板。
- **`cater-app preview-tickets`**：CLI 包装，见下。

## CLI 用法

```bash
# 预览 JSON 打到 stdout
cater-app preview-tickets --eml path/to/mail.eml

# 同时写入文件（已存在需显式 --overwrite；拒绝覆盖输入文件）
cater-app preview-tickets --eml mail.eml --output preview.json [--overwrite]

# 追加邮件之外的显式附件（可重复）
cater-app preview-tickets --eml mail.eml --source extra.xlsx
```

退出码：0 = 预览生成（冲突只是预览内容，见 `batch_ready`）；1 = 输入层
错误（文件缺失/超限/输出冲突）。

## 预览契约（schema_version=1）

顶层：`schema_version`、`inspector_version`、`alias_marker`、
`source_fingerprint`（附件内容哈希 + 正文哈希 + 检查器版本 + 别名标记的
复合哈希，不含任何秘密）、`mail`（subject/message_id/notes，notes 为剔除
引用与表格后的新写业务说明行，仅展示用）、`candidates[]`、
`unmatched_sources[]`、`unmatched_mail_rows[]`、`issues[]`、
`batch_ready`。

每个候选：`candidate_id`（内容寻址：身份 + 成员内容哈希的哈希，
**与附件顺序/文件名无关**，改名/乱序/重复预览保持稳定）、`kind`
（machine/parts/unknown）、`identity`、`source_refs`（内容哈希 + 原始
文件名 + 各 sheet 区域定位）、`mail_row_refs`、`facts`（值 + 多来源 +
冲突 variants）、`checks`（已执行的核对规则与结果）、`issues`、
`needs_confirmation`。

### 归并的证据强度分层

- **强证据：明确的当前发票号。** 同号（含同号不同内容的版本）归并为同一
  候选；内容冲突在事实层并列 variants（`CANDIDATE_FACT_CONFLICT`），
  绝不吞掉任何版本。
- **弱证据：MSO / 序列号 / Order Code。** 只用于把"无明确发票号的附件"
  挂到唯一匹配的票。两个具有不同明确发票号的簇**绝不**因弱字段相同而
  归并；共享弱字段作为 `WEAK_IDENTITY_ACROSS_TICKETS` 冲突可见。
- **歧义保留。** 无发票号附件的弱字段同时命中多票 →
  `ATTACHMENT_MATCHES_MULTIPLE_TICKETS`，该附件成为独立候选，不充当
  桥接节点。

### 关键判定规则

- 一个 Excel ≠ 一票：分文件（发票 + 箱单）按身份合并；字节级重复附件
  按内容哈希去重（文件名并列保留）；同号修订版并列冲突。
- 无法读取/伪造后缀/读到但无业务身份的附件：进入 `unmatched_sources`
  并带阻断问题（不影响其他附件识别出候选）；图片等明确非工作簿附件
  同样列入 `unmatched_sources` 可见，但**不阻断** `batch_ready`。
- BL 页出现的与当前发票不同的票号 → `STALE_BL_INVOICE`（旧模板残留），
  不作为当前发票号来源。
- 套件应用表数据行至少需两项身份字段；仅剩单项的行视为模板残留
  （`TEMPLATE_RESIDUE_ROWS`），终止该表扫描，不构成身份。
- 金额差异（`AMOUNT_VARIANTS` / `AMOUNT_CONFLICT`）只标记不修改；
  净重/毛重、件数/套数分别成字段；皮重为独立事实并带
  `PREVIEW_TARE_INCLUDED` 核对记录；提取不出的字段留空并给问题，
  不填造值。
- 正文业务行按 MSO 优先、序列号兜底一对一关联；主题不覆盖附件与正文
  首行业务内容。关联基于**附件冻结身份建立的规范机器 ID**（同表行
  (MSO, 序列号) 为一台机器；正文补认不改变基准）：正文行的
  全字段/仅 MSO/仅序列号形式都映射到同一规范 ID，映射歧义
  （`MAIL_ROW_MACHINE_AMBIGUOUS`）显式阻断。关联后**按票聚合**再核对
  整票金额：仅当行→规范机器一一对应、无重复（`MAIL_ROW_DUPLICATE`）/
  交叉/缺价且映射集合等于候选规范机器集合时，才把正文行合计与整票
  金额比较；部分覆盖/不确定一律 `MAIL_AMOUNT_INCOMPLETE`，绝不把部分
  行总和当整票、不伪造 pass。附件缺金额时也只在全量覆盖下采用正文
  合计（带来源）。正文解析对缺 MSO 但带序列号的行仅在预览路径保留
  （`require_mso=False`），process_job 行保留规则不变。
- 机器级 MSO×序列号配对语义：同票多机的集合身份不掩盖行内交叉
  （`MSO_SERIAL_CROSS`）。附件已有明确 MSO 时，序列号兜底命中的冲突
  MSO 不并入身份（`MSO_CONFLICT`，阻断）；只有附件确实无 MSO 且序列号
  唯一完整匹配时才从正文补认 MSO（旧 CI 布局正常兜底）。

## 边界与尚未实现

已完成（阶段 1）：离线识别与分票预览、冲突/歧义可见、CLI、
合成测试与三封真实样本回放（8/1/4 候选）。

**不做**：不生成上传工作簿、不建任务、不写数据库、不收发邮件、不启动
轮询。`batch_ready` 仅表示**预览层面未发现阻断问题**，不是正式分票
结论、不是报关或开票确认，也不代表任何后续动作已获授权。

后续阶段（未实现，迁移方向）：

1. 预览结果落库：新增 `mail_batches` / `batch_tasks` 表与迁移，把
   `source_fingerprint` 作为批次幂等键，候选确认后按票建任务；
2. Web 界面呈现候选与冲突，人工确认/调整归属后触发逐票 `process_job`；
3. 轮询管线接入：来邮先跑 `inspect_sources`，多票时挂起等待确认，
   单票维持现有自动流程。

## 安全边界

预览路径永不读取 .env/钥匙串/配置；安全上限硬编码（邮件 60MB、
单附件 25MB、累计 50MB、附件数 64）。附件只读，落盘仅在自动清理的
临时目录内。回放证据（真实邮件预览 JSON）只保存在 Git 忽略的
`runtime/multi-ticket-preview/`，不进入版本控制。
