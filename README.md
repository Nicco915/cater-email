# CATER — 邮件驱动出口单证本地应用

CATER 在本地 Windows 电脑上监控工作邮箱，只处理允许名单发件人的业务邮件，调用离线转换核心生成配件/整机出口 Excel，并以邮件交付原发起人。所有邮件传输、附件、输出、任务状态和审计数据均保存在本机，网页仅绑定回环地址。

## 组成

- `src/cater/app/` — 应用层：配置、SQLite 迁移与状态机、IMAP/SMTP 适配器、去重/补件、任务编排、发件箱、CLI、本地管理网页、备份/诊断。
- `src/cater/core/` — 业务转换核心（独立开发），通过 `cater.core.api.process_job` 的 JSON 契约调用；应用不导入其内部类型。
- `templates/` — 配件台账、整机企业物料/发运沟通表模板。
- `config/config.example.toml` — 配置样例；实际 `config/config.toml` 不入库。
- `scripts/` — Windows 安装、启动、备份和计划任务脚本。
- `docs/operations.md` — 安装、配置、离线演练、常驻运行、补件、备份恢复和故障处理。

## 快速开始（离线）

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config/config.example.toml config/config.toml
PYTHONPATH=src .venv/bin/python -m cater.app.cli init
```

在 `config/config.toml` 中把测试发件人加入 `allowlist.test`，保持 `dry_run = true`、`enabled = false`，然后：

```bash
PYTHONPATH=src .venv/bin/python -m cater.app.cli process \
  --sender tester@example.com --kind parts --destination 法国 \
  --source "input/配件/1x920_BEECS-6613427.xlsx"

PYTHONPATH=src .venv/bin/python -m cater.app.cli health
PYTHONPATH=src .venv/bin/python -m cater.app.cli tasks
```

启动本地管理网页（同时轮询邮箱）：

```bash
PYTHONPATH=src .venv/bin/python -m cater.app.cli serve
# http://127.0.0.1:8765/
```

或仅查看界面、不轮询：`serve --no-poll`。

Windows 用户使用 `scripts/install.ps1`、`scripts/run-web.ps1`，详见 [docs/operations.md](docs/operations.md)。部署仅支持完整源码目录 + editable 安装：核心从仓库 `templates/` 读取 Excel 模板，普通 wheel 安装无法找到这些模板；不要用非 editable wheel 部署。

## 邮件与数据规则

- 只处理当前允许名单（`test` 或 `production` 二选一）；离线 `process` 同样受限。
- 首次扫描默认只建立游标、不处理历史；显式 `poll --history` 才导入首轮历史。
- 一封邮件对应一类业务；配件读取 xlsx 明细，整机合并 xlsx 与邮件正文表格。
- 任一必需字段/映射/机型/来源冲突都暂停整票并追问，不交付部分文件。
- 补件只接受原发起人、可明确关联的邮件，且只解析固定格式：`任务编号`、`目的国`、`国家映射：Alias=FRA`、`修正：record.field=value`、唯一记录时的 `MSO=/SO=`。
- 同一票重复提交提示已处理；显式重新处理生成新版本并保留旧版本。
- 发信意图先落库。SMTP 结果不确定进入 `SEND_UNKNOWN`，禁止盲目重发，只能核对已发送文件夹或人工确认。
- 真实发信需同时满足 `sending.enabled=true`、`dry_run=false`、收件人在当前名单。
- 凭据只从环境变量或 Windows 凭据管理器读取；日志/诊断/网页均不包含密码，诊断包也不含邮件正文、附件和输出。

## CLI 命令

```text
version  init  health  tasks  task  outbox  logs  poll  serve  process
retry  reprocess  confirm-reprocess  deliver  outbox-action  settings
add-country-alias  import-machines  backup  verify-backup  restore
cleanup  diagnostics  credentials
```

完整参数见 `python -m cater.app.cli <command> --help`。

## 测试

测试使用临时 runtime、模拟邮箱和 fake SMTP，不访问真实邮箱：

```bash
.venv/bin/python -m pytest -q
```

`pyproject.toml` 已设置 `pythonpath = ["src"]`，无需手动导出 `PYTHONPATH`；该命令运行 `tests/` 下的全部应用与核心测试。

本轮测试在 Python 3.13 / macOS 上完成，锁定的业务与测试依赖与实际安装版本一致：`pytest 9.1.1`、`lxml 6.1.1`、`soupsieve 2.9.1`、`openpyxl 3.1.5`、`beautifulsoup4 4.15.0`、`et_xmlfile 2.0.0`。验收机应安装 `.[dev]` 后重跑，并记录 `python -m pytest --version` 的实际输出；上述版本不需要额外升级。

核心样例集成仅读取 `input/` 与 `database/`（不修改），输出写入临时目录。

## 状态

应用层与核心契约已接通；配件 xlsx 和整机 `.msg`（正文表格 + xlsx 附件）均在开发环境通过真实核心离线转换。Windows 计划任务、Windows 凭据管理器、真实 IMAP/SMTP 收发的目标机验收尚未完成，见 `docs/operations.md` 第 11 节。

## 本地业务样例

公开仓库不包含客户原始邮件、发票、箱单和机型数据库，也不包含从邮件提取的完整 HTML 测试副本。`input/`、`output/`、`database/` 及 `tests/core/fixtures/machine_mail.html` 仅保留在本机。样例集成测试依赖这些文件，新环境需自行放置经授权的样例后运行完整测试；模板和程序代码随仓库提供。


## 使用 .env 统一配置邮箱

将仓库根目录 `.env.example` 复制为 `.env`，填写邮箱地址、IMAP/SMTP服务器、端口、SSL/STARTTLS和两个授权码。以后通过现有启动脚本运行即可自动加载，无需每次输入密码或设置CMD环境变量。`.env` 是本机明文配置，已被Git忽略；只提交无真实凭证的 `.env.example`。

邮箱参数优先级为系统环境变量 > `.env` > `config/config.toml`。密码优先级为系统环境变量 > `.env` > Windows凭据管理器。白名单、测试/生产模式和发信开关继续通过TOML或管理网页维护，`.env`不会自动启用发信。网页已保存的运行设置仍优先于TOML。

值按字面量读取，不执行命令、不展开 `$变量`；密码建议使用单引号或双引号包裹，不支持多行值或引号后的行尾注释。修改后重启程序生效。默认读取启动工作目录的 `.env`，仓库启动脚本会自动切换到项目根目录；高级场景可用系统环境变量 `CATER_ENV_FILE` 指定其他文件。


## 一键测试与生产切换

Windows 双击 `start-test.bat` 或 `start-production.bat`。分别绑定独立的本地TOML、runtime/test或runtime/production目录、8766或8765端口，首次自动复制配置样例。窗口提示为英文，使用UTF-8代码页。首次安装与完整实单测试、重复测试、生产切换、计划任务绑定及回退步骤见 [测试与生产切换手册](docs/testing-and-production.md)。

Windows首次实单测试可先双击 `initialize-test.bat` 自动安装缺失环境、初始化测试库并选择/导入机型参考Excel，再填写 `.env` 和测试白名单，双击 `start-test.bat`。服务就绪后自动打开本地网页；启动/初始化窗口在退出或失败后保留，方便复制错误。详见测试与生产切换手册的“快速路径”。

生产首次配置可双击 `initialize-production.bat`，独立初始化生产库和机型资料。收到重复或同票变更提示后，原发起人可直接回复“确认重新处理”，不需要在电脑端点击；仍执行完整校验和发送门禁。详见 [生产初始化与邮件重新处理](docs/mail-reprocessing.md)。
