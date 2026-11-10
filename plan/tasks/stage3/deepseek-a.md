# A：DeepSeek Harness，快照到核心请求适配（预计25%）
先读common.md与自身AGENTS。只允许编辑：src/cater/app/confirmed_input.py、tests/app/test_confirmed_input.py。其他文件只读。
按公共契约实现build_confirmed_request，复用现有ValidationError，校验schema/候选/资产/正文结构、文件哈希/路径边界（拒绝symlink越界），深拷贝防副作用。不得读取父EML或全批次材料，不能把排除/别票的正文补回。规范HTML字段名需映射现有mail别名（mso -> MSO, serial_number -> Serial Number等），HTML转义且保留金额/机器字段精度，不做价格补算。输出references/destination/task/kind/output_dir与当前process_job契约精确兼容，明确上游trusted_hash验证前置。
测试：两票输入不串、缺字段/哈希被改/越界/symlink/重复资产/未知schema拒绝、HTML恶意字段被转义、正文来源保留、输入dict不被修改、同票多行完整保留。不得以读取全父邮件实现补认。发现契约缺失ask。交付测试通过及给Claude的调用示例（纯合成），worker_done。
