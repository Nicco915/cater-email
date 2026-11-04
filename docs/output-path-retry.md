# 输出路径错误与任务重试

日期：2026-11-04。

`core artifact is missing: 出口台账表体.xlsx` 的已知原因是相对运行目录：核心返回包含runtime的相对文件路径，应用又添加一次outputs目录，导致验证时找不到文件。修复后任务请求使用绝对输出目录，响应校验同时兼容绝对路径、工作目录相对路径和仅文件名，仍拒绝目录越界及哈希不一致。

这条报错不是模板缺失提示。三个受控模板随仓库提供，位置是：

- templates/parts_ledger.xlsx
- templates/machine_materials.xlsx
- templates/machine_comm.xlsx

不需要把生成的“出口台账表体.xlsx”手动放到模板目录，原始input/output也不是运行期模板路径。如显式设置CATER_TEMPLATE_DIR，则该目录必须包含以上三个文件。

Windows修复步骤：停止程序，git pull，然后start-test.bat。已有任务通常进入RETRY_PENDING，后台会尝试恢复；也可在任务详情点击“重试处理”。如果想手动控制，先启动不轮询界面再点重试，不要同时反复点多个重试按钮。

命令行等效操作（先停止常驻进程）：

```powershell
.venv\Scripts\python.exe -m cater.app.cli --config config/test.local.toml --runtime runtime/test retry task_b1278e79a9d85e4feb282b42
```

不要删除数据库或重新初始化来解决输出路径问题。补充目的国已保存时不需重新回复；若仍有必需字段缺失，程序会再次暂停并仅说明本次需补充的信息。

邮件提示面向操作人员：不展示错误代码、英文内部消息和全部可用指令；缺目的国时仅给目的国回复示例，字段冲突时展示对应编号、字段中文名和双方值，其他技术错误保留在管理页与日志。历史邮件补扫边界单独记录在plan/follow-ups.md，按用户要求暂缓修改。
