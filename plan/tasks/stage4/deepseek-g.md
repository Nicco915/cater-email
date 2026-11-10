# G DeepSeek：目的国Web/CLI及离线来源报告（35%）
先读plan/tasks/stage4/common.md。只编辑该文件列明G所有权文件，不改F或H文件。F并行实现接口，先做UI/CLI调用和契约测试，完成后协调者通知接线复验。
实现每票国家表单与勾选批量同国，当前值/空值明确，显示国家中文名与代码、确认人/时间和过期修订保护。提交仅修改显式选中的票；整批默认值不存在。不预选某个国家、不默认选全部票。原有批次详情加“确认目的国”入口，保持布局兼容。全部POST使用既有安全机制，源数据转义，错误信息不带他票明细。不可绕过materialized/hold等F服务校验。
CLI使用纯离线config/最小服务，不调用带.env读取的_service或AppService；明确--task-id选择列表与--country、--reason，不自动执行核心。输出每票确认结果，失败正确exit；不要把客户端重复参数无声去重掩盖选择问题。
batch_offline.py只新增当前任务destination确认审计报告字段，使用F读取结果，不改快照核验、source_hash、独占输出和状态边界。缺国家保持waiting_input；目的国更改后新运行应新报告值，旧报告不改。
相关Web/CLI测试覆盖单票不同国、批量同国、未选不变、CSRF/Origin/Host、跨批次/过期/未知country、HTML恶意值、CLI不读.env不连网、报告追溯和原模板/字段兼容。没有F模块先ask等待，不自行写第二个服务。完成worker_done明确测试及界面证据路径，窗口由协调者关闭。
