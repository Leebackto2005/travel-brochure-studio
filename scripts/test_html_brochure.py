"""Meaningful source-preservation and failure tests for html_brochure.py."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bs4 import BeautifulSoup
from PIL import Image

import html_brochure as tool


class HtmlBrochureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.source = self.root / 'brochure.html'
        self.output = self.root / 'updated.html'
        self.report = self.root / 'report.json'
        self.image = self.root / 'room.png'
        Image.new('RGB', (32, 20), '#614020').save(self.image)
        self.raw = (
            '<!DOCTYPE html>\r\n<html><head><style>.page{color:#614020}</style></head>\r\n<body>\r\n'
            '<section class="page cover" data-page-id="cover">\r\n'
            '  <h1>原海报 &amp; 标题</h1><img alt=海报 src="room.png">\r\n</section>\r\n'
            '<!-- page separator must remain byte-identical -->\r\n'
            '<section class="page" data-page-id="hotels">\r\n'
            '<h2 class="title">酒店</h2><div class="entry"><img class="room" src="room.png"></div>\r\n'
            '</section>\r\n'
            '<section class="page itinerary" data-page-id="day1">\r\n'
            '<article class="day" data-day="1"><h2>第一天</h2><p>早餐自理</p></article>\r\n'
            '<IMG SRC="room.png" alt="旧式图像"><!-- retain capital letters -->\r\n'
            '</section>\r\n</body></html>\r\n'
        )
        self.source.write_bytes(self.raw.encode('utf8'))
        self.original = self.source.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, **extra):
        operation = {'page': 2, 'selector': '.title', 'expected_count': 1,
                     'expected_text': '酒店', 'action': 'text', 'value': '四家参考酒店'}
        operation.update(extra)
        return {'operations': [operation]}

    def cli(self, *arguments):
        return subprocess.run([sys.executable, '-X', 'utf8', str(Path(tool.__file__)), *map(str, arguments)],
                              capture_output=True, text=True, encoding='utf8')

    def test_patch_preserves_other_page_bytes_and_outer_markup_crlf(self):
        result, report = tool.patch(self.source, self.spec(), self.root)
        old = tool.PageSpans(self.raw).spans
        new = tool.PageSpans(result).spans
        for index in (0, 2):
            a, b, _ = old[index]
            c, d, _ = new[index]
            self.assertEqual(self.raw[a:b].encode('utf8'), result[c:d].encode('utf8'))
        self.assertEqual(self.raw[:old[0][0]], result[:new[0][0]])
        self.assertEqual(self.raw[old[-1][1]:], result[new[-1][1]:])
        self.assertIn('<!-- page separator must remain byte-identical -->\r\n', result)
        self.assertEqual(report['unchanged_html_page_bytes_verified'], [1, 3])
        self.assertEqual(report['changed_pages'], [2])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(report['output_sha256'], tool.digest(result.encode('utf8')))

    def test_inspect_reports_original_source_and_page_hashes(self):
        info = tool.inventory(self.source)
        self.assertEqual(info['page_count'], 3)
        self.assertEqual(info['source_sha256'], tool.digest(self.original))
        a, b, _ = tool.PageSpans(self.raw).spans[0]
        self.assertEqual(info['pages'][0]['sha256'], tool.digest(self.raw[a:b].encode()))
        self.assertEqual(info['pages'][2]['days'], ['1'])

    def test_multiple_actions_keep_order_and_remove_only_target(self):
        spec = {'operations': [
            {'page': 2, 'selector': '.title', 'action': 'insert_after', 'value': '<p class="lead">先看外观</p><p>再看双床</p>'},
            {'page': 2, 'selector': '.entry', 'action': 'attrs', 'value': {'data-role': 'hotel', 'style': 'display:grid'}},
            {'page': 2, 'selector': '.lead', 'action': 'insert_before', 'value': '<strong>说明</strong>'},
            {'page': 2, 'selector': '.lead', 'action': 'remove'},
        ]}
        result, report = tool.patch(self.source, spec, self.root)
        soup = BeautifulSoup(result, 'html.parser')
        hotel = soup.select('section.page')[1]
        self.assertEqual(hotel.get_text(' ', strip=True), '酒店 说明 再看双床')
        self.assertEqual(hotel.select_one('.entry')['data-role'], 'hotel')
        self.assertEqual(len(soup.select('section.page')), 3)
        self.assertEqual(report['changed_pages'], [2])

    def test_image_patch_embeds_pixels_and_records_provenance(self):
        spec = {'operations': [{'page': 2, 'selector': 'img.room', 'action': 'image', 'path': 'room.png',
                                'fit': 'contain', 'position': '50% 70%', 'alt': '双床房',
                                'source_url': 'https://example.org/official-gallery', 'role': 'hotel-room'}]}
        result, report = tool.patch(self.source, spec, self.root)
        image = BeautifulSoup(result, 'html.parser').select_one('img.room')
        self.assertEqual(base64.b64decode(image['src'].split(',', 1)[1]), self.image.read_bytes())
        self.assertIn('object-fit:contain', image['style'])
        self.assertIn('object-position:50% 70%', image['style'])
        self.assertEqual(image['alt'], '双床房')
        record = next(x for x in report['operations'] if 'image' in x)
        self.assertEqual(record['image']['sha256'], tool.digest(self.image.read_bytes()))
        self.assertEqual(record['source_url'], spec['operations'][0]['source_url'])
        self.assertEqual(record['role'], 'hotel-room')

    def test_embed_only_changes_src_and_keeps_legacy_img_syntax(self):
        result, report = tool.embed(self.source)
        uri, _ = tool.local_uri('room.png', self.root)
        self.assertEqual(result, self.raw.replace('"room.png"', '"' + uri + '"'))
        self.assertEqual(len(report['images_embedded']), 3)
        self.assertIn('<IMG SRC=', result)
        self.assertIn('<img alt=海报', result)
        self.assertIn('\r\n', result)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_embed_ignores_comments_script_and_attribute_lookalikes(self):
        raw = ('<!-- <img src="not-real.png"> -->\r\n'
               '<script>const sample = \'<img src="not-real.png">\';</script>\r\n'
               '<img data-src="not-real.png" title="literal src=\'not-real.png\' >" src="room.png">\r\n')
        self.source.write_bytes(raw.encode())
        result, report = tool.embed(self.source)
        uri, _ = tool.local_uri('room.png', self.root)
        self.assertEqual(result, raw.replace('src="room.png"', 'src="' + uri + '"'))
        self.assertEqual(len(report['images_embedded']), 1)

    def test_embed_already_embedded_is_idempotent(self):
        result, _ = tool.embed(self.source)
        self.output.write_bytes(result.encode('utf8'))
        repeated, report = tool.embed(self.output)
        self.assertEqual(repeated.encode('utf8'), self.output.read_bytes())
        self.assertEqual(report['images_embedded'], [])

    def test_embed_rejects_unquoted_src_and_ambiguous_srcset(self):
        for raw in ('<img src=room.png>', '<img src="room.png" srcset="room.png 1x">', '<img alt="no source">'):
            with self.subTest(raw=raw):
                self.source.write_text(raw, encoding='utf8')
                with self.assertRaises(ValueError):
                    tool.embed(self.source)

    def test_remote_and_nonimage_sources_are_rejected(self):
        text = self.root / 'notes.txt'
        text.write_text('not an image')
        values = ('https://example.org/room.png', '//example.org/room.png', r'\\example.org\share\room.png',
                  'data:text/html,<script>x</script>', 'data:image/png;base64,!', 'notes.txt', '')
        for raw in values:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                tool.local_uri(raw, self.root)

    def test_patch_invalid_page_count_selector_and_action_rejected(self):
        for extra in ({'page': True}, {'page': 0}, {'page': 4}, {'expected_count': True},
                      {'expected_count': 0}, {'expected_count': 2}, {'selector': '.missing'},
                      {'selector': '['}, {'selector': ''}, {'action': 'invented'},
                      {'selector': 'section.page'}, {'expected_text': 'wrong source'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                tool.patch(self.source, self.spec(**extra), self.root)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_fragments_reject_script_handlers_remote_css_and_nested_pages(self):
        fragments = ('<script>alert(1)</script>', '<img src="data:image/png;base64,AA==" onload="x()">',
                     '<div style="background:url(https://example.org/a)">X</div>',
                     '<meta http-equiv="refresh" content="0;url=https://example.org">',
                     '<img srcset="https://example.org/a 1x">',
                     '<section class="page">nested page</section>')
        for fragment in fragments:
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                tool.patch(self.source, self.spec(action='html', value=fragment), self.root)

    def test_cli_precondition_failure_leaves_no_output_or_report(self):
        for spec in (self.spec(expected_text='stale text'),
                     {'source_sha256': 'incorrect', **self.spec()},
                     {'operations': [self.spec()['operations'][0], {'page': 3, 'selector': '.missing', 'action': 'remove'}]}):
            path = self.root / 'spec.json'
            path.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf8')
            result = self.cli('patch', self.source, '--spec', path, '--out', self.output, '--report', self.report)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.report.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_success_outputs_updated_html_and_report(self):
        spec = self.root / 'spec.json'
        spec.write_text(json.dumps(self.spec()), encoding='utf8')
        result = self.cli('patch', self.source, '--spec', spec, '--out', self.output, '--report', self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('四家参考酒店', self.output.read_text(encoding='utf8'))
        report = json.loads(self.report.read_text(encoding='utf8'))
        self.assertEqual(report['output_sha256'], tool.digest(self.output.read_bytes()))
        self.assertEqual(report['unchanged_html_page_bytes_verified'], [1, 3])

    def test_output_guard_preserves_inputs_and_existing_files(self):
        self.output.write_text('previous delivery', encoding='utf8')
        with self.assertRaises(ValueError):
            tool.atomic_text(self.output, 'new', inputs=[self.source])
        self.assertEqual(self.output.read_text(), 'previous delivery')
        for overwrite in (False, True):
            with self.assertRaises(ValueError):
                tool.atomic_text(self.source, 'bad', overwrite, [self.source])
        tool.atomic_text(self.output, 'approved replacement', True, [self.source])
        self.assertEqual(self.output.read_text(), 'approved replacement')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_hardlink_alias_cannot_overwrite_input(self):
        alias = self.root / 'input-alias.html'
        try:
            os.link(self.source, alias)
        except OSError as exc:
            self.skipTest(f'Hard links unavailable: {exc}')
        with self.assertRaises(ValueError):
            tool.atomic_text(alias, 'bad', True, [self.source])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_report_conflict_rejected_before_any_output(self):
        result = self.cli('embed', self.source, '--out', self.output, '--report', self.source, '--overwrite')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_outputs_cannot_destroy_embedded_image_inputs(self):
        original_image = self.image.read_bytes()
        result = self.cli('embed', self.source, '--out', self.image, '--overwrite')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.image.read_bytes(), original_image)
        result = self.cli('embed', self.source, '--out', self.output, '--report', self.image, '--overwrite')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.image.read_bytes(), original_image)


if __name__ == '__main__':
    unittest.main()
