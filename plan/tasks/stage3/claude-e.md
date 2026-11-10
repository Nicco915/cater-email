# E：Claude Code，公共入口与离线执行集成（预计20%）
先读common.md和阶段2审核历史。独占允许文件：src/cater/core/api.py、src/cater/app/batch_offline.py（新）、src/cater/app/cli.py、tests/app/test_batch_offline.py、tests/core/test_confirmed_api.py。不改其他worker模块、batches.py/数据库/任务hold/生产链路。
按公共契约接线A/B/C（其他3worker并行实现，初期可以先写合约测试及集成骨架；模块未完成不将其缺失当业务失败，必要ask等消息）。新增离线单票执行服务：BatchService.verify_batch_task_snapshot必须ok=True，再读取可信快照经A生成请求；references/目的国只从既有参考服务与该任务读取，缺失按waiting_input，不猜；调用核心process_job。
核心api显式confirmed_input路径使用B/C，仍走现有reconcile、transform、模板writer、status/layout/provenance/金额及HS等正式校验；旧单票契约不变。若对sources范围/完整性或确定schema无法验证，先拒绝，不能落回全父邮件。确认冲突candidate不能靠传confirmed_input跳过。
CLI新增run-batch-offline或等效明确名字，参数task_id+显式新输出目录（需验证属于已确认batch_tasks）；只允许本地显式执行，不读用户生产.env或真实网关、不触发收发信。为此使用现有离线配置机制或独立最小服务构建，不实例化会发送的自动流程。
执行不释放execution_hold、不改变正式task状态/版本/next_retry/confirmation，不建outbox，不写生产outputs；每次隔离输出有报告记录task/plan/snapshot_hash/reference版本或哈希、产物哈希/结果issues；并发相同输出路径使用独占创建，拒绝覆盖输入/快照/既有输出，失败不得展示旧文件为成功，不能删除既有目录。异常路径有可读状态，不泄露秘密。已有坏参考/缺模板如实waiting/failed不伪造。
测试：snapshot verifier返回False/篡改必须拦截、父邮件禁止读取、两票不串、hold/outbox/task状态保持、同目录并发、无输出覆盖、legacy API回归、真正调用新parser而非只转发preview。A/B/C未就绪可先完成骨架与合约测试，待就绪后同dispatch继续集成，不自己重写其代码。全部就绪后跑相关集成/共享回归，worker_done报告。重大问题ask。
