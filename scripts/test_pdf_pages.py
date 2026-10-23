"""Behavior tests for pdf_pages.py; run: python -m unittest -v test_pdf_pages.py."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
from PIL import Image, ImageDraw

import pdf_pages as tool


def make_pdf(path, labels, width=200, height=300):
    with fitz.open() as doc:
        for i, label in enumerate(labels):
            page = doc.new_page(width=width, height=height)
            page.draw_rect(fitz.Rect(10, 30, 70, 95), fill=(i / 10, 0.3, 0.7))
            page.insert_text((15, 20), label)
        doc.save(path)


class PdfPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.base = self.root / "base.pdf"
        self.candidate = self.root / "candidate.pdf"
        self.output = self.root / "output.pdf"
        self.image = self.root / "poster.png"
        self.report = self.root / "report.json"
        make_pdf(self.base, ["Cover", "Hotel old", "Day 1", "Day 8 old"])
        make_pdf(self.candidate, ["Cover NEW", "Hotel NEW", "Day 1 NEW", "Day 8 NEW"])
        image = Image.new("RGB", (120, 240), "red")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 119, 5), fill="green")
        draw.rectangle((0, 234, 119, 239), fill="blue")
        draw.rectangle((0, 0, 5, 239), fill="yellow")
        draw.rectangle((114, 0, 119, 239), fill="purple")
        image.save(self.image)
        self.original = self.base.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_poster_is_full_bleed_original_ratio_and_pixels(self):
        report = tool.cover(self.base, self.image, self.output, report_path=self.report)
        self.assertTrue(report["passed"])
        with fitz.open(self.output) as doc:
            self.assertEqual(len(doc), 4)
            self.assertEqual((doc[0].rect.width, doc[0].rect.height), (200, 400))
            xref = doc[0].get_images()[0][0]
            self.assertEqual(doc[0].get_image_rects(xref), [doc[0].rect])
            pix = fitz.Pixmap(doc, xref)
            with Image.open(self.image) as image:
                self.assertEqual(pix.samples, image.convert("RGB").tobytes())
        self.assertEqual(self.base.read_bytes(), self.original)
        self.assertTrue(report["cover_checks"]["full_bleed"])
        self.assertTrue(report["all_unchanged_pages_match"])
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8"))["sources"][0]["sha256"], tool.sha256(self.original))

    def test_contain_preserves_page_size_centers_and_colors_border(self):
        report = tool.cover(self.base, self.image, self.output, mode="contain", background="#614020")
        with fitz.open(self.output) as doc:
            self.assertEqual(tuple(doc[0].rect), (0, 0, 200, 300))
            rect = doc[0].get_image_rects(doc[0].get_images()[0][0])[0]
            self.assertEqual(tuple(rect), (25, 0, 175, 300))
            pix = doc[0].get_pixmap(colorspace=fitz.csRGB, alpha=False)
            rgb = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            self.assertEqual(rgb.getpixel((2, 150)), (97, 64, 32))
            self.assertEqual(rgb.getpixel((198, 150)), (97, 64, 32))
        self.assertTrue(report["passed"])

    def test_poster_single_page_input_supported(self):
        one = self.root / "single.pdf"
        make_pdf(one, ["One"])
        result = tool.cover(one, self.image, self.output)
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["after_pages"]), 1)

    def test_merge_only_changes_selected_pages(self):
        candidate_bytes = self.candidate.read_bytes()
        result = tool.merge(self.base, self.candidate, "2,4", self.output)
        self.assertTrue(result["passed"])
        self.assertEqual(result["actual_changed_pages"], [2, 4])
        with fitz.open(self.output) as doc:
            self.assertEqual([page.get_text().strip() for page in doc], ["Cover", "Hotel NEW", "Day 1", "Day 8 NEW"])
        self.assertTrue(all(x["unchanged_checks_passed"] for x in result["replacement_checks"]))
        self.assertEqual(self.base.read_bytes(), self.original)
        self.assertEqual(self.candidate.read_bytes(), candidate_bytes)

    def test_invalid_page_numbers_do_not_publish(self):
        for value in ("0", "5", "-1", "2,2", "2,", "a", ""):
            with self.subTest(value=value), self.assertRaises(tool.PdfToolError):
                tool.merge(self.base, self.candidate, value, self.output)
            self.assertFalse(self.output.exists())

    def test_mismatched_page_count_rejected(self):
        short = self.root / "short.pdf"
        make_pdf(short, ["only"])
        with self.assertRaisesRegex(tool.PdfToolError, "Page counts"):
            tool.merge(self.base, short, "1", self.output)
        self.assertFalse(self.output.exists())

    def test_same_input_output_refused_even_with_overwrite(self):
        for overwrite in (False, True):
            with self.assertRaises(tool.PdfToolError):
                tool.cover(self.base, self.image, self.base, overwrite=overwrite)
            with self.assertRaises(tool.PdfToolError):
                tool.merge(self.base, self.candidate, "2", self.candidate, overwrite=overwrite)
            with self.assertRaises(tool.PdfToolError):
                tool.verify(self.base, self.candidate, "2", self.base, overwrite=overwrite)
        self.assertEqual(self.base.read_bytes(), self.original)

    def test_existing_output_requires_explicit_overwrite(self):
        self.output.write_bytes(b"keep existing")
        with self.assertRaises(tool.PdfToolError):
            tool.cover(self.base, self.image, self.output)
        self.assertEqual(self.output.read_bytes(), b"keep existing")
        self.assertTrue(tool.cover(self.base, self.image, self.output, overwrite=True)["passed"])

    def test_failed_validation_never_publishes_final_or_temp(self):
        original_compare = tool.compare_records
        def fail(*args, **kwargs):
            result = original_compare(*args, **kwargs)
            result["passed"] = False
            return result
        with patch.object(tool, "compare_records", side_effect=fail):
            with self.assertRaisesRegex(tool.PdfToolError, "validation failed"):
                tool.cover(self.base, self.image, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob("*.candidate.pdf")))
        self.assertEqual(self.base.read_bytes(), self.original)

    def test_failed_validation_preserves_existing_final(self):
        self.output.write_bytes(b"old delivered result")
        original_compare = tool.compare_records
        def fail(*args, **kwargs):
            result = original_compare(*args, **kwargs)
            result["passed"] = False
            return result
        with patch.object(tool, "compare_records", side_effect=fail):
            with self.assertRaises(tool.PdfToolError):
                tool.merge(self.base, self.candidate, "2", self.output, overwrite=True)
        self.assertEqual(self.output.read_bytes(), b"old delivered result")

    def test_verify_detects_unexpected_changes_and_reports_native_content(self):
        result = tool.verify(self.base, self.candidate, "2,4", self.report)
        self.assertFalse(result["passed"])
        self.assertEqual(result["actual_changed_pages"], [1, 2, 3, 4])
        self.assertGreater(result["after_pages"][0]["native_text_characters"], 0)
        self.assertIn("image_resource_count", result["after_pages"][0])
        self.assertEqual(result["human_visual_acceptance"], "not performed by this tool")

    def test_verify_previews_and_matching_remaining_pages(self):
        tool.merge(self.base, self.candidate, "2,4", self.output)
        result = tool.verify(self.base, self.output, "2,4", self.report, self.root / "preview")
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["preview_files"]), 5)
        self.assertTrue(all(Path(p).exists() for p in result["preview_files"]))
        with self.assertRaises(tool.PdfToolError):
            tool.verify(self.base, self.output, "2,4", self.root / "another.json", self.root / "preview")

    def test_interactive_form_is_refused(self):
        form = self.root / "form.pdf"
        with fitz.open(self.base) as doc:
            widget = fitz.Widget()
            widget.field_name = "traveler"
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.rect = fitz.Rect(20, 100, 160, 125)
            doc[0].add_widget(widget)
            doc.save(form)
        with self.assertRaisesRegex(tool.PdfToolError, "Interactive form"):
            tool.merge(form, self.candidate, "2", self.output)
        self.assertFalse(self.output.exists())

    def test_encrypted_pdf_is_refused(self):
        encrypted = self.root / "encrypted.pdf"
        with fitz.open(self.base) as doc:
            doc.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
        with self.assertRaisesRegex(tool.PdfToolError, "Encrypted"):
            tool.merge(encrypted, self.candidate, "2", self.output)
        self.assertFalse(self.output.exists())

    def test_owner_only_encryption_is_also_refused(self):
        encrypted = self.root / "owner-only.pdf"
        with fitz.open(self.base) as doc:
            doc.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="")
        with self.assertRaisesRegex(tool.PdfToolError, "Encrypted"):
            tool.merge(encrypted, self.candidate, "2", self.output)
        self.assertFalse(self.output.exists())

    def test_signature_flag_is_refused(self):
        signed = self.root / "signature-flag.pdf"
        with fitz.open(self.base) as doc:
            doc.xref_set_key(doc.pdf_catalog(), "AcroForm", "<</SigFlags 1>>")
            doc.save(signed)
        with self.assertRaisesRegex(tool.PdfToolError, "Signed PDF"):
            tool.merge(signed, self.candidate, "2", self.output)
        self.assertFalse(self.output.exists())

    def test_report_cannot_overwrite_output_or_inputs(self):
        with self.assertRaises(tool.PdfToolError):
            tool.cover(self.base, self.image, self.output, report_path=self.output, overwrite=True)
        self.assertFalse(self.output.exists())
        with self.assertRaises(tool.PdfToolError):
            tool.cover(self.base, self.image, self.output, report_path=self.image, overwrite=True)


if __name__ == "__main__":
    unittest.main()
