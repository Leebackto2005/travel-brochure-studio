"""Requirement checks use native PDF text and independently scoped HTML."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import fitz

from check_delivery import check_delivery, main, normalize


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf, self.html = self.root/'final.pdf', self.root/'source.html'
        self.spec_path, self.report = self.root/'requirements.json', self.root/'report.json'
        self.make_pdf(['Hotel Four\nD1\n07:30-10:30', 'D8\n00:05-01:05\nTransfer retained'])
        self.html.write_text('<section class="page"><h2>Hotel Four</h2><div class="day"><h2>D1</h2><p>07:30-10:30</p></div></section><section class="page"><div class="day"><h2>D8</h2><p>00:05-01:05</p><p>Transfer retained</p></div></section>', encoding='utf-8')
        self.spec = {'schema_version': 1, 'page_count': 2,
                     'html_checks': [{'id': 'return', 'page': 2, 'selector': '.day', 'count': 1, 'contains': ['00:05-01:05', 'Transfer retained'], 'ordered': ['D8', '00:05-01:05']},
                                     {'id': 'no-old', 'page': 2, 'selector': '.old-flight', 'count': 0}],
                     'pdf_checks': [{'id': 'hotel', 'page': 1, 'contains': ['Hotel Four'], 'excludes': ['Hotel Old']},
                                    {'id': 'return', 'page': 2, 'ordered': ['D8', '00:05-01:05'], 'contains': ['Transfer retained']}]}

    def make_pdf(self, texts):
        with fitz.open() as doc:
            for text in texts:
                page = doc.new_page()
                if text:
                    page.insert_text((40, 50), text)
            doc.save(self.pdf)

    def check(self):
        return check_delivery(self.spec, self.pdf, self.html)

    def test_correct_pair_reports_hashes_and_limited_evidence(self):
        result = self.check()
        self.assertTrue(result['passed'])
        self.assertEqual(result['actual_page_counts'], {'pdf': 2, 'html': 2})
        self.assertEqual(len(result['sources']['html']['sha256']), 64)
        self.assertIsNone(result['visual_qa_passed'])
        self.assertIsNone(result['business_facts_verified'])

    def test_stale_html_fails_even_with_correct_pdf(self):
        self.html.write_text(self.html.read_text().replace('00:05-01:05', '01:00-02:00'))
        result = self.check()
        self.assertFalse(result['passed'])
        self.assertTrue(all(row['passed'] for row in result['checks'] if row['target'] == 'pdf'))

    def test_stale_pdf_fails_even_with_correct_html(self):
        self.make_pdf(['Hotel Old\nD1\n07:30-10:30', 'D8\n00:05-01:05\nTransfer retained'])
        result = self.check()
        self.assertFalse(result['passed'])
        self.assertTrue(all(row['passed'] for row in result['checks'] if row['target'] == 'html'))

    def test_pdf_order_is_required_not_just_presence(self):
        self.make_pdf(['Hotel Four\nD1\n07:30-10:30', '00:05-01:05\nD8\nTransfer retained'])
        result = self.check()
        self.assertFalse(result['passed'])
        self.assertIn('out of order', result['checks'][1]['problems'][0]['reason'])

    def test_text_on_wrong_day_does_not_satisfy_html_scope(self):
        self.html.write_text(self.html.read_text().replace('<p>00:05-01:05</p>', '') + '<div>00:05-01:05</div>')
        self.assertFalse(self.check()['passed'])

    def test_selector_cardinality_and_obsolete_nodes(self):
        original = self.html.read_text()
        for altered in [original.replace('class="day"', 'class="other"'), original.replace('Transfer retained', '<span class="old-flight">Old time</span>Transfer retained')]:
            self.html.write_text(altered)
            self.assertFalse(self.check()['passed'])

    def test_page_count_and_image_only_page_do_not_pass(self):
        self.make_pdf(['Hotel Four\nD1', ''])
        result = self.check()
        self.assertFalse(result['passed'])
        self.assertIn('no native text', result['checks'][1]['problems'][0]['reason'])
        self.make_pdf(['Hotel Four'])
        result = self.check()
        self.assertFalse(result['page_counts_match'])
        self.assertFalse(result['passed'])

    def test_normalization_does_not_hide_changed_time_or_amount(self):
        self.assertEqual(normalize('参考航班：\n０７：３０'), normalize('参考航班: 07:30'))
        self.assertNotEqual(normalize('07:30'), normalize('07:35'))
        self.assertNotEqual(normalize('2,399'), normalize('2,499'))

    def test_empty_mistyped_and_unscoped_requirements_rejected(self):
        variants = [dict(self.spec, html_checks=[], pdf_checks=[]), dict(self.spec, page_count=True)]
        for field, value in [('contians', ['text']), ('page', 3), ('count', True), ('ordered', ['D8']), ('contains', [' '])]:
            spec = copy.deepcopy(self.spec)
            spec['html_checks'][0][field] = value
            variants.append(spec)
        for spec in variants:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                check_delivery(spec, self.pdf, self.html)
        with self.assertRaises(ValueError):
            check_delivery(self.spec, self.pdf)

    def test_cli_report_and_input_protection(self):
        self.spec_path.write_text(json.dumps(self.spec), encoding='utf-8')
        args = ['--html', str(self.html), '--pdf', str(self.pdf), '--spec', str(self.spec_path)]
        self.assertEqual(main(args + ['--report', str(self.report)]), 0)
        result = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(len(result['requirements']['sha256']), 64)
        before = self.pdf.read_bytes()
        self.assertEqual(main(args + ['--report', str(self.pdf), '--overwrite']), 2)
        self.assertEqual(self.pdf.read_bytes(), before)
        self.make_pdf(['Hotel Old', 'D8\nWrong time'])
        self.assertEqual(main(args + ['--report', str(self.report), '--overwrite']), 1)


if __name__ == '__main__':
    unittest.main()
