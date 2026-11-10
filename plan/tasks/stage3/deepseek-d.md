# D：DeepSeek Harness，独立端到端验收与运行文档（预计5%）
先读common.md。依赖A/B/C/E均完成并经协调者初审后才启动。只允许编辑tests/app/test_stage3_acceptance.py、docs/multi-ticket-offline.md、runtime/multi-ticket-stage3/d/内忽略报告。不得改实现、其他worker测试或用户既有文档。
独立使用合成EML走导入→确认→held任务→显式离线执行→模板文件重开，验证各票独立字段/数量/单位/净毛重/总金额/HS及来源，坏输入/缺参考必须阻断，正式hold/tasks/outbox不变。实际三封只在隔离runtime只读回放，不替业务确认目的国/金额/HS，不生成伪造ready。发现问题发给协调者，不直接改实现。
写清CLI命令、所需本地参考和模板、限制/失败恢复、输出位置和无外发边界；最终全套测试（/tmp无.env，绝对路径），必要保存截图/文件校验报告，真实材料不进跟踪fixture。worker_done给出通过/失败/跳过、未解决问题和文档位置。
