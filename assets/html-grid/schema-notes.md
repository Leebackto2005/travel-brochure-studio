# 独立分格 HTML 模板

适合将已确认的正文、景点照片、餐食、酒店和车辆照片组成固定分页行程单。普通页为 A4，保留棕金独立分格框架；颜色可配置。用户明确需要 HTML 或 HTML 转 PDF 时使用，原有 PPTX 流程继续保留。

```powershell
python scripts/grid_brochure.py build assets/html-grid/example.json --out output/brochure.html --report output/build-report.json
```

依赖 Python 3.10+ 和 Pillow。输出为图片内嵌的单文件 UTF-8 HTML，可离线打开；这一步不生成 PDF，不自动分页，也不代表视觉验收。默认不覆盖文件，显式 `--overwrite` 才可替换两个输出。SPEC、HTML、报告必须分别使用不同路径。生成后交给 skill 的 HTML 渲染器导出 PDF 并检查溢出，再人工检查实际 PDF。

## 页面与顺序

根对象：`title`（页脚总标题）、`theme`、`pages`。theme 仅接受 `frame`、`accent`、`paper`、`text` 四个 `#RRGGBB` 色值。pages 顺序就是最终页序，不依据标题或天数重新排列。

构建器会由四个主题色派生 `surface`、`tint`、`line`、`on_frame`、`heading`，使照片底、餐宿条、航班条和表格线随主题联动；浅色框底自动选可读的标题文字色。输入字段保持原样，派生结果在报告的 `palette`。`contrast_checks` 给出五组实际色值的对比度，低于4.5:1时记录阅读提醒；不会擅自改写用户指定的正文色。这是有限的配色提示，不检查透明叠色、照片背景、全部字体或印刷色彩，也不代表可访问性认证。

普通页：`{id,kind:"content",title,subtitle,blocks:[...]}`。id 必须唯一，由英文字母、数字、下划线或连字符组成。标题、字幕和正文均为原生可选中文字；传入 HTML 标签只会显示为文字。固定页面高度 297 mm；超出的原文不会被构建器删除、截短、缩小字号或自动放入下一页，需据渲染检查增加明确的续页。

封面：`{id,kind:"cover",title,image,mode}`，不能再混入正文 blocks。

- `contain`：A4 页、完整等比海报，未被海报占据的区域使用 frame 颜色。
- `poster`：宽 210 mm，高度根据源图片比例计算，四边满版，无裁切、无拉伸；后续页仍 A4。每个封面使用独立的命名 `@page`，因此可包含多个不同长宽比封面。文件没有可读尺寸时放置 A4 占位并记录警告，不假定海报比例。

## 图片与来源

每个 `image`、酒店的 `exterior`/`room` 均可使用本地路径字符串，或以下对象：

```json
{"path":"photos/example.jpg","source_url":"https://example.org/source-page","role":"meal-reference","reference_label":"餐食风格参考，非固定菜单"}
```

路径相对 SPEC 文件所在目录，也可用绝对路径或 PNG/JPEG/WebP/GIF 的 base64 data URI。禁止将网络 URL 当作图片路径；构建器不会下载网络资源。source_url 仅用于记录来源，不证明使用授权。生成报告包含图片实际路径、SHA-256、尺寸、来源字段、使用位置；缺图显示明确占位并记录警告。空路径用于有意留空。损坏或不支持的图片报错，防止悄悄跳过。参考标注显示在对应图片下方。

图片对象可选字符串字段：`creator`、`license_name`、`license_url`、`attribution`、`usage_scope`、`checked_on`。这些字段进入报告，不自动成为图注，也不会自动核验许可；`license_metadata_verified:false` 始终明确这一边界。需要公开署名时由代理另行加入适当图注/署名页。素材库选择及记录方法见 [素材流程](../../references/asset-libraries.md)。

## 内容块

每个块通过 `type` 指定类型：

| type | 字段与行为 |
|---|---|
| `paragraphs` | `paragraphs` 完整原文字符串数组，逐段输出，不删节 |
| `gallery` | 可选 `title`；`columns` 1–4；`image_height_mm` 默认 55；`items` 包含 `image,caption,note,fit,position`。fit 为 cover 或 contain，默认 cover；position 默认 `50% 50%`。车辆等需要整图展示时设 contain |
| `hotels` | `items` 包含 `name,en,exterior,room,note`，每家左外观、右客房。1–3 家默认每张高 53 mm；4 家必须显式 `compact:true`，默认每张高 40 mm。可用 `image_height_mm` 覆盖。支持 `exterior_caption,room_caption`、`exterior_fit,room_fit`、`exterior_position,room_position`；整块可设 note |
| `day` | `day,title,subtitle,paragraphs,flight,meals,hotel`。flight 始终紧随对应日标题；仅显示实际填写的 `route,time,aircraft,number` 字段，缺字段不补写。meals 为 `breakfast,lunch,dinner`，只显示传入的字段 |
| `table` | `headers` 字符串数组，`rows` 为相同列数的字符串数组；保留全部单元格 |
| `notice` | `title,paragraphs`，适合费用、须知、取消条款；原文完整，需人工拆分续页 |

航班示意对象如下，仅表示字段位置，不应把示例中的待确认内容直接当作旅行产品事实：

```json
{"type":"day","day":1,"title":"抵达日","flight":{"route":"出发地 → 目的地","time":"填写已确认时刻"},"paragraphs":["完整行程原文"]}
```

食品与酒店图片区别于已确认的服务承诺。请按行程中的实际餐食类别和费用归属填写 caption/note；图片无法证明对应菜品、餐厅或酒店已经包含。未经确认的数据留空或明确待确认。

## 编辑接口与验收边界

- 页：`section.page[data-page-id="..."]` 与从 1 开始的 `data-page`。正文：`.paper main`；页脚：`footer`。
- 当日：`article.day[data-day="..."]`；航班：其内部的 `.flight-strip`，紧跟 `.dayhead`。
- 相册：`.gallery-block` / `.gallery figure`；图片：`[data-image-location="..."]`；酒店：`.hotels .hotel-row[data-hotel="..."]`。
- 生成报告明确 `automatic_pagination:false`、`overflow_checked:false`、`visual_qa_passed:null`。它证明生成结构和记录素材，不证明内容真实性、授权状态、分页可读性或印刷出血。
- 最低验收：核对页面顺序和尺寸，检查图片主体与注记、长文和表格溢出、航班归属、四酒店清晰度；局部更新还需与基线 PDF 对比未改页面。

示例 JSON 的封面和所有照片均为占位，没有真实产品、价格、航班、地点或用户素材；用于复制后按资料填写。
