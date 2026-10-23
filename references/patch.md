# 局部修改：只改授权文字

先inspect获取页码和shape_id，再创建补丁JSON：

```json
{"changes":[{"slide":7,"shape_id":32,"old":"旧句子","new":"新句子"}]}
```

运行 `python scripts/studio.py patch source.pptx --changes edits.json --out revised.pptx`。

该命令只修改指定slide XML中的指定shape，支持跨run匹配，保留未涉及run属性；新字继承匹配起点样式。重复匹配、空old、未找到目标、同名输出覆盖源文件都会报错。仅支持同一段落替换，不接受换行；需改段落结构时另用原生编辑后渲染。

其余ZIP成员的解压后内容逐字节不变（压缩包本身hash会变）。文字变长可能超框，必须重新渲染。图片更新用原生PowerPoint或重建明确授权页；此文字补丁工具不谎称能做图片编辑。

`python scripts/studio.py compare before after --allowed 7 --out diff.json` 检查未授权页面PNG变化。before/after应由同一个渲染器、尺寸和字体环境导出。
