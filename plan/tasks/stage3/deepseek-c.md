# C：DeepSeek Harness，已确认配件Health Care Kit解析（预计25%）
先读common.md。只允许编辑src/cater/core/parsing/kit_confirmed.py、tests/core/test_kit_confirmed.py。其他文件只读，不改api.py/parts.py/transform。
按公共契约返回PartsParseResult，正式读取配件明细，不拿preview总数填明细。新样本Invoice / Invoice (2)、Packing list、BL，无CI-For Dealer；可复用parts模型和底层语义加载。区分料号/kit号、每套数量/套数/总数量、单价/总价、单件/总净重、原产国、HS/中文品名/申报要素，不能把KIT号作为所有行料号。两发票表应以已确认区域选择，证据不足或两表不一致报告冲突，不按(2)或顺序自动定稿；不能自动选择某个差价口径。箱单首段H件数/G套数/K毛重及后段零件净重区按语义定位，皮重仅作为核对事实（已含不得再加），多TOTAL正确区分。
旧BL残留号、辅助金额列不同、行价×量与金额/净重不符、缺中文字段/HS时必须如实阻断，不为了输出绕过；不会替用户改价/选HS/推断目的国。合成清洁数据应完整可转换（作为后续集成测试），真实四票应保留现有冲突。测试覆盖上述结构、重复料号和sheet版本、精度、费用行排除、上下两段TOTAL、错票来源。worker_done给标准records示例/字段映射/测试证据；重大新业务含义问题先ask并停依赖工作。
