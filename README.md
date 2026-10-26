# Travel Brochure Studio · 旅游宣传册设计工坊

用于制作与局部修改旅游宣传册、双语行程单和旅行海报的 Codex skill。

保留用户选定模板的分栏、图框与标题层级，依据确认后的行程事实制作材料。默认支持可编辑 PPTX 与 PDF；用户允许 HTML→PDF 或继续修改既有 HTML 时，使用独立分格模板和浏览器导出流程。

## 使用入口

**给 Agent：开始工作前先阅读 [AGENTS.md](AGENTS.md)，再阅读 [SKILL.md](SKILL.md)，按任务选择其中的工具和参考文档。** 使用标准文件名 `AGENTS.md`，便于支持该约定的 Agent 发现；通过 Skill 调用时，`SKILL.md` 也会显式指向这份文件。

将仓库放入 Codex 的 `skills/travel-brochure-studio` 目录，入口为 [SKILL.md](SKILL.md)。

示例指令：

> 使用 $travel-brochure-studio，沿用独立分格模板，根据 Word 行程和海报制作 HTML 与 PDF。酒店外观与对应双床房配对，餐食按行程安排，完整保留费用和注意事项，导出后检查页数与排版。

新项目默认先分轮沟通：读资料、问清关键选择、对齐制作约定，给出1—2页实际样张确认后再做整册。每轮以一个重点问题和少量选项为主，已经确认的内容不重复问；明确的小改直接按范围处理。详见 [需求沟通与样张确认](references/discovery.md)。

## 工具与模板

| 文件 | 用途 |
|---|---|
| `scripts/strict_template.py` | 四套原生 PPTX 严格模板的槽位检查与填充 |
| `scripts/studio.py` | 文档检查、PPTX 构建、局部补丁及验证 |
| `scripts/grid_brochure.py` | JSON 驱动的固定纸张独立分格 HTML |
| `scripts/html_brochure.py` | 按页检查、嵌图、局部修改，保留未改页原始字节 |
| `scripts/render_brochure.cjs` | 浏览器导出，检查资源、文字溢出、页数及尺寸 |
| `scripts/pdf_pages.py` | 海报封面、指定页合并、其他页文字与渲染像素比较 |
| `assets/html-grid/` | 通用 HTML 模板、JSON 示例和字段说明 |

详见 [HTML→PDF 工作流](references/html-pdf.md) 与 [严格模板流程](references/strict-template.md)。

## 依赖与运行

Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

HTML 导出还需要 Node.js、Playwright、Chromium/Chrome/Edge；可使用现成运行时，通过 `BROCHURE_PLAYWRIGHT`、`BROCHURE_BROWSER`、`BROCHURE_PYTHON` 指定路径。Office 导出路径需要 Windows 桌面 PowerPoint，HTML 路径不需要。

```powershell
python scripts/grid_brochure.py build assets/html-grid/example.json --out output/example.html --report output/build.json
node scripts/render_brochure.cjs output/example.html --out output/example.pdf --report output/render.json --allow-incomplete
```

示例含明确图片占位，`--allow-incomplete` 仅用于审阅稿，不会放行坏图或溢出。工具不会自动下载图片，不会自动进行语义分页，也不编造航班、价格或酒店安排。最终页面仍需视觉与业务核对。

## 检查

```powershell
python scripts/self_test.py
python scripts/test_strict.py
python -m unittest discover -s scripts -p "test_*brochure.py" -v
python -m unittest discover -s scripts -p "test_pdf_pages.py" -v
```

浏览器测试需要上述渲染环境及可写工作目录。硬链接创建受系统权限限制时，相应测试会明确跳过。

本仓库包含工具、通用模板和示例，不包含客户实际照片或本次旅游产品成品。原模板的来源说明保留在 `assets/reference-shapes/sources.json`；来源记录不代表已获得第三方公开再分发许可。
