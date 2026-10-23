"""Inspect, embed, and make scoped edits in paged brochure HTML.

Page boundaries are located in the original source. Unchanged page bytes are
never reserialized through BeautifulSoup (legacy img tags may normalize).
"""
from __future__ import annotations
import argparse, base64, hashlib, json, mimetypes, os, re, tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from bs4 import BeautifulSoup
from soupsieve.util import SelectorSyntaxError


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_source(path):
    # Preserve source CRLF and UTF-8 bytes while working with character offsets.
    with Path(path).open('r', encoding='utf8', newline='') as stream:
        return stream.read()


class PageSpans(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0]
        self.lines += [m.end() for m in re.finditer('\n', text)]
        self.depth = 0
        self.active = None
        self.spans = []
        self.feed(text)
        self.close()
        if self.active is not None:
            raise ValueError('Unclosed section.page')
        if not self.spans:
            raise ValueError('No section.page found; inspect/adapt the source first')

    def source_offset(self):
        row, col = self.getpos()
        return self.lines[row - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag != 'section':
            return
        self.depth += 1
        attrs = dict(attrs)
        if 'page' in (attrs.get('class') or '').split():
            if self.active:
                raise ValueError('Nested section.page is unsupported')
            self.active = (self.depth, self.source_offset(), attrs)

    def handle_endtag(self, tag):
        if tag != 'section':
            return
        if self.active and self.active[0] == self.depth:
            start = self.active[1]
            end = self.text.index('>', self.source_offset()) + 1
            self.spans.append((start, end, self.active[2]))
            self.active = None
        self.depth -= 1


def ensure_target(path, inputs=(), overwrite=False):
    path = Path(path).resolve()
    if any(path == Path(p).resolve() or (path.exists() and Path(p).exists() and os.path.samefile(path, p)) for p in inputs):
        raise ValueError(f'Output must not overwrite an input: {path}')
    if path.exists() and not overwrite:
        raise ValueError(f'Output exists; choose another path or use --overwrite: {path}')
    if path.exists() and not path.is_file():
        raise ValueError(f'Output is not a regular file: {path}')
    return path


def atomic_text(path, text, overwrite=False, inputs=()):
    path = ensure_target(path, inputs, overwrite)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.brochure-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        ensure_target(path, inputs, overwrite)
        if overwrite:
            os.replace(tmp, path)
        elif os.name == 'nt':
            os.rename(tmp, path)
        else:
            os.link(tmp, path)
            os.unlink(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def local_uri(raw, base):
    if not isinstance(raw, str) or not raw:
        raise ValueError('Image path must be a nonempty string')
    if raw.startswith('data:'):
        if not re.match(r'^data:image/[A-Za-z0-9.+-]+(?:;[^,]*)?,.+', raw, re.S):
            raise ValueError('Only nonempty image data URIs are accepted')
        if ';base64,' in raw:
            try:
                if not base64.b64decode(raw.split(',', 1)[1], validate=True):
                    raise ValueError('Empty image data URI')
            except (ValueError, base64.binascii.Error) as exc:
                raise ValueError('Invalid base64 image data URI') from exc
        return raw, {'kind': 'embedded', 'sha256': digest(raw.encode())}
    parsed = urlsplit(raw)
    if parsed.netloc or raw.startswith('\\\\') or (parsed.scheme and not re.match(r'^[A-Za-z]:[\\/]', raw)):
        raise ValueError(f'Only local files or data URIs are accepted: {raw[:100]}')
    p = Path(unquote(raw))
    if not p.is_absolute():
        p = Path(base) / p
    p = p.resolve()
    data = p.read_bytes()
    media = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
    if not media.startswith('image/'):
        raise ValueError(f'Expected an image: {p}')
    return 'data:' + media + ';base64,' + base64.b64encode(data).decode(), {'path': str(p), 'sha256': digest(data)}


def inventory(source):
    p = Path(source)
    text = read_source(p)
    pages = []
    for i, (start, end, attrs) in enumerate(PageSpans(text).spans, 1):
        fragment = text[start:end]
        s = BeautifulSoup(fragment, 'html.parser')
        pages.append({'page': i, 'page_id': attrs.get('data-page-id'),
                      'sha256': digest(fragment.encode()),
                      'text': s.get_text(' ', strip=True),
                      'images': [{'src': x.get('src', '') if not x.get('src', '').startswith('data:') else '[embedded]',
                                  'alt': x.get('alt', ''), 'style': x.get('style', '')} for x in s.select('img')],
                      'days': [x.get('data-day') or x.get_text(' ', strip=True)[:45] for x in s.select('.day')]})
    return {'source': str(p.resolve()), 'source_sha256': digest(p.read_bytes()), 'page_count': len(pages), 'pages': pages}


def safe_fragment(value):
    if not isinstance(value, str):
        raise ValueError('HTML fragment must be a string')
    s = BeautifulSoup(value, 'html.parser')
    if s.select('script, iframe, object, embed, link, style, base, meta'):
        raise ValueError('Patch fragments cannot add executable content or global styles')
    for el in s.find_all(True):
        if any(k.lower().startswith('on') for k in el.attrs):
            raise ValueError('Event handlers are not allowed in document fragments')
        if re.search(r'url\s*\(|expression\s*\(', str(el.get('style', '')), re.I):
            raise ValueError('External or executable inline CSS is unsupported')
        if any(key in el.attrs for key in ('srcset', 'srcdoc', 'poster', 'action', 'formaction', 'xlink:href')):
            raise ValueError('Unsupported external or executable fragment attribute')
        for key in ('src', 'href'):
            val = el.get(key, '')
            if val and not (val.startswith(('data:image/', '#'))):
                raise ValueError('Embed fragment images first; external resources are not accepted')
    return list(s.contents)


def patch(source, spec, spec_base):
    if not isinstance(spec, dict) or not isinstance(spec.get('operations'), list):
        raise ValueError('Patch spec must contain an operations array')
    text = read_source(source)
    if spec.get('source_sha256') and spec['source_sha256'] != digest(Path(source).read_bytes()):
        raise ValueError('Source SHA256 does not match the requested revision')
    spans = PageSpans(text).spans
    changed, logs = {}, []
    for op in spec.get('operations', []):
        if not isinstance(op, dict):
            raise ValueError('Each operation must be an object')
        target = op.get('page')
        if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= len(spans):
            raise ValueError(f'Page must be a 1-based integer in 1..{len(spans)}')
        idx = target - 1
        a, b, attrs = spans[idx]
        if idx not in changed:
            changed[idx] = BeautifulSoup(text[a:b], 'html.parser')
        soup = changed[idx]
        if not isinstance(op.get('selector'), str) or not op['selector'].strip():
            raise ValueError('Selector must be a nonempty string')
        try:
            nodes = soup.select(op['selector'])
        except SelectorSyntaxError as exc:
            raise ValueError(f'Invalid CSS selector: {op["selector"]!r}') from exc
        expected = op.get('expected_count', 1)
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1 or len(nodes) != expected:
            raise ValueError(f'Page {target}: selector {op["selector"]!r} matched {len(nodes)}, expected {expected}')
        for node in nodes:
            if node.name == 'section' and 'page' in node.get('class', []):
                raise ValueError('Do not replace the page root; target a content element')
            if 'expected_text' in op and node.get_text() != op['expected_text']:
                raise ValueError(f'Page {target}: expected_text does not match')
            action = op['action']
            if action == 'text':
                node.clear(); node.append(str(op['value']))
            elif action in ('html', 'insert_before', 'insert_after'):
                pieces = safe_fragment(op['value'])
                if action == 'html':
                    node.clear()
                    for piece in pieces:
                        node.append(piece)
                elif action == 'insert_before':
                    for piece in pieces:
                        node.insert_before(piece)
                else:
                    anchor = node
                    for piece in pieces:
                        anchor.insert_after(piece); anchor = piece
            elif action == 'remove':
                node.decompose()
            elif action == 'attrs':
                if not isinstance(op['value'], dict):
                    raise ValueError('Attribute value must be an object')
                for key, val in op['value'].items():
                    if key not in ('style', 'class', 'alt', 'title') and not key.startswith('data-'):
                        raise ValueError(f'Unsupported attribute: {key}')
                    if key == 'style' and re.search(r'url\s*\(|expression\s*\(', str(val), re.I):
                        raise ValueError('External or executable inline CSS is unsupported')
                    node[key] = val
            elif action == 'image':
                if node.name != 'img':
                    raise ValueError('image operation requires an img selector')
                node['src'], metadata = local_uri(op['path'], spec_base)
                node.attrs.pop('srcset', None)
                if 'alt' in op:
                    node['alt'] = op['alt']
                if op.get('fit') not in (None, 'contain', 'cover'):
                    raise ValueError('Image fit must be contain or cover; stretch is unsupported')
                styles = node.get('style', '').rstrip(';')
                if op.get('fit') is not None:
                    styles += ';object-fit:' + op['fit']
                if 'position' in op:
                    pos = op['position']
                    if not isinstance(pos, str) or not re.fullmatch(r'\d+(?:\.\d+)?% \d+(?:\.\d+)?%', pos):
                        raise ValueError('Position must be two percentages, e.g. 50% 70%')
                    styles += ';object-position:' + pos
                node['style'] = styles
                logs.append({'page': target, 'image': metadata, 'source_url': op.get('source_url'), 'role': op.get('role')})
            else:
                raise ValueError(f'Unknown action: {action}')
        logs.append({'page': target, 'action': op['action'], 'selector': op['selector'], 'count': len(nodes)})
    if not changed:
        raise ValueError('No operations supplied')
    parts, pos = [], 0
    for i, (a, b, attrs) in enumerate(spans):
        parts.append(text[pos:a])
        parts.append(str(changed[i].select_one('section.page')) if i in changed else text[a:b])
        pos = b
    parts.append(text[pos:])
    result = ''.join(parts)
    new_spans = PageSpans(result).spans
    if len(new_spans) != len(spans):
        raise ValueError('Patch changed the number of pages')
    unchanged = []
    for i, ((a,b,_),(c,d,_)) in enumerate(zip(spans,new_spans)):
        if i not in changed:
            if text[a:b] != result[c:d]:
                raise ValueError(f'Unchanged page {i + 1} bytes unexpectedly changed')
            unchanged.append(i+1)
    return result, {'page_count': len(spans), 'changed_pages': [i+1 for i in sorted(changed)],
                    'unchanged_html_page_bytes_verified': unchanged, 'operations': logs,
                    'source_sha256': digest(Path(source).read_bytes()), 'output_sha256': digest(result.encode()),
                    'visual_review': 'not performed by this command'}


class ImageSpans(HTMLParser):
    """Locate actual img tags, excluding comments and script text."""
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.lines = [0] + [m.end() for m in re.finditer('\n', text)]
        self.spans = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            row, col = self.getpos()
            start = self.lines[row - 1] + col
            raw = self.get_starttag_text()
            self.spans.append((start, start + len(raw), raw))


def embed(source):
    source = Path(source).resolve()
    text = read_source(source)
    # Edit src/srcset byte ranges only. Do not normalize page markup.
    records = []
    def replace(tag):
        s = BeautifulSoup(tag, 'html.parser').find('img')
        if s.get('srcset'):
            raise ValueError('Resolve srcset to one confirmed photograph before embedding')
        raw = s.get('src', '')
        if not raw:
            raise ValueError('Image without src')
        if raw.startswith('data:'):
            local_uri(raw, source.parent)
            return tag
        uri, metadata = local_uri(raw, source.parent)
        records.append(metadata)
        attrs = list(re.finditer(r'''\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''', tag))
        src_attrs = [m for m in attrs if m.group(1).lower() == 'src']
        if len(src_attrs) != 1 or (src_attrs[0].group(2) is None and src_attrs[0].group(3) is None):
            raise ValueError('Quote all img src attributes before embedding')
        src_match = src_attrs[0]
        group = 2 if src_match.group(2) is not None else 3
        return tag[:src_match.start(group)] + uri + tag[src_match.end(group):]
    parts, pos = [], 0
    for start, end, tag in ImageSpans(text).spans:
        parts.extend([text[pos:start], replace(tag)])
        pos = end
    parts.append(text[pos:])
    output = ''.join(parts)
    return output, {'images_embedded': records, 'source_sha256':digest(source.read_bytes()),
                    'note':'img src only; relative stylesheets, fonts and SVG resources are not bundled automatically'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('inspect','embed','patch'):
        p=sub.add_parser(name);p.add_argument('input',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--overwrite',action='store_true')
        if name=='patch':p.add_argument('--spec',type=Path,required=True)
        if name!='inspect':p.add_argument('--report',type=Path)
    args=parser.parse_args()
    inputs=[args.input]+([args.spec] if args.command=='patch' else [])
    target=ensure_target(args.out,inputs,args.overwrite)
    report=getattr(args,'report',None)
    if report:
        ensure_target(report,inputs+[target],args.overwrite)
    if args.command=='inspect':
        result=json.dumps(inventory(args.input),ensure_ascii=False,indent=2);meta=None
    elif args.command=='embed':result,meta=embed(args.input)
    else:result,meta=patch(args.input,json.loads(args.spec.read_text(encoding='utf8')),args.spec.resolve().parent)
    if meta:
        assets = [item.get('path') for item in meta.get('images_embedded', [])]
        assets += [item.get('image', {}).get('path') for item in meta.get('operations', [])]
        inputs += [Path(item) for item in assets if item]
        ensure_target(target, inputs, args.overwrite)
        if report:
            ensure_target(report, inputs+[target], args.overwrite)
    atomic_text(target,result,args.overwrite,inputs)
    if report:atomic_text(report,json.dumps(meta,ensure_ascii=False,indent=2),args.overwrite,inputs+[target])
    print(json.dumps({'output':str(target),'operation':args.command,'report':str(report) if report else None},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError) as exc:raise SystemExit(str(exc))
