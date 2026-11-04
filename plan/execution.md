# 开发任务分配与集成记录

启动日期：2026-11-04。用户已授权开始代码开发，允许执行 agent 使用 sub-agent。邮箱凭证、实际允许发件人由用户配置；开发期间禁止真实业务发信。

## 并发阶段 A

- Claude Code：业务核心（P0样例核查、P2配件、P3整机、模板保真及对应测试）。拥有 src/cater/core/、tests/core/、docs/field-mapping.md、docs/template-audit.md、templates/。
- OpenCode：基础与应用（P1状态持久化、P4邮箱闭环、P5管理网页、P6部署文档及对应测试）。拥有 src/cater/app/、tests/app/、pyproject.toml、config/、scripts/、docs/operations.md、README.md、.gitignore。
- 协调者：公共契约、任务派发、集成适配、独立验收与审核。拥有本目录计划文件和 tests/integration/；不覆盖执行者正在修改的文件。
- 原始 input/output/database、AGENTS.md 不允许修改；输出测试使用临时目录。两个 agent 共用当前工作区，禁止 git reset/clean/checkout 或提交其他人的文件。

## 公共接口 v1

不跨目录导入内部类型；对接用 JSON 可序列化 dict，避免两边争改公共模型。

核心导出 src/cater/core/api.py：

`process_job(request: dict) -> dict`

request: task_id(str)、kind(parts/machine/auto)、destination({name,alpha3})或null、sources([{path,filename,mime_type}])、mail({html,text,message_id})、references({machines:[{model,english_name,hs_code,product_name,declaration}],countries:[{alpha3,name,aliases:[]}],machine_aliases:{}})、output_dir(str)、overrides(list，缺省空)。

overrides: [{record_key,field,value,source_message_id}] 是原发起人明确确认的修正，需审计来源；不能静默覆盖无法对齐的记录。

response: status(ready/waiting_input/unsupported)、kind、records(list)、issues([{code,record_key,field,message,sources:[],required_action}])、artifacts([{path,filename,sha256}])、provenance(list)、business_identity(str或null)、content_hash(str)。永不发送邮件或写业务数据库。缺国家应报告问题，不能异常退出；未知错误可抛出供应用记录。core不得依赖app。

核心另导出 `import_machine_reference(path: str) -> list[dict]`，仅读Sheet1，用于应用初始化。应用延迟导入核心 API，未就绪时以注入的假引擎进行基础测试，禁止创建同名假核心文件。

SQLite和网页在 app 内部自行组织；应用负责凭证、白名单、任务状态、参考资料快照、outbox、回复及重试。core开发者交付自己所需的依赖清单 docs/core-dependencies.txt；只有OpenCode修改pyproject.toml，合并依赖通过协调者消息。

## 阶段 B：集成及返工

两者完成独立测试后，协调者安装依赖并进行端到端离线验收、模板样式核查、状态恢复和假SMTP测试。问题派回归属agent修复。未经真实邮箱配置不标记生产验收通过。

## 阶段 C：用户环境验收

用户填写IMAP/SMTP和测试/生产白名单；先dry-run与测试模式，再在Windows验证收发、开机运行和恢复。禁用正式发送为出厂默认值。生产启用需要用户明确操作。

## 派发记录（2026-11-04）

- Run：run_b03a6f441166。
- Claude Code：task_98c5ec6ef655 / ctx_584a501fd926，已启动。
- OpenCode：task_c9e9d704dd53 / ctx_3b345832421f，已启动。
- 两个任务均通过Orca监督执行，结果回传后进入协调者测试、审核及返工阶段。

## 协调者独立测试（2026-11-04）

- 标准命令 `python3 -m pytest -q`：133 passed in 25.46s。此前3项特殊机型测试失败已在当前快照消失。
- 限制：当前是macOS/Python3.13环境；依赖锁定安装、Windows实机、真实IMAP/SMTP仍未验收。
- 审核返工：Windows PowerShell5.1部署脚本需显式检查外部命令退出码，避免pip失败却报告安装成功。
- 核心OOXML保真修正已落盘，等待Claude正式返工报告与独立差异检查。

## 最新审核状态（2026-11-04）

协调者独立全量测试158 passed、3 skipped。清理模板及填值已改为OOXML并纳入原样例→模板→输出保真回归。依赖清单同步实际测试版本，Claude在干净venv复测核心81项通过。详见plan/acceptance.md；Windows与真实邮箱仍待用户配置后验收。

两个执行agent均已完成并经审核返工。最终执行者全量159通过/3跳过；协调者已独立验证前一完整快照158通过及新增依赖检查。开发阶段结束，转入用户Windows/邮箱验收；未提交。
