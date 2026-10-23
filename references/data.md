# 数据和命令

先运行 `studio.py init --out project.json --style editorial` 获取完整可运行示例。所有资源路径相对项目文件；四个style值为 editorial/zine/swiss/boutique。可用 `style_overrides` 覆盖 ink/paper/accent/primary/pale 六位色值。

assets数组：`id, path, label, source, verified, fit, focus`。path为null表示明确缺图；source记录用户提供/官方链接等；verified表示素材对应关系已确认；可追加ai_generated/ai_edited。不得因路径存在就自动认为真实。

pages类型：cover(title,en,asset,secondary_asset,highlights)，overview(title,en,items)，gallery或meals或services(title,en,items)，hotels(title,en,items)，itinerary(title,en,days)，terms(title,en,rows,notice_zh,notice_en)。

items通用字段：title,en,asset,note。hotels使用exterior/room/room_label。days含day,title,en_title,zh,en,asset,flight,meal_zh,meal_en,hotel_zh,hotel_en；flight是中英共用的确认航班信息，不从旧参考自动补全。rows含label,zh,en。

gallery可设 `paired_featured: true`，前两张等大，下方其余小图。meals可设 `featured: true`，第一张主图，其余并列。仅靠示例字段不能确认实际产品含餐或含演出。

内容校验会拒绝缺失必填字段、失效路径、重复asset id、未知引用、超容量、显著过长正文和可识别的中英金额/重量/航班数字差异。它不做机器翻译，也无法证明每个语义都对应。费用包含、取消条件、酒店房型必须人工核对。

`inspect` 支持PPTX可编辑文本和图片，DOCX段落/表格文字，PDF文字及整页PNG；图片扫描文档不会自动OCR。提取PPT文字包含被覆盖或隐藏的文本，最终必须以渲染可见内容及用户确认核对。

`verify` 检查画布越界、PNG/PDF页数、PowerPoint文字溢出报告与源文件哈希。`compare` 比较两次PNG导出，只对未授权变化页判错。图片源质量、语义与审美均需目视检查。
