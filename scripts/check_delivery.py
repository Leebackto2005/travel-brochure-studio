#!/usr/bin/env python3
"""Check explicit revision requirements in paged HTML and native PDF text.

This reads local files only. It neither infers product facts nor certifies
visual acceptance. Image-only PDF pages need separate visual/OCR review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import unicodedata

from bs4 import BeautifulSoup
from soupsieve.util import SelectorSyntaxError

from html_brochure import PageSpans, atomic_text, ensure_target, read_source
from pdf_pages import file_record, open_pdf


def normalize(value):
    # Ignore line wraps, spaces and full-width forms, but retain punctuation,
    # digits and case so changed amounts or flight times cannot pass silently.
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def positive_int(value):
    return type(value) is int and value > 0


def validate_spec(spec):
    allowed = {'schema_version', 'page_count', 'html_checks', 'pdf_checks'}
    if not isinstance(spec, dict) or set(spec) - allowed:
        raise ValueError('Unknown or invalid requirement spec fields')
    if type(spec.get('schema_version')) is not int or spec['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    if not positive_int(spec.get('page_count')):
        raise ValueError('page_count must be a positive integer')
    total = 0
    for kind in ('html', 'pdf'):
        checks = spec.get(kind + '_checks', [])
        if not isinstance(checks, list):
            raise ValueError(f'{kind}_checks must be an array')
        seen = set()
        for check in checks:
            keys = {'id', 'page', 'contains', 'excludes', 'ordered'}
            if kind == 'html':
                keys |= {'selector', 'count'}
            if not isinstance(check, dict) or set(check) - keys:
                raise ValueError(f'Unknown {kind} check fields')
            name = check.get('id')
            if not isinstance(name, str) or not name.strip() or name in seen:
                raise ValueError(f'{kind} check ids must be nonempty and unique')
            seen.add(name)
            if not positive_int(check.get('page')) or check['page'] > spec['page_count']:
                raise ValueError(f'{name}: page must be within page_count (1-based)')
            text_count = 0
            for field in ('contains', 'excludes', 'ordered'):
                values = check.get(field, [])
                if not isinstance(values, list) or any(not isinstance(v, str) or not normalize(v) for v in values):
                    raise ValueError(f'{name}: {field} must be an array of nonempty strings')
                text_count += len(values)
            if 'ordered' in check and len(check['ordered']) < 2:
                raise ValueError(f'{name}: ordered needs at least two text fragments')
            if kind == 'html':
                if 'selector' in check and (not isinstance(check['selector'], str) or not check['selector'].strip()):
                    raise ValueError(f'{name}: selector must be nonempty')
                count = check.get('count', 1)
                if type(count) is not int or count < 0:
                    raise ValueError(f'{name}: count must be a nonnegative integer')
                if count == 0 and text_count:
                    raise ValueError(f'{name}: text checks require matching nodes')
                if not text_count and not ('selector' in check and 'count' in check):
                    raise ValueError(f'{name}: provide text requirements or an explicit selector/count')
            elif not text_count:
                raise ValueError(f'{name}: PDF checks require text requirements')
        total += len(checks)
    if not total:
        raise ValueError('Provide at least one explicit requirement')


def text_problems(value, check):
    value = normalize(value)
    problems = []
    for fragment in check.get('contains', []):
        if normalize(fragment) not in value:
            problems.append({'reason': 'required text missing', 'text': fragment})
    for fragment in check.get('excludes', []):
        if normalize(fragment) in value:
            problems.append({'reason': 'obsolete text still present', 'text': fragment})
    offset = 0
    for fragment in check.get('ordered', []):
        token = normalize(fragment)
        found = value.find(token, offset)
        if found < 0:
            problems.append({'reason': 'text missing or out of order', 'text': fragment})
            break
        offset = found + len(token)
    return problems


def check_delivery(spec, pdf_path, html_path=None):
    validate_spec(spec)
    if spec.get('html_checks') and html_path is None:
        raise ValueError('html_checks require --html')
    sources = {'pdf': file_record(pdf_path)}
    results, page_counts = [], {}
    with open_pdf(pdf_path) as doc:
        page_counts['pdf'] = len(doc)
        for check in spec.get('pdf_checks', []):
            problems = []
            if check['page'] > len(doc):
                problems.append({'reason': 'page missing'})
            else:
                value = doc[check['page'] - 1].get_text('text')
                if not normalize(value):
                    problems.append({'reason': 'no native text; visual/OCR review required'})
                else:
                    problems.extend(text_problems(value, check))
            results.append({'target': 'pdf', 'id': check['id'], 'page': check['page'], 'passed': not problems, 'problems': problems})
    if html_path is not None:
        sources['html'] = file_record(html_path)
        source = read_source(html_path)
        sections = [BeautifulSoup(source[start:end], 'html.parser').select_one('section.page')
                    for start, end, _ in PageSpans(source).spans]
        page_counts['html'] = len(sections)
        for check in spec.get('html_checks', []):
            problems, nodes = [], []
            if check['page'] > len(sections):
                problems.append({'reason': 'page missing'})
            else:
                section = sections[check['page'] - 1]
                nodes = section.select(check['selector']) if 'selector' in check else [section]
                if len(nodes) != check.get('count', 1):
                    problems.append({'reason': 'selector count differs', 'expected': check.get('count', 1), 'actual': len(nodes)})
                for index, node in enumerate(nodes, 1):
                    problems.extend({'node': index, **problem} for problem in text_problems(node.get_text(' ', strip=True), check))
            results.append({'target': 'html', 'id': check['id'], 'page': check['page'], 'matched_nodes': len(nodes), 'passed': not problems, 'problems': problems})
    count_ok = all(count == spec['page_count'] for count in page_counts.values())
    stable = all(file_record(record['path'])['sha256'] == record['sha256'] for record in sources.values())
    return {
        'schema_version': 1, 'operation': 'check_delivery_requirements', 'sources': sources,
        'expected_page_count': spec['page_count'], 'actual_page_counts': page_counts,
        'page_counts_match': count_ok, 'inputs_unchanged_during_check': stable,
        'checks': results, 'passed': count_ok and stable and all(row['passed'] for row in results),
        'coverage': {kind: len(spec.get(kind + '_checks', [])) for kind in ('html', 'pdf')},
        'visual_qa_passed': None, 'business_facts_verified': None,
        'limits': ['Checks cover only supplied requirements, not whole-document equivalence.',
                   'HTML DOM and PDF native text may include hidden content; inspect final PDF visually.',
                   'No OCR, image identity, typography, crop, license or business-fact verification.'],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--html', type=Path)
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    inputs = [args.pdf, args.spec] + ([args.html] if args.html else [])
    try:
        ensure_target(args.report, inputs, args.overwrite)
        spec_record = file_record(args.spec)
        spec = json.loads(args.spec.read_text(encoding='utf-8-sig'))
        report = check_delivery(spec, args.pdf, args.html)
        report['requirements'] = spec_record
        if file_record(args.spec)['sha256'] != spec_record['sha256']:
            raise ValueError('Requirements changed during check; rerun with a stable spec')
        atomic_text(args.report, json.dumps(report, ensure_ascii=False, indent=2) + '\n', args.overwrite, inputs)
        print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'report': str(args.report)}, ensure_ascii=False))
        return 0 if report['passed'] else 1
    except (ValueError, OSError, RuntimeError, SelectorSyntaxError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
