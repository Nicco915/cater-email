# B：DeepSeek Harness，已确认整机格式解析（预计25%）
先读common.md。只允许编辑src/cater/core/parsing/machine_confirmed.py、tests/core/test_machine_confirmed.py。其他文件只读，不改api.py或machine_wb.py。
按公共契约返回MachineWorkbookResult，给现有reconcile_machines/transform复用。重点支持本次8份整机Invoice+Packing list单票快照和旧BIFF CI/PL无正文重量。按已确认candidate/source_refs区域/机器身份读取，不把整个批次当本票；唯一序列号与mail_rows补认缺MSO时保留原来源且冲突拒绝。重量来自对应箱单，数量/净毛重/TOTAL核对；没有组内MSO时仅在来源机器清单能唯一映射时建WeightGroup，不平均猜配；多机组合不唯一必须报告。PIN/engine/serial逐机核对，尊重真实地区/收票地址与最终目的国区别，不推断国家。不得把invoice合计当逐机单价；价格仍来自本票确认正文行，由原reconcile处理。
合成测试：新式KG表头、CI/PL旧xls、单机/多机、同票分文件、无MSO唯一序列兜底、同MSO/serial交叉冲突、多个重量区/合计冲突、数量和重量不匹配、不泄露其他票。可以在隔离runtime/multi-ticket-stage3/b/只读研究真实单票快照，真实8票不默认确认或生成任务。若真实格式不能在证据上可靠处理，报告等待人工而非伪造成功。worker_done包含解析结果及接线建议。
