"""Browser integration tests. Requires the same runtime as render_brochure.cjs."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import fitz

SCRIPT = Path(__file__).with_name('render_brochure.cjs')


class RenderTests(unittest.TestCase):
    def setUp(self):
        # A writable working directory also works under Windows sandbox profiles,
        # where Node cannot atomically rename files in the redirected system temp.
        self.tmp = tempfile.TemporaryDirectory(prefix='brochure-render-test-', dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.html'
        self.pdf = self.root / 'result.pdf'
        self.report = self.root / 'report.json'

    def source_html(self, body, extra_css=''):
        self.source.write_text('''<!doctype html><meta charset="utf-8"><style>
          @page content {size:A4;margin:0} @page poster {size:210mm 420mm;margin:0}
          *{box-sizing:border-box}html,body{margin:0;padding:0}
          .page{position:relative;display:flow-root;width:210mm;height:297mm;page:content;break-after:page}
          .page:last-child{break-after:auto}.poster{height:420mm;page:poster}
          .paper{position:absolute;inset:10mm 10mm 20mm;padding:6mm}
          p{margin:0;font:14px Arial;line-height:1.5}footer{position:absolute;bottom:5mm}
        ''' + extra_css + '</style>' + body, encoding='utf-8')

    def run_render(self, *args):
        return subprocess.run(['node', str(SCRIPT), str(self.source), '--out', str(self.pdf), '--report', str(self.report), *args], capture_output=True, text=True, encoding='utf-8', timeout=90)

    def test_named_paper_sizes_native_text_and_disabled_scripts(self):
        self.source_html('<section class="page poster"><p>封面原比例</p></section><section class="page"><div class="paper"><main><p>完整行程与条款</p><article class="day" data-day="8"><div class="flight-strip">已确认航班</div></article></main></div><footer>2</footer></section><script>document.body.innerHTML="bad"</script>')
        result = self.run_render()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(report['page_count'], 2)
        self.assertFalse(report['scripts_executed'])
        self.assertEqual(report['pages'][1]['flight_days'][0]['day'], '8')
        with fitz.open(self.pdf) as doc:
            self.assertEqual(len(doc), 2)
            self.assertAlmostEqual(doc[0].rect.height * 25.4 / 72, 420, delta=0.5)
            self.assertIn('完整行程与条款', doc[1].get_text())

    def test_placeholder_requires_explicit_draft(self):
        self.source_html('<section class="page"><div class="image-placeholder">照片待补</div></section>')
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pdf.exists())
        result = self.run_render('--allow-incomplete', '--overwrite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(self.report.read_text(encoding='utf-8'))['draft_with_placeholders'])

    def test_overflow_does_not_replace_existing_output(self):
        self.source_html('<section class="page"><div class="paper"><main><p style="height:400mm">完整长文不删减</p></main></div></section>')
        original = b'preserve previous output'
        self.pdf.write_bytes(original)
        result = self.run_render('--overwrite')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pdf.read_bytes(), original)
        self.assertGreater(json.loads(self.report.read_text(encoding='utf-8'))['overflow_count'], 0)

    def test_missing_image_stops_export(self):
        self.source_html('<section class="page"><img src="missing.png" alt="酒店外观"></section>')
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pdf.exists())
        self.assertEqual(json.loads(self.report.read_text(encoding='utf-8'))['missing_image_count'], 1)

    def test_hidden_main_overflow_stops_export(self):
        self.source_html('<section class="page"><div class="paper"><main style="height:10mm;overflow:hidden"><p style="height:30mm">不能裁掉的完整长文</p></main></div></section>')
        self.pdf.write_bytes(b'old')
        result = self.run_render('--overwrite')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pdf.read_bytes(), b'old')
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertTrue(any('clipped or scrolling content' in item['problems'] for item in report['pages'][0]['violations']))

    def test_remote_resource_stops_export(self):
        self.source_html('<section class="page"><img src="https://example.invalid/photo.png"></section>')
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pdf.exists())
        self.assertTrue(json.loads(self.report.read_text(encoding='utf-8'))['remote_requests'])

    def test_wrong_print_size_stops_export(self):
        self.source_html('<section class="page"><p>保持页面尺寸</p></section>', '@page content{size:220mm 300mm}')
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pdf.exists())
        self.assertIn('differs from HTML', result.stderr)

    def test_output_protection(self):
        self.source_html('<section class="page"></section>')
        self.pdf.write_bytes(b'old')
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pdf.read_bytes(), b'old')
        original = self.source.read_bytes()
        result = subprocess.run(['node', str(SCRIPT), str(self.source), '--out', str(self.source), '--report', str(self.report), '--overwrite'], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
