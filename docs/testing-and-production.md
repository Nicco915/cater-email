# Windows 实单测试与生产切换步骤

日期：2026-11-04。适用：完整源码目录部署，IMAP/SMTP统一放根目录 `.env`。

## 1. 环境区别与一键入口

是的，建立两个本地TOML文件；不用通过修改同一个文件反复切换。

| 入口 | 配置 | 独立数据目录 | 管理网页 |
|---|---|---|---|
| `start-test.bat` | `config/test.local.toml` | `runtime/test/` | http://127.0.0.1:8766/ |
| `start-production.bat` | `config/production.local.toml` | `runtime/production/` | http://127.0.0.1:8765/ |

首次启动缺少本地配置时，BAT从对应 `.example.toml` 复制。BAT使用UTF-8代码页、英文提示，自动定位项目目录，显式传递配置、运行目录和端口，不受 `.env` 中 CATER_CONFIG/CATER_RUNTIME_DIR 的默认值影响。真实本地配置和runtime均不上传Git。

选择BAT即选择环境：两套数据库、附件、输出、日志、游标、配置修改与去重记录独立。test不是自动禁发信；其真实发信也受两个发送开关和测试名单控制。首次默认禁发，已有数据库则保留此前管理页中的开关，不会每次强制重置。

## 2. 一次性安装

1. 将仓库完整下载到如 `C:\CATER`，保留templates和scripts。
2. 安装Python（建议3.13），在项目目录PowerShell执行：

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\install.ps1
```

安装器初始化的默认 `runtime/` 不是上述两个环境目录；后续用BAT创建独立目录，不将其数据直接搬入生产。

3. 复制 `.env.example` 为 `.env`，填写真实邮箱地址、IMAP/SMTP主机、端口、加密方式、密码/授权码。无需再执行credentials set。
4. 复制 `config/test.example.toml` 为 `config/test.local.toml`，复制 `config/production.example.toml` 为 `config/production.local.toml`。也可让首次BAT自动复制后停止程序再编辑。
5. 将样例中的tester/operator示例地址替换为自己的真实测试/业务发件地址，均按最外层From配置；两份配置初期保持：

```toml
[sending]
dry_run = true
enabled = false
```

6. 客户原件不在公开仓库，手动在本机放置经授权的input、output测试基准和机型数据库；完整测试还需本机邮件HTML测试副本。不要把真实资料或 `.env` 提交Git。

## 3. **初始化测试数据**

停止其他CATER进程，执行：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test init
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test import-machines "database\整机HS CODE Review 2026-03.xlsx"
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test settings --json
```

检查模式为test、名单正确、dry_run为true、enabled为false。若测试数据库以前已使用，网页保存的配置会覆盖TOML基线；用网页设置或settings修改，不通过删生产库解决。

## 4. 离线验证转换

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test process --sender tester@example.com --kind parts --destination 法国 --source "input\配件\1x920_BEECS-6613427.xlsx"
```

将sender替换成测试名单中的地址。整机用本机实际msg路径：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test process --sender tester@example.com --kind machine --destination 印度尼西亚 --source "input\整机\你的整机邮件.msg"
```

若msg未内含箱单，可再加一个 `--source "箱单发票.xlsx"`。仅看管理页且不收信时运行：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test serve --port 8766 --no-poll
```

成功任务在 `runtime/test/tasks/<task_id>/v1/outputs/`；用Windows Excel打开检查数据、表头、字体、批注、打印布局。整机两份、配件一份。核实SO前导零、国家、单价、数量和净重；SEM520申报要素按数据库19600KG原文而箱单单台净重20000，属于已确认规则。

## 5. 收信测试，先不回复

1. 停止离线网页和生产进程，双击 `start-test.bat`。
2. 打开8766网页，确认禁发开关与测试名单；观察首轮日志成功建立邮箱游标。
3. 首次扫描默认不处理历史邮件。必须等首轮成功后再发送新测试邮件；不要为方便直接扫描整个日常收件箱历史。
4. 从测试名单邮箱发送业务邮件，新增正文写 `目的国：法国`，附配件Excel；整机则包含正文表格和箱单Excel。
5. 等待约60秒，在任务详情检查解析、文件、日志；outbox应为dry_run，不会收到真实回复。
6. 从非名单邮箱发送一封测试，验证忽略且不回复。

## 6. 打开测试回复，验证完整闭环

在8766管理页“设置”中保持test模式，确认仅测试地址，显式设置dry_run=false、enabled=true并保存。该动作会发真实测试邮件。再发一封新业务测试邮件验证回复；已有dry_run记录不会自动全部发出。

按顺序完成下表，每项记录任务编号、实际结果：

| 测试 | 预期 |
|---|---|
| 配件正常单 | 回复一个Excel，首条毛重/件数/发票号正确 |
| 整机正常单 | 回复两个Excel，MSO对应正确 |
| 缺目的国 | 整票等待、邮件追问；原发起人回复 `目的国：法国` 后继续 |
| 缺字段或双来源冲突 | 汇总问题并暂停，无部分交付；按追问中的字段格式修正后继续 |
| 未知国家别名 | 追问，原发起人回复 `国家映射：Alias=FRA`；确认后保存到测试库 |
| 同单再次发送 | 提示重复/重处理，不静默重复交付 |
| 程序重启 | 状态与文件保留，不无条件重复发送 |
| 非原发起人补件 | 拒绝更新原任务 |

如需核查健康或有效配置：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test health --json
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test settings --json
```

## 7. 相同邮件反复测试

不关闭全局去重、不删除生产数据。选择原测试任务的“重新生成”，或执行：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test reprocess task_xxxxx --regenerate-only
```

生成v2/v3且不交付；完整再次回复时去掉 `--regenerate-only`，前提是测试发信已显式开启。新来信提示需确认时使用网页对应确认按钮或 `confirm-reprocess task_xxxxx`。相同Message-ID/原文被传输去重时直接操作已有任务，不靠重发强行绕过。

需要完全干净的测试时，停止程序，用新的独立目录而不删旧目录：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test-run-002 init
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test-run-002 import-machines "database\整机HS CODE Review 2026-03.xlsx"
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test-run-002 serve --port 8766
```

该目录首次启动仍只建游标，待启动后重发新邮件，或用离线process读取相同样例。BAT固定runtime/test；临时目录测试使用上述显式命令。

## 8. 切换生产

1. 停止测试程序（Ctrl+C或关闭其BAT窗口），确认8766不再轮询。不同时监控同一个邮箱文件夹。
2. 检查production.local.toml中的production名单、mode=production、初期禁发。测试/生产都用同一 `.env` 邮箱，但不共享数据库。
3. 初始化并单独导入机型库：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/production.local.toml --runtime runtime/production init
.venv\Scripts\python.exe -m cater.app.cli --config config/production.local.toml --runtime runtime/production import-machines "database\整机HS CODE Review 2026-03.xlsx"
```

4. 仅迁移经过确认的参考资料，不复制测试数据库、任务、outbox和游标；测试阶段新增的国家别名通过生产管理页单独复核后添加。
5. 双击 `start-production.bat`，打开8765，核对生产路径、模式、名单。首次成功扫描建立生产水位前不要开始正式投递；切换空档收到的业务邮件应核对补发，不能假定新库会自动接管旧邮件。
6. 保持禁发先用生产名单中的受控地址做一票核对；确认正确后在生产管理页显式启用enabled=true、dry_run=false，再发新的一票做受控交付。
7. 通知业务发件人正式投递。生产文件在runtime/production，不在runtime/test或根input/output。
8. 使用网页设置时如需从test切换production会要求显式确认；两个独立配置的mode已分别固定，避免日常切错。

## 9. 长期自动启动及备份

现有register-tasks.ps1/run-web.ps1/backup.ps1默认读取 `.env` 的 CATER_CONFIG 和 CATER_RUNTIME_DIR。手动BAT则显式绑定环境。通过这两个变量让计划任务也指向生产：

```dotenv
CATER_CONFIG=config/production.local.toml
CATER_RUNTIME_DIR=runtime/production
```

这只选择目录，不会自动开启发信。保持本机系统环境中没有同名旧变量覆盖 `.env`；执行health确认最终路径。停止手动生产BAT后再注册并启动计划任务：

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
Start-ScheduledTask -TaskName "CATER-App"
Get-ScheduledTask -TaskName "CATER-*"
```

计划任务使用8765，登录后启动；每日02:00备份生产数据库。不要同时双击production BAT再启动第二个生产实例。电脑保持联网且不休眠，网页浏览器可关闭。Windows凭据若使用必须属于运行任务的Windows账号；使用 `.env` 时该账号需能读取文件。

数据库备份不等于附件备份：另定期复制整个runtime/production到备份盘。先在目标Windows验证任务重启、注销/重启行为、备份可用及3项PowerShell动态测试。当前实现不是开机无人登录的Windows服务。

## 10. 临时返回测试及回退

先停止生产计划任务：

```powershell
Stop-ScheduledTask -TaskName "CATER-App"
Disable-ScheduledTask -TaskName "CATER-App"
```

再启动test BAT。生产目录保持原样，测试完成后关闭test，启用并启动原生产任务：

```powershell
Enable-ScheduledTask -TaskName "CATER-App"
Start-ScheduledTask -TaskName "CATER-App"
```

短暂停产后用原生产目录恢复会按原游标补取；不要用新目录替代，否则失去此前去重与发送记录。发送结果不确定的任务先核对已发送邮件，不盲目重新处理。普通测试切换不需要restore备份或删除数据库。

如确需测试和生产同时在线，先建立独立邮箱或收信规则分流至不同文件夹。`.env`中的CATER_FOLDER会覆盖两份TOML的folder，分文件夹部署需移除该统一项并分别设置；同一邮件不能同时进入两套处理器。

## 11. 验收记录

记录Windows/Python版本、测试日期、测试/生产路径、有效配置、每项测试任务编号、Excel人工检查、实际回复收件人、自动启动和备份恢复结果。凭证不写入记录。离线开发通过不代表真实邮箱与Windows实机已经验收。
