# 第三阶段并行开发公共契约

用户恢复工作并要求约80%开发工作交给DeepSeek Harness，20%交给Claude；Codex负责计划、监控、审核。重大业务/架构问题必须通过Orca ask上报协调者，暂停依赖该决定的工作，不猜测。使用当前checkout并行开发，严格按各自文件所有权，不修改其他worker文件；需要改公共契约先ask。不要再派子代理。

## 基线与阶段范围

第一阶段分票预览、第二阶段批次确认/建票均已完成。Codex恢复后复验暂停前关键4项回归通过；第二阶段最后完整测试报告550 passed/3 skipped。现全部为未提交修改，严禁git restore/reset/checkout覆盖。docs/testing-and-production.md为用户既有修改，禁止触碰。

第三阶段目标：已人工确认的批次子任务，使用经过可信哈希校验的独立快照，进行显式离线转换/生成结果。不得释放execution_hold，不接生产poller，不发邮件、不启用SMTP/IMAP、不批量自动处理、不读取/打印.env或钥匙串。业务冲突、目的国/参考库缺失等保持waiting_input，不能为了生成文件而编造。

真实三封样本仅用于本地只读/隔离回放：input/邮件研究/2026-11-10_Junjie-Qiu/{1433,1434,1435}/original.eml。研究资料output/邮件研究/2026-11-10_Junjie-Qiu_多票格式研究.md。真实数据和回放报告仅存被忽略runtime/或output/，不可复制进可跟踪fixture、文档、日志消息。用合成去标识化数据做测试。

## 不可变接口（必要变更先ask）

A 新模块 cater.app.confirmed_input：
```
build_confirmed_request(snapshot: dict, *, snapshot_path: str | Path,
 task_id: str, destination: dict | None, references: dict,
 output_dir: str | Path, header_aliases: dict | None = None) -> dict
```
纯函数/只读验证：输入来自调用方 BatchService.verify_batch_task_snapshot 且ok=True后的快照；必须声明该可信边界，不凭客户端verified布尔信任。只接受snapshot列出的本票assets和mail_rows，检查文件边界/哈希，输出现有process_job请求+新增confirmed_input字段。confirmed_input={schema_version:1,candidate:<深拷贝>,mail_rows:<深拷贝>,source_fingerprint:<str>}。sources为本票资产列表，mail只由这些行字段重建的规范业务表（HTML转义），绝不读父邮件补字段；references/destination/overrides等保持原契约。缺失/篡改/路径逃逸抛现有ValidationError，不生成任何文件。

B 新模块 cater.core.parsing.machine_confirmed：
```
parse_confirmed_machine_sources(sources: list[dict], candidate: dict,
 mail_rows: list[dict], aliases=None) -> MachineWorkbookResult
```
C 新模块 cater.core.parsing.kit_confirmed：
```
parse_confirmed_kit_sources(sources: list[dict], candidate: dict,
 mail_rows: list[dict], aliases=None) -> PartsParseResult
```
B/C复用现有只读加载、Source/Issue/FieldValue/模型和语义别名；通过已确认身份、明确区域逐项核对，不能把preview candidate.facts当最终明细真值，也不能跳过正式校验。每条取值定位原workbook单元格；多个文件同票、多个候选最终sheet冲突要显式问题，无按文件顺序胜出。不得修改既有解析器行为。B/C对外仅新增模块，由Claude统一接api.py，所有预览冲突仍保留。

Claude（E）集成：process_job仅在显式confirmed_input、校验通过后走B/C新路径，旧请求行为不变。新增离线执行服务/CLI，核心可以写本次隔离输出，但不触碰正式任务状态/版本/重试/hold/outbox；成功/失败/部分产物明确保留报告。本阶段只CLI，不新增释放或发信UI；Web保持阶段2。输出身份必须来自服务端确认任务/计划，不接受任意客户端JSON、路径、owner。默认不覆盖已有输出；同任务并发离线执行不能混写。

各worker最后worker_done必须有修改文件/接口、真实测试命令和结果、局限、需协调者处理的集成事项。非零测试不得当成功；不提交、不推送。配置测试从/tmp等无.env目录通过绝对路径运行。Orca ask后按其要求结束当前轮等待答复；普通task执行直到交付，不能只写计划就结束。
