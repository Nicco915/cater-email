# B组第一次审核返工：数量文本不能截取数字

遵守plan/tasks/stage3/common.md和deepseek-b.md，仅修改src/cater/core/parsing/machine_confirmed.py、tests/core/test_machine_confirmed.py。

独立复现阻断：_parse_qty使用_QTY_RE.search取任意数字，`1.5 UNITS`、`-1 UNIT`、`bad 1 text`均被接受为qty=1，完整parse_confirmed_machine_sources无blocking并产生WeightGroup.qty=1。合成复现：用现有_new_xlsx创建一机MHZ01/S5515001，重量行label=SEM655D,qty=<以上任一字符串>,net=gross=17200；TOTAL.qty=1,net=gross=17200；candidate正常ref/identity和_mail_ref。三种均错误通过。

请严格校验完整数量文本，只允许明确的完整数值或项目已支持的受控单位后缀（UNIT/UNITS等），Decimal须有限、非负且整数（是否零合法以现有组规则验证），不能截断小数、丢负号、从任意文本取数字。未知/歧义输入报告阻断Issue，不崩溃不猜测。数量明细与TOTAL共用同样规则，检查多数字文本/科学计数/NaN/Infinity等边界不会绕过或抛技术异常；净毛重若涉及同样非有限值也应结构化拒绝，避免比较/算术异常。

补参数化parse级回归，不只私有函数镜像测试，验证非法qty不生成可用重量组，合法数值/受控单位保持兼容，源坐标清晰。运行B测试和受影响机器回归，不要在并行开发中跑整个仓库或把其他组未完成失败当最终集成结论。源文件约2000行，优先局部修正，不额外重构。完成用本次新dispatch worker_done汇报，不向已完成原dispatch发信。
