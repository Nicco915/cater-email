# 多票批次（阶段 4）：每票目的国确认运行手册

阶段 2 把一封多票邮件导入为持久化批次并人工确认建票（每票一个
`execution_hold=1` 的挂起任务与不可变快照）；阶段 3 提供对单票的显式
离线执行（`run-batch-offline`）。**阶段 4 在两者之间补上唯一缺失的业务
输入：对每一票显式确认目的国。**

用户已明确选择的方案：**每票独立确认，可勾选若干票批量填写同一个国家；
不存在批次全局默认国家**。本阶段不解除挂起、不自动执行、不发邮件、不
创建新任务或新版本。国家确认之后仍需按阶段 3 手册显式运行
`run-batch-offline` 才会产出模板文件。

## 1. 范围与安全边界

允许（本阶段**全部**授权写入）：

- 把所选票当前 `tasks.destination_name/destination_alpha3` 与对应当前
  `task_versions` 的目的国字段更新为确认值；
- 为每张**改值**票追加一条 `task_events.code='batch_destination_confirmed'`
  审计（含前后值、理由、操作者、参考版本、本次选择范围），并在有改值时
  追加一条承载选择范围的 `batch_events` 批次审计。

明确禁止/不会发生：

- 不改变任务的 `status`/`version`/`next_retry_at`、不释放
  `execution_hold`、不写 `outbox`、不创建任务或新版本；
- 不改未勾选的票（空值保持空值、修订保持、零审计事件）；
- 不改快照、批次材料、参考数据，不读写父邮件正文；
- 不读 `.env`、不访问钥匙串、不联网、不实例化网关/轮询/交付服务；
- 不信任客户端提交的中文名/alpha3 对应关系、owner 或任何路径；国家一律
  由服务端经既有参考服务（启用国家 + 启用别名）解析复核；
- 不自动确认：真实样本国家未知时保持未确认，离线执行如实
  `waiting_input(MISSING_DESTINATION)`，绝不按正文/收件人猜国。

上述语义由 `cater.app.batch_destinations.BatchDestinationService` 实现；
Web 与 CLI 都只是组合该服务与既有 db/references/tasks/logger 的薄入口。

## 2. 前置条件

1. 批次已完成阶段 2 人工确认建票（`mail_batches.status='materialized'`）；
   未物化批次打开确认页/命令会被明确拒绝。
2. 目标票在该批次确认方案的 `batch_tasks` 中且 `execution_hold=1`、未删除/
   未归档、状态仍为 `RECEIVED`（尚未开放执行）。
3. 本地数据库与参考资料已存在（同一 runtime）：国家/别名来自既有参考库，
   目的国只能是**启用**国家或可解析的启用别名（UI 使用三位码如 `FRA`）。
4. 每次确认必须给出**明确业务理由**（写入逐票审计；必填，最长 500 字）。

## 3. Web 操作

入口：已物化批次的详情页 `GET /batches/{batch_id}` 顶部有
“确认目的国（逐票）”按钮，直达 `GET /batches/{batch_id}/destinations`。

页面每票一行，显示：票号/候选、当前目的国（未设置为空值标识）、最近
确认记录（确认人/时间/修订号）、本次目的国下拉（只列启用国家，默认
“不修改/请选择”）、任务状态与 hold 标识。页面不预选任何国家、不自动
勾选任何票。

确认方式（两种互斥，同时填写会被**明确拒绝**，不会悄悄二选一）：

1. **逐票确认**：勾选若干票，为每张选票在自己的下拉里选国家；
2. **批量同国**：勾选若干票，在“统一国家”下拉里选一个国家，应用于全部
   选票（不是批次默认值，未选票不受影响）。

填写确认理由后提交。表单还携带服务器渲染的 `plan_id`、
`reference_version` 与每票 `revision_<task_id>` 隐藏字段；提交时服务端在
**同一事务内**重新复核全部事实（批次物化状态、确认方案一致、所选票归属、
挂起状态、国家启用、参考版本、逐票修订），任何一项过期或非法则**整批
零写入**并回显错误，请刷新页面后基于最新值重试。

安全保护沿用既有机制：CSRF 令牌、Origin/Host 校验、模板转义。无令牌/
错令牌/跨站或空 Origin 的提交一律 403 且不产生任何写入。

## 4. CLI 操作

以下命令与 `run-batch-offline` 同为纯离线入口：只读 `config.toml` 的
`runtime.dir` 与 `core.header_aliases`，不读 `.env`/OS 凭据，不创建网关，
绝不执行核心。

### 4.1 只读查看每票目的国

```bash
python -m cater.app.cli batch-destinations <batch_id> \
  --config /abs/path/config.toml --runtime /abs/path/runtime --json
```

输出 `plan_id`、`reference_version` 与逐票 `task_id`/`candidate_key`/
`destination_name`/`destination_alpha3`/`revision`/`confirmed_by`/
`confirmed_at`/`task_status`/`execution_hold`。本命令只读，不确认、不执行。

### 4.2 显式确认所选票目的国

```bash
python -m cater.app.cli confirm-batch-destinations <batch_id> \
  --task-id <task_id_1> --task-id <task_id_2> \
  --country FRA \
  --reason "客户邮件明确要求改发法国" \
  --config /abs/path/config.toml --runtime /abs/path/runtime --json
```

- `--task-id` 可重复；重复指定同一票会被拒绝（不自动去重掩盖选择错误）；
- `--country` 一个，应用于全部所选票；不同国家分多次调用；
- CLI 先读取当前 `plan_id`、参考版本与每票修订，再提交事务复核——与
  Web 表单同一事务边界，过期照样拒绝；
- 退出码：0 成功（含同值无操作）；1 输入/业务拒绝（未知国、跨批次、
  过期修订、未物化等），拒绝时**零写入**；2 配置错误；3 核心组件不可用
  （本命令不依赖核心）。

确认**不会**自动执行离线转换；需要产物时按阶段 3 手册对每票显式运行
`run-batch-offline` 到各自新目录。

## 5. 事务与并发语义

- 确认在 **BEGIN IMMEDIATE** 单事务内完成：先完整校验所有选择，全部合法
  后才写；任何一项非法（未知/停用国家、跨批次任务、非挂起票、非
  RECEIVED、过期方案、过期/伪造修订、参考版本漂移）都**整体回滚**，
  未选票与落选票保持原样。
- 每票修订 = 该票最新 `task_events.code='batch_destination_confirmed'`
  事件的**真实 id**，无记录为 0；不凭秒级 `updated_at`。
- 重复提交相同值且修订匹配为**无操作**：不改行、不追加审计；修订不匹配
  （含同值）按过期明确拒绝。
- 并发安全依赖事务内复核：两个操作者基于同一旧修订提交，只有一方能
  成功，另一方收到过期错误并刷新重试；不会出现静默覆盖。
- 审计可追踪每次确认的 `actor`、`reason`、前后值、参考版本与本次选择
  范围（`selection_scope`）。

## 6. 与离线执行（阶段 3）的衔接

- 离线执行只读取**该票当前确认来源**：`run-batch-offline` 报告新增
  `destination` 字段 `{name, alpha3, revision, confirmed_by,
  confirmed_at}`，取自同一公共读取接口，使改国后的新离线结果可追溯
  到具体确认事件；未确认时各值为空、`revision` 为 0。
- 目的国进入产物：整机 SEM发运沟通表 V 列为目的国中文名；配件出口台账
  N 列（最终目的国）为三位码。重开产物即可核对最终目的国。
- 改国后请换**新的 `--output-dir`** 重跑；旧输出目录、旧报告、快照文件
  保持逐字节不变（离线执行不回写任何旧现场）。
- 缺国票保持 `waiting_input(MISSING_DESTINATION)`、零产物，不会自动
  确认或猜测。

## 7. 不外发边界

目的国确认与后续离线执行都不存在以下路径：不读 `.env`/`CATER_*` 邮件
环境变量/钥匙串；不构造网关/轮询/交付服务；不写 `runtime/mail`、
`runtime/tasks` 生产 outputs、`outbox`、批次快照；不建立 IMAP/SMTP
连接，不发任何邮件。国家确认只回答“这票发往哪里”，**释放挂起、批量
执行与交付外发仍未实现**，不得把本阶段产物当作已释放或已交付。

## 8. 验收测试

独立黑盒验收：`tests/app/test_stage4_acceptance.py`（全合成数据，只经
Web/CLI 用户入口驱动，不用直接 SQL 或内部 `update_destination` 跳过
确认流程；数据库仅只读核对）。

```bash
cd /tmp
env -u CATER_IMAP_SSL -u CATER_IMAP_HOST -u CATER_FOLDER \
    -u CATER_MAIL_ACCOUNT -u CATER_IMAP_PORT -u CATER_IMAP_STARTTLS \
    -u CATER_CONFIG -u CATER_RUNTIME_DIR -u CATER_ENV_FILE \
    PYTHONPATH=/abs/path/to/repo/src \
    python3 -m pytest /abs/path/to/repo/tests/app/test_stage4_acceptance.py -q
```

覆盖：

1. 整机两票经 Web 逐票确认不同国家（FRA/DEU）→ 显式离线执行 → 重开
   SEM发运沟通表核对 V 列最终目的国与别票不混入，报告 destination 可
   追溯（修订=真实事件 id、确认人、确认时间）；
2. 配件两票经 CLI 一次批量同国 → 台账 N 列为 FRA，报告可追溯；
3. 只确认选中票：未选票保持空/修订 0/零审计，离线仍
   `waiting_input(MISSING_DESTINATION)` 零产物；
4. 改国后新目录结果与报告为新值/新修订，旧产物/快照/旧报告逐字节不变；
5. 未知国、跨批次任务、部分非法（全成或全不成）、过期/伪造修订、
   逐票国家与统一国家歧义提交：全部拒绝且**零写入**；
6. Web CSRF/Origin/Host 防护：403 且零写入；详情页入口存在；
7. 未确认目的国离线执行如实阻断，绝不自动确认/猜国；hold/outbox/
   status/version/快照全程不变。

## 9. 故障排查

| 现象 | 原因/处理 |
| --- | --- |
| `批次尚未完成确认建票（未物化）` | 批次还在阶段 2；先按既有边界人工确认建票。 |
| `国家 'XXX' 不可用` | 国家未知或已停用/别名停用；只能选启用国家，必要时先维护受控别名。 |
| `修订已过期` / `参考数据版本已变化` | 表单基于旧值；刷新页面或用 `batch-destinations` 取最新修订后重试，不会部分写入。 |
| `不属于该已物化批次` | 任务来自别的批次；按批次分别确认。 |
| `未处于批次挂起状态` / `状态不可确认` | 该票已离开本阶段可确认范围；停止操作并上报协调者，不手工改库。 |
| 同一票逐票国家与统一国家都被填写 | 明确拒绝；只保留一种选择方式后重试。 |
| 离线报告 `MISSING_DESTINATION` | 该票尚未确认目的国；按本手册确认后换新输出目录重跑。 |

## 10. 与相邻阶段的边界

- 阶段 2：导入、方案版本、人工确认建票、hold 与可信快照；
- 阶段 4（本文件）：逐票显式目的国确认（Web/CLI），只写目的国与审计；
- 阶段 3：显式离线执行与报告（报告含 destination 追溯）；
- 后续阶段（**未实现**）：释放 hold、批量/自动执行、交付外发、失败重试
  UI。不得用本阶段产物冒充已释放或已交付。
