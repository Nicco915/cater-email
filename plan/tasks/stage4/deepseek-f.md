# F DeepSeek：每票目的国确认服务（45%）
先读plan/tasks/stage4/common.md。只编辑本组新服务与测试，实现确定的公共接口。
重点：country按ReferenceService.resolve+get_country启用状态复核（包括别名），不可从收票地址推断；明确当前batch_tasks/confirmed_plan归属、hold=1、任务未删除/未归档、只允许未开放执行的RECEIVED票。乐观修订采用目的国审计事件真实id，不用秒级updated_at；参考版本必须在事务内复核。读取和写入都不得跨票泄露父邮件正文或其他owner数据。
输入非空、唯一task_id、理由非空、更新字段白名单、revision类型严格；一个非法即零写入。初次/改值写task当前版本目的国和前后审计；同值无操作或明确过期拒绝，不增加版本/状态，不动hold/outbox/快照。get展示票号可从服务器快照/候选身份取，但只读已保存preview JSON，不依赖不可信前端事实。
测试覆盖启用/禁用国家、别名、跨批次/未物化/非held/删除任务、伪造revision/plan/参考版本、并发双连接、批量中途失败回滚、部分选择、同值/改值审计、无网络和凭据访问。代码简洁复用现有模型，不新增不必要迁移。有契约问题ask，完成worker_done证据并结束轮次，协调者会关闭窗口。
