# C第一次审核返工：非有限数值必须阻断，真实数据不得硬编码入测试

继续遵守plan/tasks/stage3/common.md及deepseek-c.md。仅允许修改src/cater/core/parsing/kit_confirmed.py、tests/core/test_kit_confirmed.py；报告保存忽略runtime/multi-ticket-stage3/c/。

独立完整解析复现：现有tests/core/test_kit_confirmed.py的build_kit_workbook创建正常合成两发票工作簿，用openpyxl将两页Invoice与Invoice (2)的I12（单价）改字符串NaN/Infinity/1E+1000000，保存后重算candidate.source_refs hash（用现有_candidate/_source），正常_default_regions与_identity。_run_parser：NaN抛未捕获InvalidOperation，Infinity没有blocking，1E+1000000抛未捕获Overflow。_read_number与明细numbers循环直接to_decimal，不判断is_finite，_price_equal相对容差对Infinity变成Infinity<=Infinity误通过。

请在所有金额/数量/单件和总净重/毛重/皮重/费用/合计/辅助列读取入口统一有限与合理量级验证，非法值结构化Issue（精确sheet/cell），不进入算术/量化/相对容差，不只是包大try吞异常。不为了让数据过而改价或截断。覆盖NaN/sNaN/Infinity/负Infinity/极端指数（含候选facts和正文相关字段），合法数值/浮点尾差保持，非法明细不得形成可用记录/ready输出。

另：跟踪候选tests/core/test_kit_confirmed.py中REAL_CASES以及test_real_qm30_*已硬编码真实发票号/MSO/序列号/毛重/金额，违反任务明确“真实数据不可复制进可跟踪fixture/文档/日志”约束。请移除真实商业数据常量和断言，移到被忽略runtime/multi-ticket-stage3/c/的只读回放脚本/报告，或测试仅对显式本地输入动态发现身份并核对通用不变量，不在源码嵌具体票号/重量/金额。常规可跟踪测试用完全合成变体覆盖同样冲突。核对模块源码无真实客户字段硬编码。隐私清理不得删除/修改源文件或.env。

运行C相关和受影响core回归即可，无需并行中重复全仓。完成本次新dispatch worker_done返回命令/结果、非有限值Issue与隐私清理证据。重大业务含义问题ask，不自行选真实发票页/价格口径。
