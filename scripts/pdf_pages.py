#!/usr/bin/env python3
"""Safe, local PDF cover replacement, selective merge, and regression checks.

Dependencies: PyMuPDF, Pillow. Page numbers are 1-based. No image rewriting is
performed. Reports describe machine checks, not human visual acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

import fitz
from PIL import Image, ImageOps, ImageDraw


class PdfToolError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()), "bytes": path.stat().st_size}


def same_path(first, second):
    a, b = Path(first).resolve(), Path(second).resolve()
    if os.path.normcase(str(a)) == os.path.normcase(str(b)):
        return True
    return a.exists() and b.exists() and os.path.samefile(a, b)


def check_output(path, inputs, overwrite=False):
    path = Path(path).resolve()
    if any(same_path(path, item) for item in inputs):
        raise PdfToolError(f"Output must never overwrite an input: {path}")
    if path.exists() and not overwrite:
        raise PdfToolError(f"Output already exists; choose a new path or use --overwrite: {path}")
    if path.exists() and not path.is_file():
        raise PdfToolError(f"Output is not a regular file: {path}")
    return path


def publish_temp(temp_name, output, overwrite):
    if overwrite:
        os.replace(temp_name, output)
    elif os.name == "nt":
        # Unlike POSIX rename, Windows rename refuses an existing destination.
        os.rename(temp_name, output)
    else:
        # Same-directory hard link is an atomic create-if-absent operation.
        os.link(temp_name, output)
        os.unlink(temp_name)


def atomic_bytes(path, data, inputs=(), overwrite=False):
    path = check_output(path, inputs, overwrite)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # A second preflight prevents accidental ordinary overwrite during work.
        check_output(path, inputs, overwrite)
        publish_temp(name, path, overwrite)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def parse_pages(value, page_count):
    if not value or not re.fullmatch(r"\s*\d+\s*(,\s*\d+\s*)*", value):
        raise PdfToolError("Page list must be comma-separated positive integers, e.g. 4,6,10")
    pages = [int(item) for item in value.split(",")]
    if len(set(pages)) != len(pages):
        raise PdfToolError("Page list contains duplicates")
    if any(page < 1 or page > page_count for page in pages):
        raise PdfToolError(f"Page numbers must be between 1 and {page_count}")
    return sorted(pages)


def open_pdf(path, mutation=False):
    doc = fitz.open(path)
    try:
        if not doc.is_pdf:
            raise PdfToolError(f"Expected a PDF: {path}")
        if doc.needs_pass or doc.is_encrypted or doc.xref_get_key(-1, "Encrypt")[0] != "null":
            raise PdfToolError(f"Encrypted PDF is unsupported; supply an unencrypted copy: {path}")
        if not len(doc):
            raise PdfToolError(f"PDF has no pages: {path}")
        if mutation:
            if doc.get_sigflags() > 0:
                raise PdfToolError(f"Signed PDF cannot be modified by this tool: {path}")
            if doc.is_form_pdf or any(next(page.widgets() or iter(()), None) for page in doc):
                raise PdfToolError(f"Interactive form PDF cannot be modified by this tool: {path}")
        return doc
    except Exception:
        doc.close()
        raise


def rounded_rect(rect):
    return [round(float(value), 6) for value in rect]


def page_record(page, dpi=72):
    text = page.get_text("text")
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB, alpha=False, annots=True)
    return {
        "page": page.number + 1,
        "size_pt": [round(page.rect.width, 6), round(page.rect.height, 6)],
        "media_box": rounded_rect(page.mediabox), "crop_box": rounded_rect(page.cropbox),
        "rotation": page.rotation,
        "native_text_characters": len(text), "native_text_sha256": sha256(text.encode("utf-8")),
        "image_resource_count": len(page.get_images(full=True)),
        "render_rgb_dimensions": [pix.width, pix.height],
        "render_rgb_sha256": sha256(pix.samples),
    }


def document_records(doc, dpi=72):
    return [page_record(page, dpi) for page in doc]


def compare_records(before, after, changed_pages):
    comparisons = []
    for i, (a, b) in enumerate(zip(before, after), 1):
        checks = {
            "page_size_equal": all(a[key] == b[key] for key in ("size_pt", "media_box", "crop_box", "rotation")),
            "native_text_equal": a["native_text_sha256"] == b["native_text_sha256"],
            "render_rgb_pixels_equal": all(a[key] == b[key] for key in ("render_rgb_dimensions", "render_rgb_sha256")),
        }
        comparisons.append({"page": i, "allowed_to_change": i in changed_pages, **checks,
                            "unchanged_checks_passed": all(checks.values())})
    same_count = len(before) == len(after)
    passed = same_count and all(row["unchanged_checks_passed"] for row in comparisons if not row["allowed_to_change"])
    return {"same_page_count": same_count, "all_unchanged_pages_match": passed,
            "actual_changed_pages": [row["page"] for row in comparisons if not row["unchanged_checks_passed"]],
            "page_comparisons": comparisons, "passed": passed}


def report_base(operation, sources, dpi=72):
    return {"schema_version": 1, "operation": operation, "sources": [file_record(p) for p in sources],
            "renderer": {"name": "PyMuPDF", "version": fitz.VersionBind, "dpi": dpi,
                         "colorspace": "RGB", "alpha": False, "annotations": True},
            "human_visual_acceptance": "not performed by this tool",
            "verification_limits": "Pixel checks use the reported DPI and renderer; link destinations, accessibility tags, color-managed print proofs and bookmark view settings are not verified."}


def rgb_image(path, background):
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        canvas = Image.new("RGBA", image.size, tuple(round(v * 255) for v in background) + (255,))
        canvas.alpha_composite(image)
        image = canvas.convert("RGB")
    data = io.BytesIO()
    image.save(data, format="PNG")
    return image, data.getvalue()


def color(value):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise PdfToolError("Background must be #RRGGBB, e.g. #614020")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def save_validated(doc, output, inputs, overwrite, validate):
    """Save to a sibling temporary path, reopen and verify, then publish atomically."""
    output = check_output(output, inputs, overwrite)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".candidate.pdf", dir=output.parent)
    os.close(fd)
    try:
        doc.save(temp_name, garbage=4, deflate=True)
        with open_pdf(temp_name) as candidate:
            result = validate(candidate)
        if not result["passed"]:
            raise PdfToolError("Candidate validation failed; final PDF was not published: " + json.dumps(result.get("page_comparisons", [])))
        check_output(output, inputs, overwrite)
        publish_temp(temp_name, output, overwrite)
        return result
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def optional_report_preflight(report, output, inputs, overwrite):
    if report:
        check_output(report, [*inputs, output], overwrite)


def publish_report(report, path, inputs, overwrite):
    if path:
        atomic_bytes(path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), inputs, overwrite)


def cover(input_pdf, image_path, output, mode="poster", background="#614020", overwrite=False, report_path=None):
    inputs = [input_pdf, image_path]
    check_output(output, inputs, overwrite)
    optional_report_preflight(report_path, output, inputs, overwrite)
    bg = color(background)
    source_image, image_bytes = rgb_image(image_path, bg)
    report = report_base("cover", inputs)
    with open_pdf(input_pdf, mutation=True) as doc:
        before = document_records(doc)
        if mode not in ("poster", "contain"):
            raise PdfToolError("Cover mode must be poster or contain")
        width = doc[1 if len(doc) > 1 else 0].rect.width if mode == "poster" else doc[0].rect.width
        height = width * source_image.height / source_image.width if mode == "poster" else doc[0].rect.height
        scale = min(width / source_image.width, height / source_image.height)
        w, h = source_image.width * scale, source_image.height * scale
        target = fitz.Rect((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2)
        toc = doc.get_toc()
        doc.delete_page(0)
        page = doc.new_page(pno=0, width=width, height=height)
        page.draw_rect(page.rect, color=None, fill=bg, overlay=False)
        page.insert_image(target, stream=image_bytes, keep_proportion=True)
        if toc:
            doc.set_toc(toc)

        def validate(candidate):
            after = document_records(candidate)
            result = compare_records(before, after, {1})
            first = candidate[0]
            images = first.get_images(full=True)
            rects = first.get_image_rects(images[0][0]) if len(images) == 1 else []
            actual = rects[0] if len(rects) == 1 else fitz.Rect()
            image_pix = fitz.Pixmap(candidate, images[0][0]) if len(images) == 1 else None
            if image_pix and image_pix.colorspace.n != 3:
                image_pix = fitz.Pixmap(fitz.csRGB, image_pix)
            cover_checks = {
                "page_dimensions_match": abs(first.rect.width - width) < 0.001 and abs(first.rect.height - height) < 0.001,
                "image_placement_matches": len(rects) == 1 and max(abs(a - b) for a, b in zip(actual, target)) < 0.001,
                "image_aspect_ratio_preserved": actual.width > 0 and abs(actual.height / actual.width - source_image.height / source_image.width) < 0.00001,
                "complete_rgb_source_pixels_preserved": bool(image_pix and image_pix.width == source_image.width and image_pix.height == source_image.height and image_pix.samples == source_image.tobytes()),
                "image_within_page": first.rect.contains(actual),
            }
            if mode == "poster":
                cover_checks["full_bleed"] = max(abs(a - b) for a, b in zip(actual, first.rect)) < 0.001
            result.update({"before_pages": before, "after_pages": after, "cover_checks": cover_checks})
            result["passed"] = result["passed"] and all(cover_checks.values())
            return result

        result = save_validated(doc, output, inputs, overwrite, validate)
    report.update(result)
    report.update({"mode": mode, "background": background, "image_px": list(source_image.size),
                   "image_normalization": "EXIF orientation applied; RGB converted; transparency composited on background",
                   "output": file_record(output)})
    publish_report(report, report_path, [*inputs, output], overwrite)
    return report


def merge(base_path, candidate_path, pages, output, overwrite=False, report_path=None):
    inputs = [base_path, candidate_path]
    check_output(output, inputs, overwrite)
    optional_report_preflight(report_path, output, inputs, overwrite)
    report = report_base("merge", inputs)
    with open_pdf(base_path, mutation=True) as base, open_pdf(candidate_path, mutation=True) as candidate:
        if len(base) != len(candidate):
            raise PdfToolError(f"Page counts must match: base={len(base)}, candidate={len(candidate)}")
        selected = parse_pages(pages, len(base))
        before = document_records(base)
        expected = {p: page_record(candidate[p - 1]) for p in selected}
        toc = base.get_toc()
        for p in selected:
            base.delete_page(p - 1)
            base.insert_pdf(candidate, from_page=p - 1, to_page=p - 1, start_at=p - 1, links=True, annots=True)
        if toc:
            base.set_toc(toc)

        def validate(final):
            after = document_records(final)
            result = compare_records(before, after, set(selected))
            replacement_checks = []
            for p in selected:
                item = compare_records([expected[p]], [after[p - 1]], set())["page_comparisons"][0]
                item["page"] = p
                replacement_checks.append(item)
            result.update({"before_pages": before, "after_pages": after, "replacement_checks": replacement_checks})
            result["passed"] = result["passed"] and all(row["unchanged_checks_passed"] for row in replacement_checks)
            return result

        report.update(save_validated(base, output, inputs, overwrite, validate))
    report.update({"selected_pages": selected, "output": file_record(output)})
    publish_report(report, report_path, [*inputs, output], overwrite)
    return report


def preview_paths(directory, count):
    return [Path(directory) / f"page_{i:03d}.png" for i in range(1, count + 1)] + [Path(directory) / "contact.jpg"]


def export_previews(doc, directory, inputs, overwrite):
    paths = preview_paths(directory, len(doc))
    thumbs = []
    for page, path in zip(doc, paths):
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB, alpha=False, annots=True)
        atomic_bytes(path, pix.tobytes("png"), inputs, overwrite)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        image.thumbnail((280, 390))
        tile = Image.new("RGB", (300, 420), "#eeeeee")
        tile.paste(image, ((300 - image.width) // 2, 10))
        ImageDraw.Draw(tile).text((12, 402), f"Page {page.number + 1}", fill="black")
        thumbs.append(tile)
    columns = min(4, len(thumbs))
    sheet = Image.new("RGB", (300 * columns, 420 * math.ceil(len(thumbs) / columns)), "white")
    for i, tile in enumerate(thumbs):
        sheet.paste(tile, ((i % columns) * 300, (i // columns) * 420))
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=90)
    atomic_bytes(paths[-1], buffer.getvalue(), inputs, overwrite)
    return [str(path.resolve()) for path in paths]


def verify(before_path, after_path, changed_pages, report_path, preview_dir=None, overwrite=False, dpi=72):
    if not math.isfinite(dpi) or dpi <= 0:
        raise PdfToolError("DPI must be a finite positive number")
    inputs = [before_path, after_path]
    check_output(report_path, inputs, overwrite)
    report = report_base("verify", inputs, dpi)
    with open_pdf(before_path) as before, open_pdf(after_path) as after:
        allowed = parse_pages(changed_pages, len(before)) if changed_pages else []
        if preview_dir:
            for path in preview_paths(preview_dir, len(after)):
                check_output(path, [*inputs, report_path], overwrite)
        before_records, after_records = document_records(before, dpi), document_records(after, dpi)
        report.update(compare_records(before_records, after_records, set(allowed)))
        report.update({"allowed_changed_pages": allowed, "before_pages": before_records, "after_pages": after_records})
        if preview_dir:
            report["preview_files"] = export_previews(after, preview_dir, [*inputs, report_path], overwrite)
    publish_report(report, report_path, inputs, overwrite)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    c = commands.add_parser("cover", help="Replace page 1 using a complete image")
    c.add_argument("input"); c.add_argument("image"); c.add_argument("--out", required=True)
    c.add_argument("--mode", choices=("poster", "contain"), default="poster")
    c.add_argument("--background", default="#614020"); c.add_argument("--report")
    m = commands.add_parser("merge", help="Replace selected 1-based pages only")
    m.add_argument("base"); m.add_argument("candidate"); m.add_argument("--pages", required=True)
    m.add_argument("--out", required=True); m.add_argument("--report")
    v = commands.add_parser("verify", help="Check unchanged pages and produce a JSON report")
    v.add_argument("before"); v.add_argument("after"); v.add_argument("--changed-pages", default="")
    v.add_argument("--report", required=True); v.add_argument("--preview-dir")
    v.add_argument("--dpi", type=float, default=72)
    for child in (c, m, v):
        child.add_argument("--overwrite", action="store_true", help="Allow existing outputs, never input paths")
    args = parser.parse_args(argv)
    try:
        if args.command == "cover":
            report = cover(args.input, args.image, args.out, args.mode, args.background, args.overwrite, args.report)
        elif args.command == "merge":
            report = merge(args.base, args.candidate, args.pages, args.out, args.overwrite, args.report)
        else:
            report = verify(args.before, args.after, args.changed_pages, args.report, args.preview_dir, args.overwrite, args.dpi)
        print(json.dumps({"passed": report["passed"], "operation": report["operation"],
                          "pages": len(report["after_pages"]), "report": args.report,
                          "output": report.get("output", {}).get("path")}, ensure_ascii=False))
        return 0 if report["passed"] else 1
    except (PdfToolError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
