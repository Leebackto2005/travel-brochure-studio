"""Focused regression checks for the generic JSON-to-HTML brochure builder."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
from html import unescape
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from grid_brochure import Builder, main, contrast


class GridBrochureTests(unittest.TestCase):
    def setUp(self):
        # Workspace-local temp avoids Windows sandbox system-temp rename limits.
        self.temp = tempfile.TemporaryDirectory(prefix='.grid-test-', dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        Image.new('RGB', (120, 240), '#967644').save(self.base / 'portrait.png')
        Image.new('RGB', (240, 120), '#c4b18a').save(self.base / 'wide.png')

    def content(self, blocks):
        return {'title': '模板测试', 'pages': [{'id': 'content', 'kind': 'content', 'title': '正文', 'blocks': blocks}]}

    def test_long_text_is_retained_and_escaped(self):
        long = '原文不应被截断<&>\n' * 600 + '最后一段结束'
        output, report = Builder(self.content([{'type': 'paragraphs', 'paragraphs': [long]}]), self.base).build()
        self.assertIn(long, unescape(output))
        self.assertIn('&lt;&amp;&gt;', output)
        self.assertFalse(report['automatic_pagination'])
        self.assertFalse(report['overflow_checked'])
        self.assertIsNone(report['visual_qa_passed'])

    def test_four_hotels_require_explicit_compact(self):
        hotels = {'type': 'hotels', 'items': [{'name': '参考酒店', 'exterior': 'wide.png', 'room': 'wide.png'} for _ in range(4)]}
        with self.assertRaisesRegex(ValueError, 'compact:true'):
            Builder(self.content([hotels]), self.base).build()
        hotels['compact'] = True
        output, report = Builder(self.content([hotels]), self.base).build()
        self.assertEqual(output.count('class="hotel-row"'), 4)
        self.assertEqual(output.count('style="height:40mm;'), 8)
        self.assertEqual(report['missing_image_count'], 0)

    def test_flights_belong_to_corresponding_days_without_invented_fields(self):
        output, _ = Builder(self.content([
            {'type': 'day', 'day': 1, 'title': '抵达', 'flight': {'route': '起点 → 终点'}, 'paragraphs': ['首日原文']},
            {'type': 'day', 'day': 8, 'title': '返程', 'flight': {'route': '终点 → 起点', 'time': '已确认时刻'}, 'paragraphs': ['末日原文']}
        ]), self.base).build()
        first = output.split('data-day="1">')[1].split('</article>')[0]
        last = output.split('data-day="8">')[1].split('</article>')[0]
        self.assertLess(first.index('</header>'), first.index('class="flight-strip"'))
        self.assertLess(first.index('class="flight-strip"'), first.index('首日原文'))
        self.assertIn('起点 → 终点', first)
        self.assertIn('终点 → 起点｜已确认时刻', last)
        for fake in ['A320', '07:30', '00:05', '¥', '2299', '航班号：', '早餐：']:
            self.assertNotIn(fake, output)

    def test_unique_named_poster_sizes_and_contain(self):
        output, report = Builder({'pages': [
            {'id': 'long', 'kind': 'cover', 'mode': 'poster', 'image': 'portrait.png'},
            {'id': 'wide', 'kind': 'cover', 'mode': 'poster', 'image': 'wide.png'},
            {'id': 'a4', 'kind': 'cover', 'mode': 'contain', 'image': 'portrait.png'}
        ]}, self.base).build()
        self.assertEqual([p['height_mm'] for p in report['pages']], [420, 105, 297])
        self.assertIn('@page poster_001{size:210.000000mm 420.000000mm', output)
        self.assertIn('@page poster_002{size:210.000000mm 105.000000mm', output)
        self.assertEqual(output.count('class="page cover'), 3)

    def test_images_record_provenance_and_missing_are_explicit(self):
        item = {'image': {'path': 'wide.png', 'source_url': 'https://example.org/photo', 'role': 'meal-reference', 'reference_label': '仅为风格参考'}, 'caption': '餐食'}
        output, report = Builder(self.content([{'type': 'gallery', 'items': [item, {'image': 'absent.png'}]}]), self.base).build()
        self.assertIn('data:image/png;base64,', output)
        self.assertIn('仅为风格参考', output)
        self.assertIn('图片待补充', output)
        self.assertEqual(report['missing_image_count'], 1)
        self.assertEqual(len(report['assets'][0]['sha256']), 64)
        self.assertEqual(report['assets'][0]['source_url'], 'https://example.org/photo')
        self.assertTrue(Path(report['assets'][0]['path']).is_absolute())

    def test_data_uri_and_network_refusal(self):
        encoded = 'data:image/png;base64,' + base64.b64encode((self.base / 'wide.png').read_bytes()).decode()
        _, report = Builder(self.content([{'type': 'gallery', 'items': [{'image': encoded}]}]), self.base).build()
        self.assertEqual(report['assets'][0]['path'], '<embedded-data-uri>')
        with self.assertRaisesRegex(ValueError, 'remote image URLs'):
            Builder(self.content([{'type': 'gallery', 'items': [{'image': 'https://example.org/photo.png'}]}]), self.base).build()

    def test_palette_follows_theme_and_preserves_page_structure(self):
        spec = self.content([{'type': 'day', 'day': 1, 'title': '行程', 'paragraphs': ['完整原文'], 'flight': {'route': '起点 → 终点'}, 'meals': {'lunch': '按原文'}}])
        before, old_report = Builder(spec, self.base).build()
        spec['theme'] = {'frame': '#173c54', 'accent': '#2a8193', 'paper': '#f9fcff', 'text': '#18303f'}
        after, report = Builder(spec, self.base).build()
        self.assertEqual(before.split('<body>')[1], after.split('<body>')[1])
        self.assertEqual(old_report['pages'], report['pages'])
        for role in ['surface', 'tint', 'line']:
            self.assertNotEqual(old_report['palette'][role], report['palette'][role])
        self.assertTrue(all(item['meets_4_5'] for item in report['contrast_checks']))

    def test_light_frame_has_readable_titles_and_poor_body_colors_warn(self):
        self.assertAlmostEqual(contrast('#000000', '#ffffff'), 21)
        spec = self.content([])
        spec['theme'] = {'frame': '#e7f2fa', 'paper': '#ffffff', 'text': '#18303f'}
        _, report = Builder(spec, self.base).build()
        palette = report['palette']
        self.assertGreaterEqual(contrast(palette['on_frame'], palette['frame']), 4.5)
        self.assertGreaterEqual(contrast(palette['heading'], palette['paper']), 4.5)
        spec['theme']['text'] = '#eeeeee'
        _, report = Builder(spec, self.base).build()
        self.assertEqual(report['palette']['text'], '#eeeeee')
        self.assertTrue(any('text/paper contrast' in warning for warning in report['warnings']))
        self.assertIsNone(report['visual_qa_passed'])

    def test_asset_license_metadata_is_recorded_not_asserted_as_verified(self):
        metadata = {'creator': 'Example author', 'license_name': 'Review required', 'license_url': 'https://example.org/terms', 'attribution': 'Credit supplied by source', 'usage_scope': 'Brochure only', 'checked_on': '2026-01-01'}
        spec = self.content([{'type': 'gallery', 'items': [{'image': {'path': 'wide.png', **metadata}}]}])
        output, report = Builder(spec, self.base).build()
        for key, value in metadata.items():
            self.assertEqual(report['assets'][0][key], value)
        self.assertFalse(report['license_metadata_verified'])
        self.assertNotIn(metadata['license_url'], output)
        spec['pages'][0]['blocks'][0]['items'][0]['image']['creator'] = ['not a string']
        with self.assertRaisesRegex(ValueError, 'creator must be a string'):
            Builder(spec, self.base).build()

    def test_example_is_generic_and_preserves_order(self):
        path = Path(__file__).resolve().parents[1] / 'assets' / 'html-grid' / 'example.json'
        raw = path.read_text(encoding='utf-8')
        for private in ['柬埔寨', '三亚', '暹粒', '金边', 'Sokha', 'Smiling', 'LENOVO', '2299', 'A320', '微信', 'wxid']:
            self.assertNotIn(private, raw)
        spec = json.loads(raw)
        output, report = Builder(spec, path.parent).build()
        self.assertEqual([p['id'] for p in report['pages']], ['cover', 'experiences', 'hotels', 'days'])
        self.assertEqual(report['page_count'], 4)
        self.assertGreater(report['missing_image_count'], 0)
        self.assertEqual(output.count('<section class="page '), 4)

    def test_output_protection_and_same_paths(self):
        spec = self.base / 'spec.json'
        spec.write_text(json.dumps(self.content([])), encoding='utf-8')
        out, report = self.base / 'out.html', self.base / 'report.json'
        argv = ['build', str(spec), '--out', str(out), '--report', str(report)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(argv), 0)
            original = out.read_bytes()
            self.assertEqual(main(argv), 2)
            self.assertEqual(out.read_bytes(), original)
            self.assertEqual(main(argv + ['--overwrite']), 0)
            self.assertEqual(main(['build', str(spec), '--out', str(spec), '--report', str(report), '--overwrite']), 2)

    def test_invalid_style_and_page_ids_fail(self):
        spec = self.content([])
        spec['theme'] = {'paper': 'red;display:none'}
        with self.assertRaises(ValueError):
            Builder(spec, self.base).build()
        spec = self.content([])
        spec['pages'].append(copy.deepcopy(spec['pages'][0]))
        with self.assertRaisesRegex(ValueError, 'unique'):
            Builder(spec, self.base).build()

    def test_outputs_cannot_replace_images_template_or_missing_source(self):
        image = self.base / 'wide.png'
        missing = self.base / 'missing.png'
        template = Path(__file__).resolve().parents[1] / 'assets' / 'html-grid' / 'template.html'
        spec = self.base / 'spec.json'
        spec.write_text(json.dumps(self.content([{'type': 'gallery', 'items': [{'image': 'wide.png'}, {'image': 'missing.png'}]}])), encoding='utf-8')
        snapshots = {p: p.read_bytes() for p in (image, template)}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for destination in (image, missing, template):
                for option in ('out', 'report'):
                    output = destination if option == 'out' else self.base / 'result.html'
                    report = destination if option == 'report' else self.base / 'result.json'
                    self.assertEqual(main(['build', str(spec), '--out', str(output), '--report', str(report), '--overwrite']), 2)
            self.assertEqual(main(['build', str(spec), '--out', str(self.base), '--report', str(self.base / 'result.json'), '--overwrite']), 2)
        for path, before in snapshots.items():
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse(missing.exists())
        self.assertFalse((self.base / 'result.html').exists())

    def test_hard_link_alias_cannot_overwrite_source_or_other_output(self):
        image = self.base / 'wide.png'
        alias = self.base / 'image-alias.html'
        try:
            os.link(image, alias)
        except OSError as error:
            self.skipTest(f'hard links unavailable: {error}')
        spec = self.base / 'spec.json'
        spec.write_text(json.dumps(self.content([{'type': 'gallery', 'items': [{'image': 'wide.png'}]}])), encoding='utf-8')
        before = image.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['build', str(spec), '--out', str(alias), '--report', str(self.base / 'result.json'), '--overwrite']), 2)
            report = self.base / 'report-alias.json'
            os.link(alias, report)
            self.assertEqual(main(['build', str(spec), '--out', str(alias), '--report', str(report), '--overwrite']), 2)
        self.assertEqual(image.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
