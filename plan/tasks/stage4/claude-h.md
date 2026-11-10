# H Claude：目的国流程独立端到端验收与文档（20%）
先读plan/tasks/stage4/common.md。仅改本组tests/app/test_stage4_acceptance.py、docs/multi-ticket-destinations.md、docs/multi-ticket-offline.md。F/G并行开发中，先写黑盒验收与文档骨架，接口缺失ask协调者并结束本轮等待，别发成功worker_done。
用合成EML通过用户Web或CLI：导入→确认建票→每票国家确认→离线转换→重开XLSX核对最终目的国/追溯报告。不得用直接SQL或tasks.update_destination跳过新增用户流程。验证两票不同国家、批量同国仅选中票、未选不变、改国后新目录结果与报告新值而旧产物/快照/旧报告字节不变、未知国/过期/跨批次/部分非法零写入、CSRF、hold/outbox/status/version不变（目的国及明确审计事件是本阶段授权写入，不当“任何DB变化都失败”）。真实样本国家未知，不自动确认，不发信、不读取.env/钥匙串。
F/G交付后协调者审核，再通知你最终全链验收；发现缺陷报告协调者，不改他们实现/测试。全套从/tmp无.env绝对路径运行一次，共享改动不需要反复全套。更新运行手册移除“暂无入口”，写清国家确认只修改所选票、修订并发、缺国阻断及后续不外发边界。
用户最新要求所有修改commit，协调者负责统一提交，你不要自行commit。最终worker_done给真实命令/通过失败/局限、产物校验与文档路径。
