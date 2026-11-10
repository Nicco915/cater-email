# 第四阶段：每票目的国确认（用户已选推荐方案）

用户明确选择：每票独立确认，可批量填写同一国家；不是批次全局默认。用户最新要求所有修改都commit，协调者负责验收后统一commit；worker不得自行commit/push，以免并行混杂。用户希望DeepSeek窗口完成后自动关闭：worker_done成功后结束轮次，协调者保存记录并关闭自己的精确窗口。

上一阶段已验收并提交144e262（多票/离线）、cc3346f（用户原文档修改），基线808 passed/3 skipped。禁止丢失基线；docs/testing-and-production.md不改。凭据.env被忽略0600，不读取/打印/复制，不访问钥匙串，不真实收发信/联网，不改真实数据库或邮件。真实商业资料/研究产物不入跟踪fixture、文档、日志。

本阶段仅新增已物化批次held子任务的显式目的国确认。保持execution_hold=1，无释放/自动执行/发信/新任务/任务新版本/重新解释快照。金额/HS/真实各票国家没有用户数据则不猜，需要业务决定用Orca ask并停依赖工作。每票国家取现有启用国家/别名，客户端只提供国家选择和任务选择，不信任客户端中文名/alpha3对或owner/路径。所有选票同事务全成或全不成，未选票不变，改值要有理由和逐票前后审计。输出仅离线旧流程，旧产物/快照/旧报告不重写。

## 文件所有权与预计比例

F DeepSeek 45%：新src/cater/app/batch_destinations.py、tests/app/test_batch_destinations.py。
G DeepSeek 35%：src/cater/app/web/app.py、src/cater/app/web/templates/batch_destinations.html（新）、batch_detail.html、src/cater/app/cli.py、src/cater/app/batch_offline.py（只报告目的国来源）、tests/app/test_batch_destination_web.py、tests/app/test_batch_destination_cli.py。
H Claude 20%：tests/app/test_stage4_acceptance.py（新）、docs/multi-ticket-destinations.md（新）、docs/multi-ticket-offline.md（修正原无入口说明）。禁止编辑F/G代码与测试；发现问题反馈协调者。

同checkout并行，各组不改其他组文件或再派子代理。公共接口有缺口先ask。普通代码不需要业务确认，不因为小实现问题问用户。非零测试如实报告，worker_done用type=worker_done不是status；结束后不继续其他工作。

## F不可变公共接口

```
BatchDestinationService(db, references, tasks, logger)
.get_batch_destinations(batch_id: str) -> dict
.confirm_destinations(batch_id: str, *, plan_id: str,
 updates: list[dict], actor: str, reason: str,
 expected_reference_version: int | None = None) -> dict
```
get返回{batch_id,plan_id,reference_version,countries,rows}。countries为现有启用国家{name_zh,alpha3}，rows含task_id/candidate_key/destination_name/destination_alpha3/revision/confirmed_by/confirmed_at/task_status/execution_hold。plan_id是materialized批次confirmed_plan_id，未物化拒绝。
updates每项{task_id,country,expected_revision}；country为现有国家代码/可解析别名（UI用alpha3）；expected_revision非负整数（拒绝bool）。revision为该票最新task_events.code='batch_destination_confirmed'的id，没记录为0（不凭秒级updated_at）。可用既有task_events+batch_events实现，不必迁移；返回revision必须是新事件真实ID。重复同值可无操作（不追加审计）或明确过期拒绝，不覆盖并发更新。
confirm在BEGIN IMMEDIATE事务里复核批次/确认计划/所选票确实归属/hold=1/当前revision/国家启用与参考版本，再全量更新所选tasks及对应当前task_versions的destination字段，追加逐票task_events与batch_events（actor、reason、前后值、参考版本、选择范围）。不能调用会嵌套BEGIN的TaskRepository.update_destination；不改状态、版本、retry、outbox、快照或其他任务。部分非法全部回滚，过期表单拒绝。

## G接线边界

Web GET/POST /batches/{batch_id}/destinations：新目的国页，每票显示票号/现值/确认记录/修订标识；每票可选择自己的国家，可勾选若干票使用同一个国家。未选票不变；不是批次默认，没有忽略冲突或解除hold按钮。显式提交后写值，既有CSRF/Origin/Host/转义防护完整，跨批次task/未知字段/重复与伪造revision处理清楚。GET详情加入口。服务用F类组合service.db/references/tasks/logger，无须改application.py。
CLI建议batch-destinations <batch_id>只读展示；confirm-batch-destinations <batch_id> --task-id可重复 --country FRA --reason明确业务理由。显式调用获取当前计划/revision再提交（事务再复核），不用读.env/凭据或创建网关；可复用BatchOfflineRunner(load_offline_config(...))最小服务组合，绝不执行核心。国家确认不自动run。
离线报告增加destination={name,alpha3,revision,confirmed_by,confirmed_at}（当前票确认来源），让后续改目的国产生的新离线结果可追溯；不改旧报告。缺国仍MISSING_DESTINATION。不要修改已验收TOCTOU/执行边界。

## 测试基线

各组先相关测试；H集成完成再全仓/tmp无.env绝对路径跑一次。测试/本地UI用合成数据和独立runtime，不用真实生产凭据/数据。H证明用户可只通过Web/CLI选国家后离线ready（不使用SQL/内部TaskRepository.update_destination模拟“已确认”），两票不同国、批量同国、改国后新产物/报告追溯、旧文件原样、CSRF/并发/回滚/未选票保持、hold/outbox/状态不变。
