#!/usr/bin/env python3
"""Build an offline, native-text, fixed-page travel brochure from JSON.

This builder does not paginate or approve visual quality. Use the skill's HTML
renderer and inspect exported pages before delivery. Pillow is required to inspect
image dimensions; images are embedded, never downloaded.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from PIL import Image

DEFAULT_THEME = {"frame": "#614020", "accent": "#b48c4b", "paper": "#fffefb", "text": "#46301e"}
SUPPORTED_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))


def blend(base: str, ink: str, amount: float) -> str:
    return '#' + ''.join(f'{round(a * (1 - amount) + b * amount):02x}' for a, b in zip(rgb(base), rgb(ink)))


def contrast(foreground: str, background: str) -> float:
    def luminance(color):
        channels = [v / 255 for v in rgb(color)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))
    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def theme_palette(theme: dict) -> dict:
    """Derive supporting colors without changing the requested four theme inputs."""
    on_frame = theme['paper']
    if contrast(on_frame, theme['frame']) < 4.5:
        on_frame = max((theme['text'], '#000000', '#ffffff'), key=lambda color: contrast(color, theme['frame']))
    heading = theme['frame']
    if contrast(heading, theme['paper']) < 4.5:
        heading = max((theme['text'], '#000000', '#ffffff'), key=lambda color: contrast(color, theme['paper']))
    return {**theme, 'on_frame': on_frame, 'heading': heading,
            'surface': blend(theme['paper'], theme['frame'], 0.045),
            'tint': blend(theme['paper'], theme['accent'], 0.12),
            'line': blend(theme['paper'], theme['frame'], 0.25)}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def number(value: Any, label: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{label} must be a finite number between {lo} and {hi}")
    return float(value)


def paragraphs(value: Any, label: str) -> str:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of complete paragraph strings")
    return "".join(f"<p>{esc(text(p, label))}</p>" for p in value)


def same_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False


def check_output(path: Path, protected: list[Path], overwrite: bool) -> None:
    if any(same_file(path, source) for source in protected):
        raise ValueError(f'output must not overwrite an input, source image, template, or another output: {path}')
    if path.exists():
        if not path.is_file():
            raise ValueError(f'output is not a regular file: {path}')
        if not overwrite:
            raise ValueError(f'output already exists: {path}; use a new path or --overwrite')


def atomic_text(path: Path, content: str, protected: list[Path], overwrite: bool) -> None:
    """Publish one complete output; this is not a multi-file transaction."""
    check_output(path, protected, overwrite)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
        check_output(path, protected, overwrite)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Builder:
    def __init__(self, spec: dict, base_dir: Path):
        if not isinstance(spec, dict):
            raise ValueError("SPEC must be a JSON object")
        self.spec = spec
        self.base_dir = base_dir.resolve()
        self.assets: list[dict] = []
        self.warnings: list[str] = []
        self.pages: list[dict] = []
        self.page_css: list[str] = []

    def image(self, value: Any, location: str) -> tuple[str | None, tuple[int, int] | None, str]:
        """Return embedded URI, dimensions, and the human-facing reference label."""
        data = {"path": value} if isinstance(value, str) else (value or {})
        if not isinstance(data, dict):
            raise ValueError(f"{location}: image must be a local path or an image object")
        raw = data.get("path", "")
        raw = text(raw, f"{location}.path")
        label = text(data.get("reference_label", ""), f"{location}.reference_label")
        record = {"location": location, "source_url": text(data.get("source_url", ""), "source_url"),
                  "role": text(data.get("role", ""), "role"), "reference_label": label}
        # These are user/agent-supplied records, not automatic license verification.
        for field in ('creator', 'license_name', 'license_url', 'attribution', 'usage_scope', 'checked_on'):
            if field in data:
                record[field] = text(data[field], f'{location}.{field}')
        if not raw:
            record.update(status="missing", path="", sha256=None)
            self.assets.append(record)
            self.warnings.append(f"{location}: missing image; explicit placeholder inserted")
            return None, None, label
        if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I) or raw.startswith("//"):
            raise ValueError(f"{location}: remote image URLs are forbidden; download and verify the file separately")
        if raw.startswith("data:"):
            match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)", raw)
            if not match:
                raise ValueError(f"{location}: supported data URI is a base64 PNG/JPEG/WebP/GIF")
            try:
                blob = base64.b64decode(re.sub(r"\s", "", match[2]), validate=True)
            except ValueError as error:
                raise ValueError(f"{location}: invalid base64 image") from error
            record["path"] = "<embedded-data-uri>"
        else:
            path = Path(raw).expanduser()
            path = (self.base_dir / path).resolve() if not path.is_absolute() else path.resolve()
            record["path"] = str(path)
            if not path.is_file():
                record.update(status="missing", sha256=None)
                self.assets.append(record)
                self.warnings.append(f"{location}: image file not found; explicit placeholder inserted: {path}")
                return None, None, label
            blob = path.read_bytes()
        try:
            with Image.open(io.BytesIO(blob)) as verification_image:
                verification_image.verify()
            with Image.open(io.BytesIO(blob)) as image:
                dims = image.size
                mime = SUPPORTED_MIMES.get(image.format)
                if not mime:
                    raise ValueError(f"{location}: unsupported image format {image.format}")
                orientation = image.getexif().get(274, 1)
                if orientation in (5, 6, 7, 8):
                    dims = dims[1], dims[0]
        except (OSError, SyntaxError) as error:
            raise ValueError(f"{location}: invalid image file") from error
        record.update(status="embedded", sha256=hashlib.sha256(blob).hexdigest(), width_px=dims[0], height_px=dims[1], mime=mime)
        self.assets.append(record)
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii"), dims, label

    def figure(self, item: dict, height: float, location: str) -> str:
        if not isinstance(item, dict):
            raise ValueError(f"{location}: gallery item must be an object")
        uri, _, label = self.image(item.get("image"), location)
        fit = item.get("fit", "cover")
        if fit not in ("cover", "contain"):
            raise ValueError(f"{location}: fit must be cover or contain; stretching is not supported")
        position = text(item.get("position", "50% 50%"), "position")
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?%|left|center|right) (?:\d+(?:\.\d+)?%|top|center|bottom)", position):
            raise ValueError(f"{location}: position must contain two CSS percentage/axis-keyword values")
        caption = text(item.get("caption", ""), "caption")
        note = text(item.get("note", ""), "note")
        notes = list(dict.fromkeys(n for n in (note, label) if n))
        image_html = (f'<img src="{uri}" alt="{esc(caption)}" style="height:{height:g}mm;object-fit:{fit};object-position:{esc(position)}">'
                      if uri else f'<div class="image-placeholder" style="height:{height:g}mm">图片待补充</div>')
        caption_html = (f'<figcaption><strong>{esc(caption)}</strong>' + "".join(f"<small>{esc(n)}</small>" for n in notes) + "</figcaption>") if caption or notes else ""
        return f'<figure data-image-location="{esc(location)}">{image_html}{caption_html}</figure>'

    def block(self, block: dict, location: str) -> str:
        if not isinstance(block, dict):
            raise ValueError(f"{location}: block must be an object")
        kind = block.get("type")
        if kind == "paragraphs":
            return '<div class="block paragraphs">' + paragraphs(block.get("paragraphs", []), location) + '</div>'
        if kind == "gallery":
            columns = block.get("columns", 2)
            if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= 4:
                raise ValueError(f"{location}: columns must be an integer from 1 to 4")
            height = number(block.get("image_height_mm", 55), "image_height_mm", 10, 250)
            items = block.get("items", [])
            if not isinstance(items, list):
                raise ValueError(f"{location}: items must be an array")
            figures = "".join(self.figure(item, height, f"{location}.items[{i}]") for i, item in enumerate(items))
            heading = f'<h2>{esc(text(block["title"], "title"))}</h2>' if block.get("title") else ""
            return f'<div class="block gallery-block">{heading}<div class="gallery" style="grid-template-columns:repeat({columns},minmax(0,1fr))">{figures}</div></div>'
        if kind == "hotels":
            items = block.get("items", [])
            if not isinstance(items, list) or not 1 <= len(items) <= 4:
                raise ValueError(f"{location}: hotels requires 1 to 4 items")
            compact = block.get("compact", False)
            if not isinstance(compact, bool) or (len(items) == 4 and compact is not True):
                raise ValueError(f"{location}: four hotels require explicit compact:true")
            height = number(block.get("image_height_mm", 40 if compact else 53), "image_height_mm", 10, 100)
            rows = []
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"{location}: hotel item must be an object")
                name = text(item.get("name", ""), "hotel name")
                en = text(item.get("en", ""), "hotel en")
                pair = "".join(self.figure({"image": item.get(key), "caption": item.get(f"{key}_caption", caption), "fit": item.get(f"{key}_fit", "cover"), "position": item.get(f"{key}_position", "50% 50%")}, height, f"{location}.items[{i}].{key}") for key, caption in (("exterior", "酒店外观"), ("room", "客房")))
                note = text(item.get("note", ""), "hotel note")
                rows.append(f'<section class="hotel-row" data-hotel="{i+1}"><h2><span class="hotel-number">{i+1:02}</span>{esc(name)}<small>{esc(en)}</small></h2><div class="gallery">{pair}</div>' + (f'<p class="reference-note">{esc(note)}</p>' if note else '') + '</section>')
            note = text(block.get("note", ""), "hotels note")
            return '<div class="block hotels' + (' compact' if compact else '') + '">' + ''.join(rows) + (f'<p class="reference-note">{esc(note)}</p>' if note else '') + '</div>'
        if kind == "day":
            day = block.get("day")
            if isinstance(day, bool) or not isinstance(day, (str, int)) or str(day) == "":
                raise ValueError(f"{location}: day is a non-empty string or integer")
            title = text(block.get("title", ""), "day title")
            subtitle = text(block.get("subtitle", ""), "day subtitle")
            body = f'<header class="dayhead"><em>{esc(day)}</em><div><h2>{esc(title)}</h2>' + (f'<small>{esc(subtitle)}</small>' if subtitle else '') + '</div></header>'
            flight = block.get("flight")
            if flight is not None:
                if not isinstance(flight, dict):
                    raise ValueError(f"{location}: flight must be an object")
                parts = [text(flight[key], f"flight.{key}") for key in ("route", "time", "aircraft", "number") if key in flight and flight[key] not in ("", None)]
                if parts:
                    body += '<div class="flight-strip">参考航班：' + esc('｜'.join(parts)) + '</div>'
            body += paragraphs(block.get("paragraphs", []), location + ".paragraphs")
            details = []
            meals = block.get("meals", {})
            if not isinstance(meals, dict):
                raise ValueError(f"{location}: meals must be an object")
            meal_parts = [f'{label}：{text(meals[key], key)}' for key, label in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐")) if key in meals]
            if meal_parts:
                details.append(esc('　'.join(meal_parts)))
            if block.get("hotel"):
                details.append('住宿：' + esc(text(block["hotel"], "hotel")))
            if details:
                body += '<div class="meal-hotel">' + ''.join('<div>' + d + '</div>' for d in details) + '</div>'
            return f'<article class="day" data-day="{esc(day)}">{body}</article>'
        if kind == "table":
            headers, rows = block.get("headers", []), block.get("rows", [])
            if not isinstance(headers, list) or not headers or not isinstance(rows, list):
                raise ValueError(f"{location}: table needs non-empty headers and a rows array")
            cells = lambda row, tag: ''.join(f'<{tag}>{esc(text(cell, "table cell"))}</{tag}>' for cell in row)
            contents = []
            for row in rows:
                if not isinstance(row, list) or len(row) != len(headers):
                    raise ValueError(f"{location}: every row must have {len(headers)} cells")
                contents.append('<tr>' + cells(row, 'td') + '</tr>')
            return '<div class="block"><table class="data-table"><thead><tr>' + cells(headers, 'th') + '</tr></thead><tbody>' + ''.join(contents) + '</tbody></table></div>'
        if kind == "notice":
            title = text(block.get("title", ""), "notice title")
            return '<section class="block notice">' + (f'<h3>{esc(title)}</h3>' if title else '') + paragraphs(block.get("paragraphs", []), location) + '</section>'
        raise ValueError(f"{location}: unsupported block type {kind!r}")

    def build(self) -> tuple[str, dict]:
        title = text(self.spec.get("title", "旅行行程单"), "title")
        theme = dict(DEFAULT_THEME)
        supplied_theme = self.spec.get("theme", {})
        if not isinstance(supplied_theme, dict) or set(supplied_theme) - set(theme):
            raise ValueError("theme accepts only frame, accent, paper, text")
        theme.update(supplied_theme)
        if any(not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v) for v in theme.values()):
            raise ValueError("theme colors must be #RRGGBB")
        palette = theme_palette(theme)
        contrast_checks = []
        for foreground, background in [('text', 'paper'), ('text', 'surface'), ('text', 'tint'), ('heading', 'paper'), ('on_frame', 'frame')]:
            ratio = contrast(palette[foreground], palette[background])
            contrast_checks.append({'foreground': foreground, 'background': background, 'ratio': ratio, 'meets_4_5': ratio >= 4.5})
            if ratio < 4.5:
                self.warnings.append(f'theme: {foreground}/{background} contrast {ratio:.2f}:1 is below the 4.5:1 readability reference; review the actual page before delivery')
        pages = self.spec.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("pages must be a non-empty array")
        result, seen = [], set()
        for index, page in enumerate(pages, 1):
            if not isinstance(page, dict):
                raise ValueError(f"page {index} must be an object")
            page_id = text(page.get("id", f"page-{index}"), "page id")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", page_id) or page_id in seen:
                raise ValueError("page id must be unique and contain only letters, digits, hyphens and underscores")
            seen.add(page_id)
            kind = page.get("kind", "content")
            page_title = text(page.get("title", ""), "page title")
            subtitle = text(page.get("subtitle", ""), "page subtitle")
            width, height = 210.0, 297.0
            attrs = f'data-page="{index}" data-page-id="{esc(page_id)}"'
            if kind == "cover":
                if page.get("blocks"):
                    raise ValueError(f"{page_id}: cover accepts image, not body blocks")
                mode = page.get("mode", "contain")
                if mode not in ("poster", "contain"):
                    raise ValueError("cover mode must be poster or contain")
                uri, dims, _ = self.image(page.get("image"), f"pages[{index-1}].image")
                if mode == "poster" and dims:
                    height = width * dims[1] / dims[0]
                elif mode == "poster":
                    self.warnings.append(f"{page_id}: poster has no image dimensions; placeholder uses A4")
                name = f'poster_{index:03d}' if mode == 'poster' else 'content'
                if mode == 'poster':
                    self.page_css.append(f'@page {name}{{size:{width:.6f}mm {height:.6f}mm;margin:0}}')
                image_html = f'<img src="{uri}" alt="{esc(page_title or title)}">' if uri else '<div class="image-placeholder">封面图片待补充</div>'
                result.append(f'<section class="page cover {mode}" {attrs} style="height:{height:.6f}mm;page:{name}">{image_html}</section>')
            elif kind == "content":
                blocks = page.get("blocks", [])
                if not isinstance(blocks, list):
                    raise ValueError(f"{page_id}: blocks must be an array")
                body = ''.join(self.block(block, f"pages[{index-1}].blocks[{i}]") for i, block in enumerate(blocks))
                heading = f'<header class="section-pill">{esc(page_title)}' + (f'<small>{esc(subtitle)}</small>' if subtitle else '') + '</header>'
                result.append(f'<section class="page grid-page" {attrs}><div class="paper">{heading}<main>{body}</main></div><footer><span>{esc(title)}</span><b>{index:02} / {len(pages):02}</b></footer></section>')
            else:
                raise ValueError(f"{page_id}: kind must be content or cover")
            self.pages.append({"number": index, "id": page_id, "title": page_title, "kind": kind, "width_mm": width, "height_mm": height})
        template = Path(__file__).resolve().parents[1] / 'assets' / 'html-grid' / 'template.html'
        replacements = {'TITLE': esc(title), 'PAGES': ''.join(result), 'PAGE_CSS': '\n'.join(self.page_css), **{k.upper(): v for k, v in palette.items()}}
        # One pass prevents user text that resembles a token being interpreted.
        output = re.sub(r'@@([A-Z_]+)@@', lambda match: replacements[match[1]], template.read_text(encoding='utf-8'))
        report = {"schema_version": 1, "page_count": len(pages), "pages": self.pages, "assets": self.assets,
                  "palette": palette, "contrast_checks": contrast_checks, "license_metadata_verified": False,
                  "warnings": self.warnings, "missing_image_count": sum(a['status'] == 'missing' for a in self.assets),
                  "images_embedded": True, "network_requests": 0, "automatic_pagination": False,
                  "visual_qa_passed": None, "overflow_checked": False,
                  "next_step": "Render and inspect every changed page; split overflowing content into explicitly ordered continuation pages. This report is not visual acceptance."}
        return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build', help='build self-contained HTML without network access')
    build.add_argument('spec', type=Path)
    build.add_argument('--out', type=Path, required=True)
    build.add_argument('--report', type=Path, required=True)
    build.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        paths = [args.spec.resolve(), args.out.resolve(), args.report.resolve()]
        template = Path(__file__).resolve().parents[1] / 'assets' / 'html-grid' / 'template.html'
        protected = [paths[0], template]
        check_output(paths[1], protected + [paths[2]], args.overwrite)
        check_output(paths[2], protected + [paths[1]], args.overwrite)
        spec = json.loads(args.spec.read_text(encoding='utf-8-sig'))
        output, report = Builder(spec, args.spec.parent).build()
        protected += [Path(asset['path']) for asset in report['assets'] if asset.get('path') and asset['path'] != '<embedded-data-uri>']
        check_output(paths[1], protected + [paths[2]], args.overwrite)
        check_output(paths[2], protected + [paths[1]], args.overwrite)
        report.update(spec_path=str(paths[0]), output_path=str(paths[1]))
        atomic_text(paths[1], output, protected + [paths[2]], args.overwrite)
        atomic_text(paths[2], json.dumps(report, ensure_ascii=False, indent=2) + '\n', protected + [paths[1]], args.overwrite)
        print(json.dumps({'html': str(paths[1]), 'report': str(paths[2]), 'pages': report['page_count'], 'missing_images': report['missing_image_count']}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
