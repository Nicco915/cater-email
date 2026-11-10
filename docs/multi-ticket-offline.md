# 多票批次（阶段 3）：显式离线执行运行手册

阶段 2 已把一封多票邮件导入为持久化批次，并在人工确认后为每票创建
**挂起**（`batch_tasks.execution_hold=1`）的任务与不可变快照。阶段 3
在此基础上提供**唯一的新增执行入口**：对一个已确认的批次子任务，显式、
本地、隔离地做一次离线转换，产出可人工复核/上传的模板文件。

本阶段不解除挂起、不接生产轮询、不释放任何自动执行、不发邮件。它只回答：
“这一票如果现在按已确认材料转换，会得到什么文件、哪些字段/来源对不上、
哪些业务前置条件仍然缺失。”

## 1. 范围与安全边界

允许：

- 读取已确认任务、`batch_tasks` 关联、可信快照与既有参考资料；
- 以 `task_id` 为唯一业务身份，调用现有核心 `process_job` 的已确认输入
  路径（阶段 3 A/B/C 组件）；
- 只写调用方显式指定的**新目录**：本次产物 + 执行报告；
- 写现有 JSONL 运行日志（runtime/logs）。

明确禁止/不会发生：

- 不释放 `execution_hold`，不改变任务的 `status`/`version`/`next_retry`/
  `confirmation`，不写 `task_versions`/`task_events`/`outbox`；
- 不写生产 outputs（`runtime/tasks` 下任何位置）、不写原始邮件目录
  （`runtime/mail`）、不改批次快照与批次材料；
- 不读取 `.env`、不用 IMAP/SMTP 凭据、不实例化网关/轮询/交付服务、
  不建 outbox、不联网、不发邮件；
- 不自动确认批次、不确认目的国/金额/HS、不猜参考数据、不生成“伪造
  ready”；
- 不覆盖既有输出目录，失败不把旧文件展示为成功，不删除既有目录。

上述边界由 `cater.app.batch_offline.BatchOfflineRunner` 实现；其
`load_offline_config` 只读 `config.toml` 的 `runtime.dir` 与
`core.header_aliases`，发送配置固定为出厂离线默认值。

## 2. 前置条件

1. **已确认批次与挂起任务**：批次已经过阶段 2 人工确认
   （`mail_batches.status=materialized`），目标票在 `batch_tasks` 中存在且
   `execution_hold=1`，快照 `snapshot_hash` 已持久化。
2. **本地数据库已存在**：离线执行**绝不初始化新库**。如果
   `<runtime>/cater.sqlite3` 不存在，会直接报配置错误并退出。先按
   `docs/testing-and-production.md` 启动一次应用或执行 `init`。
   离线执行必须指向**与批次确认相同的 runtime**（同一数据库、同一
   批次快照目录）；不要指向另一个库或另一份 runtime。最小配置可只写：

   ```toml
   [runtime]
   dir = "/abs/path/runtime"
   ```
3. **模板文件**（仓库根 `templates/`，只读）：
   - `templates/machine_materials.xlsx`（整机企业物料）
   - `templates/machine_comm.xlsx`（整机发运沟通表）
   - `templates/parts_ledger.xlsx`（配件出口台账）
   核心默认读仓库 `templates/`；若部署环境显式设置了
   `CATER_TEMPLATE_DIR`，核心会改读该目录（部署项，不是邮件/凭据变量，
   也不是 `.env` 读取）。缺失时任务会如实 `waiting_input`
   （`TEMPLATE_ERROR`），不产出业务文件。
4. **参考资料**（SQLite，必须已在目标 runtime 中）：
   - 国家/别名：任务目的国名称→三位码；配件原产国代码/名称→三位码；
   - 机型库（仅整机）：按 `model` + `destination_scope` 匹配，提供
     `hs_code`/`product_name`/`declaration`/`english_name`；
   - 配件 HS/中文品名/申报要素来自最终配件发票本身，不读机型库。
   缺参考时保持 `waiting_input`（如 `MISSING_DESTINATION`、
   `UNKNOWN_MODEL`、`UNKNOWN_COUNTRY`），**不降级猜测**。
5. **任务目的国已由受控业务输入确认**：离线执行只读取任务自身
   的目的国确认来源（报告 `destination` 字段含确认人/时间/修订）。
   **阶段 4 已提供逐票目的国确认入口**（Web `GET/POST
   /batches/{id}/destinations`；CLI `batch-destinations` /
   `confirm-batch-destinations`），见 `docs/multi-ticket-destinations.md`。
   未确认的票目的国为空，离线执行返回 `waiting_input`
   （`MISSING_DESTINATION`），这是预期的诚实结果；禁止手工改库或让
   离线执行按正文/父邮件猜国。
6. **业务输入已人工确认**：金额、HS、数量、重量、目的国均以已确认任务/
   快照/参考资料为准；离线执行不替代业务确认，也不提供“修改后即 ready”
   的旁路。

## 3. 标准操作

以下命令以仓库根为当前目录、使用绝对路径为例。`--config`/`--runtime`
指向真实运行目录；离线执行不需要 `.env`。

### 3.1 导入本地 `.eml` 为批次

```bash
python -m cater.app.cli import-batch \
  --eml /abs/path/mail.eml \
  --config /abs/path/config.toml \
  --runtime /abs/path/runtime \
  --json
```

输出 `batch_id`、`batch_ready`、`candidate_count`。`batch_ready` 只表示
预览层未发现阻断问题，**不是**执行授权，也不会创建任务。

### 3.2 人工确认并建票

阶段 3 没有新增 `confirm` CLI。沿用阶段 2 的服务端确认边界（Web
`POST /batches/{id}/confirm`，或同一 `BatchService.confirm_and_create`
服务调用），必须显式覆盖每个候选（选入或带理由排除）。确认成功后：

- 每票得到 `execution_hold=1` 的任务（状态 `RECEIVED`，版本 1）；
- 生成该票独占、不可变、带可信哈希的快照；
- 不处理、不重试、不外发。

查看批次与任务：

```bash
python -m cater.app.cli batch <batch_id> \
  --config /abs/path/config.toml --runtime /abs/path/runtime --json

python -m cater.app.cli task <task_id> \
  --config /abs/path/config.toml --runtime /abs/path/runtime
```

### 3.3 显式离线执行一票

```bash
python -m cater.app.cli run-batch-offline <task_id> \
  --output-dir /abs/path/offline-out/<run-id> \
  --config /abs/path/config.toml \
  --runtime /abs/path/runtime \
  --json
```

要求：

- `<task_id>` 必须是已确认 `batch_tasks` 中的任务；否则报任务不存在/不
  属于已确认批次任务；
- `--output-dir` 必须是**尚不存在的新目录**（父目录可以不存在，会创建）；
  已在的目录一律拒绝，绝不覆盖；
- 目录不得与快照目录、`runtime/tasks`、`runtime/mail`、`runtime/logs`、
  `runtime/batches` 重叠；
- 一次只显式执行一票；同一路径并发由独占创建保证只有一方成功。

退出码：

| 退出码 | 含义 |
| --- | --- |
| 0 | 执行完成且报告状态为 `ready` / `waiting_input` / `unsupported` |
| 1 | 输入/权限/业务拒绝（快照不一致、任务不存在、输出目录冲突、核心拒绝等）|
| 2 | 配置错误（数据库不存在、配置无法读取等）|
| 3 | 核心组件不可用 |

注意：`waiting_input` 也返回 0，因为它表示“已如实完成检查，但业务前置
条件缺失”，不是技术失败。必须读取报告状态，不能只看退出码。

### 3.4 检查报告与产物

每次执行只在该输出目录内产生：

```
<output-dir>/
  offline_report.json
  企业物料 上传.xlsx          # 仅整机 ready
  SEM发运沟通表 上传.xlsx      # 仅整机 ready
  出口台账表体.xlsx            # 仅配件 ready
```

`offline_report.json` 关键字段：

| 字段 | 说明 |
| --- | --- |
| `schema` | 报告 schema（当前 1）|
| `task_id`/`batch_id`/`plan_id`/`candidate_key` | 本次执行身份 |
| `destination` | 本票目的国确认来源：`name`/`alpha3`/`revision`/`confirmed_by`/`confirmed_at`（阶段 4 确认记录；未确认则为空值、revision 0，绝不猜测）|
| `snapshot.path`/`trusted_hash`/`verified_hash`/`payload_hash` | 快照路径、数据库可信哈希、复核哈希、实际消费 payload 哈希（必须一致）|
| `references.version`/`sha256` | 本次读取的参考资料版本与内容哈希 |
| `task_state_before`/`task_state_after` | 执行前后任务状态/版本/hold |
| `state_unchanged` | 两次快照是否完全相同（正常必须为 true）|
| `status`/`kind`/`records`/`issues` | 核心结果与阻断/提示问题 |
| `business_identity`/`content_hash` | 本次业务身份与内容哈希 |
| `artifacts[]` | 产物文件名、绝对路径、sha256、字节数；仅 `status=ready` 时可非空 |
| `error` | 技术/拒绝失败的可读原因（仅失败路径）|

复核产物：

```bash
shasum -a 256 /abs/path/offline-out/<run-id>/*.xlsx
python - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("/abs/path/offline-out/<run-id>/offline_report.json").read_text())
print(report["status"], report["issues"])
for item in report["artifacts"]:
    print(item["filename"], item["sha256"], item["size_bytes"])
PY
```

## 4. 输入、参考与输出字段

### 4.1 整机（machine）

输入：本票快照内的发票/箱单工作簿 + 冻结正文行；参考机型库；任务目的国。
输出：

| 文件 | 字段（模板列）| 来源 |
| --- | --- | --- |
| 企业物料 上传.xlsx | 商品料号(A)=MSO；商品编码(B)=HS；商品名称(C)；申报要素(D)；申报计量单位(E)=台；英文名(G)；货品类型(H)=成品；企业计量单位(I)=台；法定比例(L)=1；第二比例(M)=单颗净重；单颗料净重(N)=净重；币制(P)=USD；原产国(Q)=CHN；机型(U) | MSO/净重来自本票箱单与复核；HS/品名/申报要素/英文名来自机型库；固定值来自既有整机规则 |
| SEM发运沟通表 上传.xlsx | Dealer Code(A)；Dealer Order(C)；MSO(D)；Material(E)=MSO；SO(F，无前导零时补 0)；Model(G)；Serial Number(H)；Machine Price(I)=正文/附件一致金额；国家(V)=任务目的国中文名 | 本票快照字段与机型库；金额须正文/附件一致，无“按行号胜出” |

说明：整机模板没有毛重/数量列。毛重、每票台数在核心重量组与快照
`weight_rows` 中核对（数量=1、净重/毛重与 TOTAL 一致），但不写入模板；
验收时从快照源工作簿与报告 issues 验证。

### 4.2 配件（Health Care Kit / parts）

输入：本票快照内的 Invoice 页、Packing list、BL 等已确认区域；冻结正文行；
国家参考；任务目的国。输出：

| 台账列 | 字段 | 来源/校验 |
| --- | --- | --- |
| B | 商品料号 | 发票明细 |
| D | 申报数量 | Qty/SET × SET QTY，与 TOTAL Qty 一致 |
| G | 币制 | 固定 USD |
| H | 申报单价 | 发票 U'Price；费用行不进入明细 |
| J | 净重 | 单件净重×数量，与发票总净重一致 |
| K | 毛重（仅首行）| 箱单 TOTAL 毛重（皮重已含，不重复加）|
| L | 件数（仅首行）| 箱单 TOTAL Packages |
| M | 原产国 | 发票 COO→三位码（需国家参考）|
| N | 最终目的国 | 任务目的国→三位码（需国家参考）|
| T | 商品编码 | 发票 HS |
| U | 商品名称 | 发票中文品名 |
| V | 规格型号 | 发票申报要素（仅既有开头 `1||` 规范化）|
| Z | 订单号 | 固定留空，禁止复制旧值 |
| AA | 发票号（仅首行）| 最终发票号 |

说明：配件台账没有“总价”列；核心会核对费用行排除、行金额=数量×单价、
TOTAL 行与箱单/发票一致。验收时重开台账，用数量×单价回算源发票行金额，
并核对净重/毛重/件数/HS/COO/目的国。台账当前无申报计量单位列映射，
不输出单位；整机计量单位见 4.1。

### 4.3 来源与溯源

- 快照 `snapshot.json` 的 `candidate.source_refs` 记录每票只允许读取的
  文件/工作表/区域，`assets[]` 记录逐文件 sha256 与绝对路径，
  `mail_rows[]` 记录冻结正文行；离线执行**只消费这些本票资产**，不读
  父邮件、不读别票材料。
- 输出值可回溯到：快照源工作簿单元格、冻结正文行、任务目的国、
  参考资料（机型库/国家表）。报告记录参考资料版本与哈希、快照哈希、
  产物哈希；报告不逐字段展开 provenance，需要时按上述快照/参考复核。
- 若来源存在冲突（正文与附件金额/价格/数量/重量不一致、多文件同票
  冲突），核心显式阻断或等待，不按文件顺序取胜。

## 5. 状态、失败恢复与并发

| 报告状态 | 含义 | 产物 |
| --- | --- | --- |
| `ready` | 全部核对通过，已写模板 | 有（整机 2 个/配件 1 个）|
| `waiting_input` | 缺目的国/参考/模板/必填字段或存在业务冲突，需业务补充 | 无（仅报告）|
| `unsupported` | 无法识别为支持的整机/配件业务 | 无 |
| `failed` | 技术失败（核心异常、报告写入前失败等）| 无/不完整，报告如实说明 |

恢复原则：

1. 先看 `offline_report.json` 的 `status`、`issues[].code`、
   `required_action`，不要只看退出码。
2. 修正业务前置条件（目的国、参考、模板、材料）后，**换一个新的
   `--output-dir`** 重跑；不要复用旧目录，runner 会拒绝覆盖。
3. 若报告存在但 `status=failed`，该目录里的文件不是成功结果；保留报告
   供排查，重新执行到新目录。
4. 若拒绝发生在创建输出目录之前（快照复核失败、目录重叠、任务不存在），
   输出目录不会出现；不要手工创建同名目录伪造结果。
5. 快照复核失败（`ok=False`/TOCTOU 哈希不一致）说明快照被替换或漂移：
   停止该票，保留现场并上报协调者；离线执行不会自动修复，也不会读取
   父邮件/别票替代。
6. 同任务并发执行到不同目录会各自复核快照；同目录只有一方成功，另一方
   明确拒绝，不会混写。仍需把“同一任务多次执行”的结果按报告区分，人工
   选择采用哪一份。
7. 失败不删除既有目录；清理只能由操作者按自己的目录策略处理。

## 6. 限制与已知边界

- 仅 CLI/服务调用，不新增 Web 执行按钮；Web 保持阶段 2 的批次确认界面
  （阶段 4 另提供逐票目的国确认页，不执行核心）。
- 无批次确认 CLI；确认沿用 Web/服务端确认边界。
- 设置任务目的国的入口在**阶段 4**（`docs/multi-ticket-destinations.md`）：
  逐票显式确认，未确认的票只能 `waiting_input`；目的国确认不自动执行
  离线转换。
- 不释放 hold、不自动重试、不批量处理、不接 poller、不发信、不创建
  outbox；不改变正式任务状态。
- 一次调用只执行一票；没有批量/队列入口。
- 报告不逐字段保存 provenance；逐字段来源需按快照/参考复核（见 4.3）。
- 整机模板不含毛重/数量列，配件台账不含总价/计量单位列；对应数值在核心
  校验与快照中核对，不会为了“让模板有值”而改模板或改实现。
- 预览 `batch_ready=true` 不等于可交付：例如非有限单价可能通过预览，
  但显式离线执行会被已确认解析器阻断为 `waiting_input`，零产物。
- 输出文件是本地中间产物；上传/报关/开票仍由人工按既有流程完成。

## 7. 无外发边界核对

运行 `run-batch-offline` 时不存在以下路径：

- 不读 `.env`/`CATER_*` 邮件环境变量/钥匙串；`load_offline_config` 只读
  `config.toml` 的 `runtime.dir` 与 `core.header_aliases`；
- 不构造 `CompositeGateway`/`PollingService`/`DeliveryService`；
- 不写 `runtime/mail`、`runtime/tasks`、`outbox`、批次快照；
- 不建立 IMAP/SMTP 连接，不发起任何网络请求；
- 只写本次显式输出目录与 `runtime/logs` JSONL 日志。

复核时可检查：报告 `task_state_before == task_state_after`、
`state_unchanged=true`；执行前后 `tasks`/`batch_tasks`/`outbox` 行不变；
输出目录之外没有新业务文件。验收测试逐表比较了这些边界。

## 8. 验收测试与真实样本回放

独立验收测试：`tests/app/test_stage3_acceptance.py`（全合成数据）。
阶段 4 目的国确认链路的独立验收在
`tests/app/test_stage4_acceptance.py`，运行方式与覆盖清单见
`docs/multi-ticket-destinations.md` 第 8 节。

```bash
cd /tmp
env -u CATER_IMAP_SSL -u CATER_IMAP_HOST -u CATER_FOLDER \
    -u CATER_MAIL_ACCOUNT -u CATER_IMAP_PORT -u CATER_IMAP_STARTTLS \
    -u CATER_CONFIG -u CATER_RUNTIME_DIR -u CATER_ENV_FILE \
    PYTHONPATH=/abs/path/to/repo/src \
    python3 -m pytest /abs/path/to/repo/tests/app/test_stage3_acceptance.py -q
```

覆盖：

1. 两票整机合成 EML：`import-batch` → 服务端确认 → hold → CLI 离线执行 →
   重开企业物料/沟通表，逐票核对 MSO/序列号/SO/金额/净重/单位/HS/来源，
   别票标识不混入；同目录重跑拒绝覆盖；
2. 两票配件合成 EML：确认 → 离线执行 → 重开台账，逐票核对料号/数量/
   单价/净重/毛重/件数/COO/目的国/HS/发票号/规格，行金额回算；
3. 损坏工作簿在确认阶段阻断，零任务；
4. 非有限单价通过预览但在显式离线执行中阻断，零业务产物；
5. 缺机型参考 → `waiting_input`，零业务产物；
6. 快照被篡改 → 创建输出目录前拒绝；
7. 本机有真实研究邮件时，在隔离 tmp runtime 只读导入三封真实样本：
   原始 `.eml` 哈希/mtime 不变，候选保持 pending，零任务/零 batch_tasks/
   零 outbox/零快照/零 ready 产物；不设置目的国、不确认金额/HS。

完整测试（2026-11-10，Python 3.13.5，从 `/tmp` 无 `.env`、绝对路径）：

```bash
cd /tmp
env -u CATER_IMAP_SSL -u CATER_IMAP_HOST -u CATER_FOLDER \
    -u CATER_MAIL_ACCOUNT -u CATER_IMAP_PORT -u CATER_IMAP_STARTTLS \
    -u CATER_CONFIG -u CATER_RUNTIME_DIR -u CATER_ENV_FILE \
    PYTHONPATH=/abs/path/to/repo/src \
    python3 -m pytest /abs/path/to/repo/tests -q
# 808 passed, 3 skipped（跳过项为环境未安装 pwsh 的三个脚本测试）
```

注意：若外层 shell/系统环境已导出 `CATER_*` 邮件变量，`load_config`
（导入/Web 路径）会按既定规则让 OS 环境覆盖配置，导致验收结果失真；
因此验收命令显式清除这些变量。离线执行本身不读它们，也不读 `.env`。

真实三封的只读回放证据、产物校验报告保存在
`runtime/multi-ticket-stage3/d/`（gitignored，不入库）；真实邮件原文
仍在 `input/邮件研究/...`，不被复制进 fixture/文档/日志。

## 9. 故障排查

| 现象 | 原因/处理 |
| --- | --- |
| `configuration error: 数据库不存在` | 目标 runtime 未初始化；先用既有流程 `init`/启动应用。离线执行绝不建新库。 |
| `任务不存在或不属于已确认批次任务` | task_id 错误或该票不是已确认批次子任务；用 `batch <id>` 核对。 |
| `输出目录已存在` / `已被并发创建` | 换一个新的空目录；不覆盖、不混写。 |
| `快照复核哈希与任务可信哈希不一致` / `TOCTOU` | 快照被替换/漂移；停止执行，保留现场并上报，不手工改快照或改库。 |
| `MISSING_DESTINATION` | 任务目的国未确认；按阶段 4 手册（`docs/multi-ticket-destinations.md`）经 Web/CLI 逐票确认后，换新输出目录重跑。 |
| `UNKNOWN_MODEL` | 机型库无匹配或 scope 被禁用；维护显式参考/别名，不模糊猜测。 |
| `UNKNOWN_COUNTRY` | 目的国/原产国无法映射；补充受控国家别名。 |
| `TEMPLATE_ERROR` | 模板缺失/损坏；恢复 `templates/` 三个文件。 |
| `status=failed` | 技术失败；查看报告 `error` 与 `runtime/logs` JSONL（已脱敏），修正后换新目录重试。 |
| `waiting_input` 但无阻断 code | 看 `issues[].required_action`；`waiting_input` 可能是缺字段而非技术错误。 |

## 10. 与阶段 2/后续阶段的边界

- 阶段 2：导入、方案版本、人工确认、建 hold 任务、快照可信哈希；
- 阶段 4：逐票显式目的国确认（Web/CLI，见
  `docs/multi-ticket-destinations.md`），只写目的国与逐票审计；
- 阶段 3（本文件）：显式离线执行与报告（报告含目的国确认来源追溯），
  不释放 hold、不发信、不写正式状态/outbox；
- 后续阶段：释放 hold、批量/自动执行、交付外发、失败重试 UI、逐字段
  provenance 展示。这些均**未实现**，不得用本阶段离线输出冒充已释放或
  已交付。
