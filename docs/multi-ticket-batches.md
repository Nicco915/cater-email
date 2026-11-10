# 多票批次（阶段 2）：批次持久化、人工确认与幂等建票

一封邮件携带多票业务（多份发票/箱单）时，阶段 1 提供离线预览
（`preview-tickets`，不写库）。阶段 2 在其上提供完整可操作闭环：

```
本地 .eml 导入 → 持久化批次 + 不可变方案版本 → Web 人工核对/归属调整
→ 显式确认并创建逐票任务（同一事务）→ 任务保持挂起等待后续阶段
```

本阶段建出的任务**全部挂起**：不处理、不重试、不外发，任何执行入口
（含补件指令）都显式拒绝并给出可读原因。生产 poller 不做分票识别。

## 使用

### CLI

```bash
# 离线导入本地 .eml（写库；重复导入同一邮件只返回既有批次）
python -m cater.app.cli import-batch --eml path/to/mail.eml [--json]

# 批次列表 / 批次详情（候选、未归属项、阻断问题、任务）
python -m cater.app.cli batches [--limit N] [--json]
python -m cater.app.cli batch <batch_id> [--plan <plan_id>] [--json]
```

与 `preview-tickets` 的区别：preview 是纯计算不落库；`import-batch`
持久化批次/方案/材料，供 Web 人工确认。两者都不联网、不收发邮件。

### Web（仅回环地址）

- `GET /batches`：批次列表 + `.eml` 上传导入（仅 .eml，大小受
  `mail.max_message_bytes` 限制；不接受服务器路径）。
- `GET /batches/{id}`：方案版本切换、候选明细（身份/金额/数量/重量、
  附件区域溯源、正文行引用、核对记录）、未归属正文行与附件、阻断问题、
  排除审计、任务、材料、事件。
- `POST /batches/{id}/adjust`：对**未归属**正文行/附件做结构化调整
  （指派到唯一候选 / 有理由排除 / 撤销），每次调整产生新的不可变
  方案版本并重新核对。
- `POST /batches/{id}/confirm`：每个候选必须显式选入或填写排除理由；
  携带方案版本与指纹（乐观锁）。确认即在同一事务内创建逐票任务。
- `GET /batches/{id}/assets/{asset_id}`：下载批次材料（仅限本批次资产）。

所有 POST 走既有 CSRF + Origin/Host 校验。页面无 JSON 编辑器、无
"一键忽略"入口。

## 数据模型（migration 14）

| 表 | 作用 | 关键约束 |
| --- | --- | --- |
| `mail_batches` | 批次身份与状态 | `UNIQUE(account, owner, mail_hash)`；`status` open→materialized 单向 |
| `batch_plan_versions` | 不可变方案版本 | `UNIQUE(batch_id, version)`；含 `source_fingerprint`、`inspector_version`、preview/assignments JSON、derived_from |
| `batch_candidates` | 方案内候选与人工决定 | `UNIQUE(plan_id, candidate_key)`；decision pending/selected/excluded + 理由 + 操作者 |
| `batch_assets` | 不可变原始材料 | `UNIQUE(batch_id, role, sha256)`；逐文件 sha256 |
| `batch_tasks` | 批次↔任务关联 | `UNIQUE(batch_id, candidate_key)`、`UNIQUE(task_id)`；`execution_hold=1` + hold_reason |
| `batch_events` | 批次审计事件 | 追加式，含 actor 与明细 JSON |

身份分层：

- **批次身份** = `(account, owner, mail_hash)`（内容哈希）。同一封邮件
  重复/并发导入 = 同一批次；不同发件人/邮箱/内容 = 不同批次。
- **`source_fingerprint`** 只是某次识别的指纹（含识别器版本、别名
  版本、人工调整），**不是**邮件身份。识别规则升级后重复导入同一
  邮件：同一批次上追加新方案版本，绝不建新批次。
- `mail_messages.task_id` 保持单票语义：批次父邮件行的 task_id 为
  NULL（filter_reason=multi_ticket_batch_import），多票归属只走
  `batch_tasks`。

## 确认边界（服务端强制）

- 方案仍有阻断问题 / 未归属正文行 / 未处理的非装饰附件 → 拒绝。
  无法读取的 Excel 必须显式排除（填理由），不会默认忽略。
- 候选必须被**完整覆盖**：每个候选要么选入、要么带理由排除；不允许
  悄悄丢票；选入+排除重叠、空选择、未知候选一律拒绝。
- 带未解决问题（needs_confirmation / 阻断）的候选不能选入，只能先
  调整或有理由排除——数值/身份冲突没有"忽略全部"按钮。
- 乐观锁：确认必须基于当前方案版本且指纹一致；过期页面/过期方案
  直接拒绝。
- 调整操作只接受**当前方案版本中**的未归属目标；指派目标必须是
  存在的候选；"已读但无身份"的工作簿才能指派，其余附件只能排除。

## 幂等与事务边界

- **导入**：材料先落盘（独占唯一暂存 + fsync + 原子改名，0600），再单
  事务建批次/资产/事件。附件按内容哈希去重：同内容不同名附件只入库
  一份，原名并入审计。只有确认为**批次身份**唯一冲突
  （同一 `mail_hash` 已被并发/重复请求建立）才按重复导入返回既有批次；
  其余 IntegrityError 是明确失败并清理本次孤儿材料。首轮识别在 DB
  提交后于已持久化材料上纯计算执行。
- **首轮失败恢复**：首轮 `_run_inspect` 失败会留下无方案批次；重复
  导入经 `recover_initial_plan` 可重入恢复 v1（并发恢复/重复导入只产生
  一个版本，INSERT 唯一冲突按"取既有"处理）。
- **调整**：纯计算（重跑识别）先于写库；新方案版本单事务插入，
  事务内复核批次仍 open 且基于当前方案（防并发调整丢版本）。
- **确认建票**：①**先在同一可靠同步边界内复核状态**（同进程批次确认
  锁 + 跨进程 BEGIN IMMEDIATE）：批次仍 open、方案仍为当前版本、指纹
  一致——任何过期/冲突/幂等请求（含旧方案请求迟到恢复）在**改动任何
  已发布文件之前**即被拦下，绝不先删除/覆盖他人已确认的快照目录；
  ②复核通过后为每票发布**本请求独占不可变快照目录**
  （`snapshots/<plan_id>/<cand>-<随机令牌>`，独占创建，已引用目录
  永不覆盖），附件按哈希校验复制 + snapshot.json 全量业务校验
  （schema/版本/指纹、候选全业务内容、正文行、附件清单+逐文件哈希+
  路径边界；时间戳等非业务元数据规范化忽略），提交前再全量复核一次；
  ③单个事务内更新候选决定 → 逐票建任务（`tasks._insert_task_conn`，
  可组合入事务，无嵌套 BEGIN）→ batch_tasks（持久化 `snapshot_hash`
  可信快照哈希）→ 批次物化。任何失败整体回滚：**要么完整创建同一组
  任务，要么零任务零确认**；确认失败的私有目录作为未引用残留，绝不
  阻塞重试（重试发布新独占目录）。
- 双击/并发/重启后相同请求（同方案+同指纹+同选票）返回既有同一组
  任务；不同方案/指纹/选票组合显式冲突，不静默加票。
- 崩溃残留：快照目录在确认事务内发布、任务关联随事务提交；两者之间
  崩溃或事务中途失败会留下未引用目录。`orphan_snapshot_report()` /
  `sweep_orphan_snapshots()` 回收未引用目录：sweep 删除前在同一可靠
  同步边界（批次确认锁 + BEGIN IMMEDIATE 事务）内**重新核验引用**，
  目录（或其子孙）刚被确认引用即放弃删除；发布路径含随机令牌，复核
  与删除之间不可能被他人重新引用，已引用目录**绝不**被改写或删除。
  只回收含文件的叶子残留；空的方案中间目录/遗留容器保留不动（无实
  际危害，且任何锁外 rmdir 都可能删掉活跃事务刚 mkdir 的在制品）。
  TTL 只是保守宽限，正确性由同步边界内的引用复核保证。
- 篡改防护：每次识别与建票前逐文件 sha256 校验批次资产；快照按完整
  业务内容校验（发布时 + 提交前各一次），确认持久化可信哈希并支持
  `verify_batch_task_snapshot(task_id)` 事后复核；本地 JSON 永不当作
  权威事实。
- 跨方案/候选残留的旧目录（身份不符）不再被任何人覆盖或删除，合法
  新方案确认直接发布新独占目录，绝不阻塞；输入隔离保持（每票独立
  快照目录、批次资产原件不被移动）。

## 执行挂起（hold）

`batch_tasks.execution_hold=1` 的任务在所有入口被拒绝
（`StateTransitionError`，含批次号与可读原因）：

- `process_task` / `retry_task` / `reprocess_task` / `confirm_reprocess`
  / `deliver_task`；
- 自动重试队列 `due_for_retry` 与启动恢复 `recover_incomplete` 的
  SQL 直接排除挂起任务；
- 补件/重试指令邮件路由到挂起任务时被过滤（`batch_task_held`）并
  附带引导说明（`GUIDANCE_BATCH_HELD`）；
- 回收站删除（单个与批量）拒绝，批次审计必须完整保留；
- 批次本身不提供删除入口（审计不可销毁）。

解除挂起属于后续阶段的显式动作，本阶段无释放入口。

## 与生产链路的兼容

- 生产 poller 不做分票识别；批次只通过显式离线导入产生。
- 批次父邮件（OFFLINE-BATCH 文件夹、合成 UID）不进入正常任务流；
  同内容邮件之后到达生产轮询时按 `duplicate_raw_content` 过滤。
- 针对批次父邮件的回复若无唯一子任务路由（任务编号），按既有
  `ambiguous_task_reference` / `unknown_task_id` 规则拒绝并引导。
- 生命周期/清理不触碰批次共享原件；快照按候选独立复制。

## 真实回放

三封真实研究邮件离线回放（隔离 runtime，结果不入库到生产）：

| 邮件 | 候选数 | batch_ready | 说明 |
| --- | --- | --- | --- |
| 1433 | 8 | true | 干净多票，可直接进入人工确认 |
| 1434 | 1 | true | 单票；3 个非工作簿图片附件待显式排除 |
| 1435 | 4 | false | 4/4 候选需确认（冲突），保持未确认 |

回放产物（数据库/材料/报告）位于 `runtime/multi-ticket-batches-review/`
（gitignored，不入版本库）。

## 未解决限制（留给后续阶段）

- 挂起任务没有释放/执行入口：解除 hold、接入正常处理/外发属于
  下一阶段设计。
- 已物化批次不可改方案、不可补票；错误确认只能留审计，后续阶段
  再考虑受控撤销。
- 批次无删除入口（含 Web/API/CLI），磁盘清理只能手工进行。
- 正文行指派解决"候选间歧义"与"自动无法定位"两类情况；行内
  机器级错误（交叉/缺价）不会被指派掩盖，仍按问题呈现。
