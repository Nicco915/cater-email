# CATER 运行与运维手册

本手册面向长期运行 CATER 的业务与运维人员，覆盖 Windows 安装、配置、离线演练、常驻运行、日常管理、备份恢复和故障处理。转换业务规则由 `cater.core` 负责；本应用负责邮件接收、状态持久化、任务编排、交付、网页和审计。

> 安全基线：默认只绑定 `127.0.0.1`，默认 `dry_run = true`、`enabled = false`，允许名单为空。完成离线核对和 Windows 收发验证前，不要开启真实发信。

## 1. 目录与数据

| 路径 | 内容 |
| --- | --- |
| `config/config.toml` | 本地配置；已排除出版本控制，绝不写入密码 |
| `config/config.example.toml` | 配置样例 |
| `runtime/cater.sqlite3` | SQLite 数据库（WAL 模式） |
| `runtime/mail/` | 原始邮件（按哈希存放） |
| `runtime/tasks/<task_id>/v<N>/` | 每版输入、请求快照、输出、交付记录 |
| `runtime/logs/` | 脱敏 JSONL 运行日志 |
| `runtime/backups/` | 数据库备份 |
| `runtime/diagnostics/` | 脱敏诊断包 |
| `runtime/web-secret` | 网页会话/CSRF 签名密钥（自动生成） |

SQLite 保存任务状态、邮件元数据、发件箱、配置变更审计和参考资料。不要手工编辑数据库；结构升级只通过内置迁移进行。

## 2. Windows 安装

要求：Windows 10/11 或 Windows Server、Python 3.11 及以上（建议 3.13）、可访问邮箱的 IMAP/SMTP 网络。

```powershell
git clone <仓库地址> C:\CATER
cd C:\CATER
PowerShell -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本会创建 `.venv`、安装依赖（含 `extract-msg` 用于离线 `.msg`）、复制配置样例并初始化数据库。随后编辑 `config\config.toml`。

> **仅支持源码目录 + editable 安装。** `cater.core` 默认从源码仓库根目录的 `templates\` 读取三个 Excel 模板（`parts_ledger.xlsx`、`machine_materials.xlsx`、`machine_comm.xlsx`），普通非 editable wheel 安装不会包含这些文件，也无法通过 `Path(__file__).parents[3]` 找到仓库模板。不要用 `pip install cater-*.whl` 部署。`scripts\install.ps1` 会先检查 `src\cater\core\api.py` 和三个模板，缺失时立即报错；`run-web.ps1`、`run-poller.ps1`、`backup.ps1` 都会先切换到仓库根目录。高级部署如确需自定义模板位置，必须把三个模板放在同一目录并设置 `CATER_TEMPLATE_DIR`，否则转换会在调用核心前明确失败。

> **原生命令退出码。** `scripts\install.ps1` 通过 `scripts\native.ps1` 统一检查 `py -3 -m venv`、`pip install`、`cli init` 等每一步的 `$LASTEXITCODE`；任一步非零都会立即抛错并以非零码退出，不会在失败后继续打印 `Installed`。`run-web.ps1`、`run-poller.ps1` 在服务异常退出时返回实际退出码，`backup.ps1` 备份失败也会抛错。上述 PowerShell 脚本只做了静态检查和 helper 逻辑测试，**尚未在真实 Windows PowerShell 5.1 / 任务计划程序上实机验收**。

> **依赖变更后必须重装一次 editable。** 代码更新若引入或升级运行依赖（例如真正 BIFF `.xls` 解析所需的 `xlrd`），拉取代码后需在仓库根目录执行一次性 `.venv\Scripts\python.exe -m pip install -e .`（或重跑 `scripts\install.ps1`，它会显式校验 `import xlrd`，缺失时安装直接报错而不是带病运行）。`cli health` 输出中的 `xls_dependency_installed` 为 `false` 即表示当前环境缺 `.xls` 解析依赖，此时 `.xls` 任务会报 `WORKBOOK_DEPENDENCY` 而不是误报“缺少附件”。

手动安装等价步骤：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\config.example.toml config\config.toml
.venv\Scripts\python.exe -m cater.app.cli init
```

`init` 只创建目录/数据库并打印路径，不会连接邮箱、不会发信。

## 3. 配置

### 3.1 邮箱与轮询

```toml
[mail]
account = "work-mailbox@example.com"
folder = "INBOX"          # 以实际邮箱文件夹名为准
sent_folder = "Sent"      # 已发送文件夹，用于发送结果不确定时核对
imap_host = "imap.example.com"
imap_port = 993
imap_ssl = true
smtp_host = "smtp.example.com"
smtp_port = 465
smtp_ssl = true
poll_interval_seconds = 60
first_run_new_only = true
```

- `imap_ssl` 与 `imap_starttls` 互斥；SMTP 同理。
- 首次运行默认只建立 UID 游标，不处理历史邮件。确认游标和允许名单正确后，才可执行一次 `poll --history` 导入历史。
- 电脑休眠恢复后会按已保存游标补取邮件。
- 附件/邮件大小上限是保护阈值；超限会留下明确问题，不会静默截断业务字段。

### 3.1.1 163 企业邮箱（专用邮箱）与 IMAP ID 握手

163 系专用邮箱（如 dedicated163 账号）在 LOGIN 成功后、未发送 IMAP
`ID`（RFC 2971）命令时，会拒绝 `SELECT/EXAMINE INBOX`（返回 `Unsafe
Login`）。本应用的处理（自动、无需配置）：

- **握手时机**：每次 IMAP 登录成功后、访问任何文件夹之前；`scan`（INBOX
  收取）与 `find_sent`（已发送核对）两条路径一致，每连接一次，不重复登录。
- **能力探测**：服务器通告 `ID` 能力才握手；不通告的服务器（如飞书）完全
  不受影响。登录后能力集可能变化，必要时自动重查一次 CAPABILITY。
- **客户端元数据**：仅发送静态真实信息 `name=CATER`、`version=1.0`；绝不
  携带账号、密码或任何个人/业务数据。
- **失败即停**：服务器通告 ID 但拒绝该命令（NO/BAD/断连）时明确报错，不
  静默继续——否则后续 SELECT 只会得到误导性的 “Unsafe Login”。
- **诊断**：SELECT/EXAMINE 失败时错误信息保留服务器返回的限长文本（去控
  制字符、截断），可区分 “Unsafe Login. Please contact kefu@…” 之类的
  策略拒绝与 “Mailbox doesn't exist” 之类的文件夹不存在；不记录协议级
  debug 日志或原始邮件内容。
- **INBOX 与已发送文件夹**：收件箱即配置项 `folder = "INBOX"`；已发送核对
  使用 `sent_folder`（默认 `Sent`，可在网页/设置中核对实际文件夹名）。
- **UID 游标不变**：握手不改变 UID 游标建立与补取语义；首次运行仍默认
  只建立游标（`first_run_new_only = true`），不会自动导入历史邮件；
  `poll --history` 仍需人工显式执行。

### 3.2 允许名单（同时控制接入与发信）

```toml
[allowlist]
mode = "test"             # test 或 production，二选一
test = ["tester@example.com"]
production = []
```

- 只有当前 `mode` 对应的名单生效；两套名单互不回退。
- 不在名单内的发件人：邮件被忽略并记录；离线 `process` 也会被拒绝。
- `production` 模式必须至少有一个生产地址。
- 网页“设置”和 `settings` 命令修改的是 SQLite 中的运行时配置，并写入 `config_versions` 审计表；配置文件仍是基线。

### 3.2.1 语义表头别名（可选）

客户工作簿表头写法漂移（如单价写成 `Final Price`、机型写成 `Type`）时，
在 `config.toml` 追加本地别名，配件与整机解析共用；随任务请求快照持久化，
重试不依赖当时配置，也不写入指纹之外的任何地方：

```toml
[core.header_aliases]
unit_price = ["Final Price", "结算价"]
model = ["Type"]
```

别名只影响**表头匹配**，不改业务规则；别名集合差异会体现为布局变化。
语法与更多语义定位规则见 `docs/flexible-input.md` §2。

同一归一化别名（大小写/空白/标点不敏感）映射到**两个邮件语义字段**
（如把 `mso` 同时配给 `dealer_code`）属配置冲突：解析会整体暂停并登记
`ALIAS_CONFIG_CONFLICT`，绝不静默取后配的一项。注意邮件与工作簿是两个
命名空间——同一个写法同时用作邮件列名与工作簿列名不算冲突。

### 3.3 发送开关

```toml
[sending]
dry_run = true
enabled = false
```

真实发信必须同时满足：

1. `sending.enabled = true`
2. `sending.dry_run = false`
3. 收件人位于当前生效允许名单
4. 邮箱账号/服务器与凭据可用

否则交付只写入发件箱并标记 `dry_run`，不连接 SMTP。网页不显示、不保存密码。

### 3.4 邮件密码

优先使用 Windows 凭据管理器（目标名固定）：

```powershell
.venv\Scripts\python.exe -m cater.app.cli credentials set --service imap --username work-mailbox@example.com
.venv\Scripts\python.exe -m cater.app.cli credentials set --service smtp --username work-mailbox@example.com
.venv\Scripts\python.exe -m cater.app.cli credentials status
```

密码通过隐藏输入读取；`credentials status` 只返回是否存在，不返回内容。

非 Windows 或临时演练可用环境变量（不要把值写入脚本或提交）：

```powershell
$env:CATER_IMAP_PASSWORD = "..."
$env:CATER_SMTP_PASSWORD = "..."
```

## 4. 离线演练（不连接真实邮箱）

先复制样例输入到临时目录，或用 `--runtime` 指定独立运行目录，避免污染生产数据库。

### 4.1 配件工作簿（xlsx / xls）

```powershell
.venv\Scripts\python.exe -m cater.app.cli process `
  --sender tester@example.com `
  --kind parts `
  --destination 法国 `
  --source "input\配件\1x920_BEECS-6613427.xlsx"
```

输出：一个配件出口台账 Excel，状态通常为 `READY_TO_SEND`；在 dry-run 下只创建 `delivery` 发件箱记录，不真实发送。

### 4.2 整机 `.msg`（含正文表格 + xlsx 附件）

```powershell
.venv\Scripts\python.exe -m cater.app.cli process `
  --sender liu_haoming@cat.com `
  --kind auto `
  --destination 印尼 `
  --source "input\整机\回复 Re 印尼 ....msg"
```

`.msg` 由 `extract-msg` 解析：正文 HTML/纯文本进入核心业务表格提取，xlsx/xls 附件作为业务来源；原始 `.msg` 保留用于审计，但不会作为核心的工作簿来源。整机成功时生成两个 Excel（企业物料上传、发运沟通表）。真正 BIFF `.xls` 由 xlrd（纯 Python）解析，无需安装 Excel。

### 4.3 标准 `.eml`

```powershell
.venv\Scripts\python.exe -m cater.app.cli process --eml "mail.eml"
```

`.eml` 走正常 MIME 解析、允许名单、去重和补件关联；不会真实回复。

### 4.4 附件型离线来源

`--source` 也接受 `.eml`；对 `.msg`/`.eml` 会提取 xlsx/xlsm/xls 附件。非工作簿附件不会进入核心业务来源。离线命令的 `--sender` 必须命中当前允许名单。

## 5. 常驻运行

### 5.1 前台运行（推荐首次核对）

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\run-web.ps1
```

浏览器打开 `http://127.0.0.1:8765/`。该进程同时运行网页与轮询器；`Ctrl+C` 停止。纯离线查看界面：

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\run-web.ps1 -NoPoll
```

仅轮询、不启动网页：

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\run-poller.ps1
```

### 5.2 Windows 任务计划程序

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
```

注册：

- `CATER-App`：用户登录时启动网页 + 轮询；异常退出自动重启（最多 3 次，每次间隔 1 分钟）。
- `CATER-Backup`：每天 02:00 创建并校验数据库备份。

查看/启动/停止：

```powershell
Get-ScheduledTask -TaskName "CATER-*"
Start-ScheduledTask -TaskName "CATER-App"
Stop-ScheduledTask  -TaskName "CATER-App"
```

移除：

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -Remove
```

也可以使用 NSSM、WinSW 等包装为 Windows 服务，但必须保持单实例并让网页只绑定回环地址。**本仓库未在 Windows 上完成上述服务/任务计划的实际验收**；上线前需在目标机验证登录触发、休眠恢复、异常重启和文件夹名称。

### 5.3 网页安全

- 服务端拒绝非回环 `Host` 头；`--host`/`web.host` 只允许 `127.0.0.1`、`::1`、`localhost`。
- 所有写操作要求会话 Cookie + CSRF Token；缺失或错误返回 403。
- 下载只允许：
  - 当前任务版本登记的输出 artifact（按数据库 ID，不按客户端路径）；
  - `runtime/diagnostics/` 下白名单文件名的诊断包。
- 页面不会显示或提交邮箱密码。
- 会话签名密钥保存在 `runtime/web-secret`（权限尽力设为仅当前用户）。

## 6. 任务状态与日常操作

| 状态 | 含义 | 常用处理 |
| --- | --- | --- |
| `RECEIVED` | 已接收，待处理 | 启动恢复会转为 `RETRY_PENDING` |
| `VALIDATING` | 校验中 | 崩溃后启动恢复 |
| `PROCESSING` | 核心转换中 | 崩溃后启动恢复 |
| `WAITING_INPUT` | 缺少目的地/字段/映射/来源，已发追问 | 原发起人按格式补件 |
| `RETRY_PENDING` | 可重试错误或核心暂不可用 | `retry`，或等待轮询自动重试 |
| `READY_TO_SEND` | 输出已生成，发件箱已建立 | `deliver` / 网页创建交付 |
| `COMPLETED` | 已交付（或 dry-run 已完成流程） | 需要修改时显式重新处理 |
| `FAILED` | 重试耗尽或不可恢复错误 | 查看日志与问题，修复后 `retry` |
| `SEND_UNKNOWN` | SMTP 结果不确定 | 禁止盲目重发，见第 8 节 |
| `CANCELLED` | 终止 | 不自动清理未完成任务资料 |

常用命令：

```powershell
.venv\Scripts\python.exe -m cater.app.cli health
.venv\Scripts\python.exe -m cater.app.cli tasks --status WAITING_INPUT
.venv\Scripts\python.exe -m cater.app.cli task task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli retry task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli reprocess task_xxxxxxxx --regenerate-only
.venv\Scripts\python.exe -m cater.app.cli reprocess task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli confirm-reprocess task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli confirm-layout task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli deliver task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli outbox
```

- `reprocess --regenerate-only`：生成新版本和输出，但不创建交付邮件。
- `reprocess`：生成新版本并创建交付；若任务处于需要明确确认的状态，先执行 `confirm-reprocess`。
- `confirm-layout`：把任务当前版本采用的表格布局确认为该类业务的基准布局
  （网页任务页有等价按钮）。首次使用的新布局会在交付正文中给出人工重点核对建议；
  人工核对无误后执行一次确认，之后同类布局不再重复提示，相对基线的结构变化
  （表头行/列位/TOTAL 行移动）会在正文中列出。确认只记录结构指纹与工作表，
  不记录正文。详见 docs/flexible-input.md §4。
- 旧版本和旧输出保留；管理操作始终作用于当前版本。
- `deliver` 只对 `READY_TO_SEND` 有效；dry-run 时只标记 `dry_run`。
- 后台轮询每轮会发送到期的发件箱、重试可重试任务并执行每日维护。

两个与附件有关的问题代码需要区分处理：

- `WORKBOOK_DEPENDENCY`（运行环境缺解析组件，如 `xlrd`）：这是部署问题，
  不是发件人缺附件——追问正文会明确写“附件无需重发”。维护人员在运行环境
  执行一次性 `.venv\Scripts\python.exe -m pip install -e .` 补齐依赖后，直接
  `retry task_xxxxxxxx`（或等待轮询自动重试）即可：附件已保存，不删除、不改动，
  无需对方重新发送，任务从当前版本继续。
- `AMBIGUOUS_INPUT`（多份内容不同的附件都符合本票要求）：整票暂停，追问正文
  列出候选文件名，本次未采用任何一份，已保存附件不会被删除或改动。请维护人员
  确认应使用的附件后继续本任务；若要更换其中一份，由发起人沿用原附件名重发该
  文件即可（同名替换只作用于新版本，旧版本输入行与文件保留审计）。
  内容完全相同的重复附件（同名或换名）只算一个来源候选，不会触发本问题代码。

### 6.1 补件格式（只接受明确结构化指令）

只接受**原任务发起人**、且能通过任务编号或 `In-Reply-To`/`References` 明确关联的补件。歧义邮件会暂停，不猜测。正文中的转发/引用历史会被剥离，避免旧指令重复生效。

也接受**完整业务表补件**：发起人直接把包含表头与 MSO 列的完整业务表粘贴进回复
（HTML 表格或以 Tab/空格明确分列的纯文本均可），无需任何修正指令。新版本的
核心处理把补件表与原始邮件表按 MSO 显式合并——缺失字段填充、一致去重、真实
冲突暂停并列出两处来源；引用历史里的旧表不参与；补件表来源按版本存档
（`task_version_mails`），重试使用同一冻结来源。

支持的行格式（每行一条）：

```text
任务编号：task_xxxxxxxx
目的国：法国
国家映射：Alias=FRA
修正：record_key.field=value
MSO=...
SO=...
```

规则：

- `国家映射` 的别名会校验；与已有映射冲突时拒绝，绝不静默覆盖。可在同一封补件中先新增别名再用作 `目的国`。
- `修正` 必须带记录键和字段名；一条补件对同一字段给出不同值会报冲突。
- 连续多封**部分表格补件**按 MSO 跨封累积：先发只含序列号的表、再发只含
  价格的表，两封的值都会保留（早先填充不被后封抹掉），只有同一封内同一
  MSO 重复出现才报 `DUPLICATE_RECORD`。表格来源邮件逐版本继承——后续
  只回目的国、只换附件、或显式重新处理都不会丢失已补的表格来源。
- 原始邮件业务表**结构损坏**（同字段多列 `AMBIGUOUS_COLUMN`、多版不一致
  `MULTIPLE_BUSINESS_TABLES`、合并单元格越界 `TABLE_STRUCTURE_INVALID`）
  且一行有效数据都解析不出来时，干净的补件完整表会**显式接管**：登记
  非阻断留痕 `TABLE_SUPERSEDED` 后按补件表继续，任务不再被原始表的结构
  问题永久卡住。这是保守的显式动作而非静默忽略——接管会留痕；真实缺失
  字段仍按 `MISSING_FIELD` 如实暂停；原始表与补件表给出不同真实值时仍走
  `FIELD_CONFLICT`，绝不静默覆盖。
- 邮件业务表的合并单元格尺寸有上限（colspan/rowspan ≤ 64、逻辑网格列数
  ≤ 256）：外部 HTML 的跨度整数不可信，越上限按 `TABLE_STRUCTURE_INVALID`
  整体阻断，绝不部分交付。
- `MSO=` / `SO=` 仅在任务恰好只有一个未完成记录键时可用；否则必须写完整 `修正：...`。
- 含糊的自然语言（如“改成法国吧”）不会被解释为修改。
- 附件补件必须是可识别的 xlsx/xls 工作簿；无法保存附件时任务保持等待并记录问题。

### 6.2 国家与机型资料

```powershell
.venv\Scripts\python.exe -m cater.app.cli add-country-alias 印尼 IDN --reason "confirmed by owner"
.venv\Scripts\python.exe -m cater.app.cli import-machines "database\整机HS CODE Review 2026-03.xlsx"
```

网页“参考资料”也提供等价操作。机型导入读取 Sheet1，并整体写入；同机型可因目的国范围不同而共存（例如 915F 的默认 110KW 与巴西 BRA 的 123KW），导入表中的目的国范围、特殊标记、G 列核对信息、来源行和 `special_rule_confirmed` 都会保留并在网页显示。若同一机型、同一目的国范围出现申报资料不同的重复行，核心会拒绝整次导入并要求维护为明确别名/特殊配置，应用不会绕过去重规则；导入失败不会留下部分机型。`special_rule_confirmed=false` 的特殊条目会原样透传给核心，由核心暂停并登记未确认规则（例如 SEM919BRZ/BRA），不会因为应用侧缺省而被当成已确认。**升级到 schema 5 后必须重新执行一次 `import-machines`**：迁移前导入的特殊条目默认标记为未确认，重新导入会用核心结果刷新该标记。

## 7. 日志、备份与诊断

### 7.1 日志

运行日志为 JSONL，默认保留 90 天，含任务/邮件/发件箱等操作元数据，自动脱敏密码、令牌、邮件正文和附件内容。查询：

```powershell
.venv\Scripts\python.exe -m cater.app.cli logs --task-id task_xxxxxxxx
.venv\Scripts\python.exe -m cater.app.cli logs --level ERROR --limit 100
.venv\Scripts\python.exe -m cater.app.cli logs --query "SMTP"
```

### 7.2 备份

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\backup.ps1
# 或
.venv\Scripts\python.exe -m cater.app.cli backup --reason manual
.venv\Scripts\python.exe -m cater.app.cli verify-backup runtime\backups\cater-....sqlite3
```

备份使用 SQLite 在线备份 API，随后立即 `quick_check` 并校验 schema 版本/任务数；默认保留最近 30 份。备份不含邮箱密码。

### 7.3 恢复

1. 停止网页/轮询进程和计划任务。
2. 确认备份通过 `verify-backup`。
3. 执行恢复（必须先确认）：

```powershell
.venv\Scripts\python.exe -m cater.app.cli restore runtime\backups\cater-....sqlite3 --yes
```

恢复前会自动创建 `pre-restore` 安全备份。恢复后重启服务并执行 `health`。任务文件目录不包含在数据库备份中；如同时丢失，需从文件备份恢复 `runtime/tasks/`。

### 7.4 保留与清理

```powershell
.venv\Scripts\python.exe -m cater.app.cli cleanup --logs --tasks
```

- 运行日志按 `logging.retention_days`（默认 90 天）删除。
- 任务文件只清理 `COMPLETED`/`CANCELLED` 且超过 `logging.task_file_retention_days`（默认 365 天）的目录。
- 未完成任务（含等待补件）永不自动清理。
- 数据库中的任务状态和配置审计长期保留。

### 7.5 诊断包

```powershell
.venv\Scripts\python.exe -m cater.app.cli diagnostics
```

生成 `runtime/diagnostics/*.zip`，只含运行元数据、健康摘要和脱敏日志尾部；不含凭据、原始邮件、附件、输出文件。网页“诊断”可导出并通过受控文件名下载。

## 8. 发送结果不确定（SEND_UNKNOWN）

应用在调用 SMTP **之前**先把发送意图写入发件箱。若进程崩溃、连接中断或 SMTP 响应丢失，记录进入 `SEND_UNKNOWN`，任务也进入 `SEND_UNKNOWN`。此时：

- **不要**直接再次发信；可能造成重复交付。
- 先人工检查邮箱“已发送”文件夹，或执行：

```powershell
.venv\Scripts\python.exe -m cater.app.cli outbox <outbox_id>
.venv\Scripts\python.exe -m cater.app.cli outbox-action <outbox_id> recheck
```

`recheck` 会在已发送文件夹按确定性 Message-ID 查找：

- 找到：记录/任务转为已发送完成。
- 未找到：仍需人工确认，再执行：

```powershell
# 确认实际已发出
.venv\Scripts\python.exe -m cater.app.cli outbox-action <outbox_id> confirm-sent
# 确认未发出，允许重新排队
.venv\Scripts\python.exe -m cater.app.cli outbox-action <outbox_id> confirm-not-sent
```

失败重试使用 `outbox-action <id> retry`；dry-run 记录先 `release` 才会转入真实发送（且仍需满足发送开关和名单）。

## 9. 故障排查

| 现象 | 检查/处理 |
| --- | --- |
| `core_unavailable` / `RETRY_PENDING` | 核心未安装或导入失败；`pip install -e .`，检查 `PYTHONPATH`/包安装，再 `retry` |
| 长期 `WAITING_INPUT` | 查看 `task` 的 open issues 与 `required_action`；只接受原发起人的结构化补件 |
| 邮件未进入任务 | 检查 `allowlist.mode` 与当前名单、发件人最外层 From、自动回复/退信过滤、`logs` |
| 首次运行没有处理旧邮件 | 设计如此；确认后 `poll --history` 仅首轮处理历史 |
| 重复邮件 | 按 `Message-ID`/原始哈希去重；检查是否有不同 Message-ID 的重复投递 |
| 附件未识别 | 仅支持 xlsx/xlsm/xls 业务附件（按扩展名或工作簿 MIME 识别）；检查大小上限与日志中的 `attachment_*` 警告 |
| 机型导入失败（915F 等重复） | 同机型同目的国范围但申报不同会被核心拒绝；确认是否为已定义的特殊范围（如 915F/BRA），未定义的特殊值不得推广猜测 |
| 网页 400 | 访问地址必须是 `127.0.0.1`/`localhost`，服务拒绝非回环 Host |
| 网页 403 | CSRF Token 缺失/过期；刷新页面重试 |
| 发信仍不发出 | 同时检查 `enabled`、`dry_run`、当前名单是否包含收件人、账号/凭据/服务器 |
| 数据库锁 | 确认只有一个轮询进程；不要在网络盘运行 SQLite；查看日志 `database is locked` |
| 电脑休眠后漏信 | 服务恢复后按 UID 游标补取；若服务器 UIDVALIDITY 变化会安全重置并重新同步 |

健康检查：

```powershell
.venv\Scripts\python.exe -m cater.app.cli health --json
```

输出包含 schema 版本、`quick_check`、任务计数、`outbox_unknown`、发送开关、当前名单数量、主机/账号是否配置、凭据是否存在（布尔值）。

## 10. 升级与回滚

1. 停止服务与计划任务。
2. 执行数据库备份并 `verify-backup`。
3. 更新代码，重新 `pip install -e ".[dev]"`。
4. 运行 `init`（应用迁移；迁移先验证后提交，失败不留下半升级结构）。
5. 启动服务，执行 `health` 和一条离线样例。
6. 如需回滚：停止服务，恢复升级前的备份，再切回旧代码。

## 11. 测试与验收边界

离线测试命令（临时 runtime，不连接真实邮箱）：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

`pyproject.toml` 已设置 `pythonpath = ["src"]`，无需手动设置 `PYTHONPATH`；默认 `testpaths = ["tests"]` 会运行应用与核心的全部测试。仅在未安装 editable 包时也无需额外路径配置。

> **测试环境版本说明。** 本轮测试在 Python 3.13 / macOS 上完成，`pyproject.toml` 的锁定版本与实际安装版本一致：**pytest 9.1.1**、**lxml 6.1.1**、**soupsieve 2.9.1**、openpyxl 3.1.5、beautifulsoup4 4.15.0、et_xmlfile 2.0.0。本手册引用的通过证据即来自这组版本，不需要再安装其他 pytest 版本。验收机应安装 `.[dev]` 后重跑，并记录 `python -m pytest --version` 的实际输出；若将来确有新版本功能需求，应先说明具体原因并重新验证后再修改锁定。

本手册所述 Windows 任务计划、Windows 凭据管理器、真实 IMAP/SMTP 收发和休眠恢复**尚未在 Windows 目标机验收**；代码仅在开发环境通过离线模拟邮箱和真实核心样例转换测试。上线前必须由运维在目标机逐项验证并记录。

## 使用 .env 统一配置邮箱

将仓库根目录 `.env.example` 复制为 `.env`，填写邮箱地址、IMAP/SMTP服务器、端口、SSL/STARTTLS和两个授权码。以后通过现有启动脚本运行即可自动加载，无需每次输入密码或设置CMD环境变量。`.env` 是本机明文配置，已被Git忽略；只提交无真实凭证的 `.env.example`。

邮箱参数优先级为系统环境变量 > `.env` > `config/config.toml`。密码优先级为系统环境变量 > `.env` > Windows凭据管理器。白名单、测试/生产模式和发信开关继续通过TOML或管理网页维护，`.env`不会自动启用发信。网页已保存的运行设置仍优先于TOML。

值按字面量读取，不执行命令、不展开 `$变量`；密码建议使用单引号或双引号包裹，不支持多行值或引号后的行尾注释。修改后重启程序生效。默认读取启动工作目录的 `.env`，仓库启动脚本会自动切换到项目根目录；高级场景可用系统环境变量 `CATER_ENV_FILE` 指定其他文件。
