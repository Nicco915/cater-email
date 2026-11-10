# A组第一次审核返工

继续读取plan/tasks/stage3/common.md与deepseek-a.md，只能改src/cater/app/confirmed_input.py和tests/app/test_confirmed_input.py。其他worker仍并行，禁止改其文件。

审核独立复现两项阻断：
1. 实际1433 EML的preview_eml结果mail_row_refs.fields含invoice_no（来自split_preview._PREVIEW_ALIASES），A默认MAIL_FIELD_LABELS与mail_aliases缺此字段，默认header_aliases=None时build拒绝。合成复现用现有测试_make_snapshot(mail_rows=[_mail_row(0,2,invoice_no='SYN-INV-01')])->_build。请模块内提供与预览一致的受控规范别名overlay并随request传入，保留调用方别名、歧义阻断；或合理保留在confirmed_input仅重建core支持业务字段，但不得无痕丢发票身份/绕过冲突。优先局部方案；改变公共契约先ask。
2. parse_mail_text的Source.table_index=None合法，当前_validate_mail_rows要求int导致合法纯文本确认快照被拒。_make_snapshot(mail_rows=[_mail_row(None,2)])复现。区分合法None与非法类型，保留来源坐标和稳定去重，不把None伪造为HTML表0。

新增真正合成preview->确认->build端到端测试：HTML带INVOICE NO列、纯文本业务表，不只手拼快照。检查输出request传入core后发票冲突仍可见。无需全套重复，仅相关测试。完成按本次新dispatch的worker_done命令汇报，不使用已completed的原dispatch。
