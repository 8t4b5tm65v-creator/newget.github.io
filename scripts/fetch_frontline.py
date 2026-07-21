#!/usr/bin/env python3
"""
Standalone script replicating the Calibre Frontline recipe.
Fetches the current or specified issue of Frontline magazine and outputs an EPUB.
Usage: python fetch_frontline.py [issue] [output_path]
  issue: optional, Volume-Issue format e.g. "41-12" (defaults to current issue)
"""
import re
import sys
import html
import json
import base64 as _base64
from datetime import date, datetime
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from ebooklib import epub


HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/145.0.0.0 Safari/537.36'
    )
}

BASE_URL = 'https://frontline.thehindu.com'

CSS = '''
    body { font-family: Georgia, serif; margin: 1em 2em; }
    h1 { font-size: 1.4em; }
    .caption, figcaption { font-size: small; text-align: center; color: #555; }
    .environment, .publish-time, .author { font-size: small; color: #404040; }
    .subhead, .bold { font-weight: bold; }
    .question { font-weight: bold; }
    img { display: block; margin: 0 auto; max-width: 100%; }
    .italic { font-style: italic; color: #202020; }
'''

# Month name → number for parsing Frontline's issue label
_MONTHS = {
    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
    'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,
    'sep':9,'oct':10,'nov':11,'dec':12,
}


def absurl(url):
    if url.startswith('/'):
        return BASE_URL + url
    return url


def sanitize(content):
    """Strip control characters and ensure content is non-empty valid text."""
    if not content:
        return '<p><em>Content not available.</em></p>'
    content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', content)
    return content or '<p><em>Content not available.</em></p>'


def issue_label_to_date_slug(issue_label):
    """Convert Frontline's issue label to a YYYY-MM-DD slug for the filename.

    Frontline labels look like:
      "Volume 42, Issue 13 | June 20, 2025"
      "Volume 41, Issue 1 | January 6, 2024"
    We parse the date portion after the pipe.  Falls back to today's date
    in YYYY-MM-DD if parsing fails.
    """
    try:
        # Take the part after the pipe if present, else the whole string
        date_part = issue_label.split('|')[-1].strip()
        # Expect "Month D, YYYY" or "Month DD, YYYY"
        m = re.search(
            r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})',
            date_part
        )
        if m:
            month_name, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
            month_num = _MONTHS.get(month_name[:3])
            if month_num:
                return f'{year}-{month_num:02d}-{day:02d}'
    except Exception:
        pass
    # Fallback
    return date.today().strftime('%Y-%m-%d')


def make_xhtml(title, description, body, chapter_file):
    """Wrap content in a minimal valid XHTML document for ebooklib."""
    anchor = chapter_file.replace('.xhtml', '')
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head>'
        f'<title>{html.escape(title)}</title>'
        '<link rel="stylesheet" href="../style/main.css" type="text/css"/>'
        '</head>'
        '<body>'
        f'<h1>{html.escape(title)}</h1>'
        + (f'<p class="author">{html.escape(description)}</p>' if description else '')
        + '<hr/>'
        f'{body}'
        '<hr/>'
        '<p style="text-align:center;font-size:small;">'
        f'<a href="../article_index.xhtml#{anchor}">&#8592; Back to Index</a>'
        '</p>'
        '</body>'
        '</html>'
    )


def make_section_index_xhtml(feeds, issue_label):
    """Page 1 — high-level section index linking to anchors in the article index."""
    section_links = ''
    for section in feeds.keys():
        anchor = re.sub(r'\s+', '_', section)
        section_links += (
            f'<li><a href="article_index.xhtml#{html.escape(anchor)}">'
            f'{html.escape(section)}</a></li>'
        )
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head>'
        f'<title>Sections — Frontline, {html.escape(issue_label)}</title>'
        '<link rel="stylesheet" href="../style/main.css" type="text/css"/>'
        '<style>'
        'ul{list-style:none;padding:0;margin:0.5em 0;}'
        'li{margin:0.6em 0;}'
        'li a{text-decoration:none;color:#1a0dab;font-size:1.1em;}'
        '</style>'
        '</head>'
        '<body>'
        f'<h1>Frontline — {html.escape(issue_label)}</h1>'
        '<hr/>'
        '<ul>'
        f'{section_links}'
        '</ul>'
        '</body>'
        '</html>'
    )


def make_index_xhtml(feeds, issue_label, chapter_map):
    """Page 2 — granular article index with anchored section headings and teaser previews."""
    sections_html = ''
    for section, articles in feeds.items():
        section_anchor = re.sub(r'\s+', '_', section)
        previews = ''
        for article in articles:
            fname = chapter_map[article['url']]
            article_anchor = fname.replace('.xhtml', '')
            teaser = article.get('description', '').strip()
            sentences = re.split(r'(?<=[.!?])\s+', teaser)
            preview_text = ' '.join(sentences[:2])
            previews += (
                f'<li id="{article_anchor}">'
                f'<a href="{html.escape(fname)}">{html.escape(article["title"])}</a>'
                + (f'<br/><span style="font-size:small;color:#444;">{html.escape(preview_text)}</span>' if preview_text else '')
                + '</li>'
            )
        sections_html += (
            f'<h2 id="{html.escape(section_anchor)}">{html.escape(section)}</h2>'
            f'<ul>{previews}</ul>'
        )
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head>'
        f'<title>Index — Frontline, {html.escape(issue_label)}</title>'
        '<link rel="stylesheet" href="../style/main.css" type="text/css"/>'
        '<style>'
        'h2{font-size:1.1em;margin-top:1.2em;border-bottom:1px solid #ccc;padding-bottom:0.2em;}'
        'ul{list-style:none;padding:0;margin:0.3em 0;}'
        'li{margin:0.4em 0;}'
        'li a{text-decoration:none;color:#1a0dab;}'
        '</style>'
        '</head>'
        '<body>'
        f'<h1>Frontline — {html.escape(issue_label)}</h1>'
        '<p style="font-size:small;"><a href="section_index.xhtml">&#8592; Back to Sections</a></p>'
        '<hr/>'
        f'{sections_html}'
        '</body>'
        '</html>'
    )


def fetch_article_list(issue=None):
    """
    Fetch the Frontline issue index.
    issue: None = current issue; or Volume-Issue string e.g. "41-12"
    Returns (feeds dict, issue_label str, cover_url str or None)
    """
    if issue:
        url = f'{BASE_URL}/magazine/issue/vol{issue}/'
    else:
        url = f'{BASE_URL}/current-issue/'

    print(f'Fetching index: {url}')
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    cover_url = None
    issue_label = issue or date.today().strftime('%-d %b %Y')

    magazine_div = soup.find('div', attrs={'class': 'magazine'})
    if magazine_div:
        cover_img = magazine_div.find('img', attrs={'data-original': True})
        if cover_img:
            src = cover_img['data-original'].replace('SQUARE_80', 'FREE_615')
            cover_url = absurl(src)
            print(f'Cover image: {cover_url}')
        sub_text = magazine_div.find(class_='sub-text')
        if sub_text:
            issue_label = sub_text.get_text(strip=True)
            print(f'Issue: {issue_label}')
    else:
        print('Magazine div not found — trying alternate cover selector.')

    if not cover_url:
        print('Cover image not found on index page.')

    feeds = defaultdict(list)
    listing = soup.find(class_='current-issue-in-this-issue')
    if not listing:
        raise ValueError('Could not find article listing — Frontline page structure may have changed.')

    for div in listing.find_all('div', attrs={'class': 'content'}):
        title_el = div.find(class_='title')
        if not title_el:
            continue
        a = title_el.find('a')
        if not a:
            continue
        url = absurl(a.get('href', ''))
        title = a.get_text(strip=True)
        if not url or not title:
            continue

        section = 'Articles'
        cat = div.find(class_='label')
        if cat:
            section = cat.get_text(strip=True)

        description = ''
        auth = div.find(class_='author')
        sub = div.find(class_='sub-text')
        if auth:
            description = auth.get_text(strip=True)
        if sub:
            sub_text_str = sub.get_text(strip=True)
            description = f'{description} | {sub_text_str}' if description else sub_text_str

        feeds[section].append({
            'title':       title,
            'url':         url,
            'description': description,
        })

    total = sum(len(v) for v in feeds.values())
    print(f'Found {total} articles across {len(feeds)} sections')

    if not total:
        raise ValueError('No articles found — Frontline page structure may have changed.')

    return dict(feeds), issue_label, cover_url


def fetch_cover(cover_url):
    """Download the cover image, return (bytes, media_type, ext) or (None, None, None)."""
    try:
        resp = requests.get(cover_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        if content_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            content_type = 'image/jpeg'
        ext = content_type.split('/')[-1].replace('jpeg', 'jpg')
        return resp.content, content_type, ext
    except Exception as e:
        print(f'Warning: could not download cover image: {e}')
        return None, None, None


def _download_images(article):
    """Download all images in an article BeautifulSoup tag once.

    Returns a list of (img_tag, src_url, raw_bytes, content_type) for every
    image that was successfully fetched, with the img tag already cleaned of
    lazy-load attributes.  Images that fail to download are decomposed from
    the tree and excluded from the list.  Handles Frontline's 1x1_spacer.png
    lazy-load pattern by pulling the real URL from the preceding <source>.
    """
    results = []
    for img in list(article.find_all('img')):
        src = img.get('data-original') or img.get('src') or ''
        if not src:
            img.decompose()
            continue
        if src.endswith('1x1_spacer.png'):
            source = img.find_previous('source', srcset=True)
            if source:
                src = absurl(source.get('srcset', '').replace('_320', '_1200'))
                source.decompose()
            else:
                img.decompose()
                continue
        else:
            src = absurl(src)

        if 'placeholder' in src or 'spacer' in src or src.endswith('.gif'):
            img.decompose()
            continue

        try:
            resp = requests.get(src, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
            if content_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
                content_type = 'image/jpeg'
            for attr in ['data-original', 'data-src', 'srcset', 'height', 'width']:
                if img.has_attr(attr):
                    del img[attr]
            results.append((img, src, resp.content, content_type))
        except Exception as e:
            print(f'    Warning: could not download image {src}: {e}')
            img.decompose()

    for source in article.find_all('source'):
        source.decompose()

    return results


def fetch_article_content(url, book, chapter_id):
    """Fetch an article and return (epub_body_html, html_body_html).

    epub_body_html  — images rewritten to EPUB-internal paths (for ebooklib).
    html_body_html  — images inlined as base64 data URIs (for self-contained HTML).
    Both are derived from a single network fetch of each image.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        article = soup.find('div', class_=lambda c: c and 'article-section' in c.split())
        if not article:
            article = soup.find(class_='article-section')
        if not article:
            stub = '<p><em>Content not available.</em></p>'
            return stub, stub

        for cls in [
            'breadcrumb', 'comments-shares', 'share-page', 'article-video',
            'referpara', 'slide-mobile', 'title-patch', 'hide-mobile', 'related-stories'
        ]:
            for el in article.find_all(class_=cls):
                el.decompose()

        for cap in article.find_all(class_='caption'):
            cap.name = 'figcaption'

        # Download every image once; get back (img_tag, src, bytes, mime)
        image_data = _download_images(article)

        # ── Build EPUB version: rewrite src to EPUB-internal path ──
        img_counter = 0
        for img, src, raw, content_type in image_data:
            img_counter += 1
            ext = content_type.split('/')[-1].replace('jpeg', 'jpg')
            img_filename = f'images/ch{chapter_id:04d}_{img_counter:03d}.{ext}'
            book.add_item(epub.EpubItem(
                uid=f'img-{chapter_id}-{img_counter}',
                file_name=img_filename,
                media_type=content_type,
                content=raw,
            ))
            img['src'] = f'../{img_filename}'

        epub_body = sanitize(article.decode_contents())

        # ── Build HTML version: rewrite src to base64 data URI ──
        img_counter = 0
        for img, src, raw, content_type in image_data:
            img_counter += 1
            b64 = _base64.b64encode(raw).decode('ascii')
            img['src'] = f'data:{content_type};base64,{b64}'

        html_body = sanitize(article.decode_contents())

        return epub_body, html_body

    except Exception as e:
        stub = f'<p><em>Failed to fetch article: {html.escape(str(e))}</em></p>'
        return stub, stub


def build_epub(feeds, issue_label, cover_url, prefetched_bodies=None, prefetched_book=None):
    """Build an EpubBook from feeds.

    If prefetched_bodies (url->html) and prefetched_book (EpubBook with
    images already embedded) are supplied, article content is taken from
    there instead of re-fetching from the network.
    """
    book = prefetched_book if prefetched_book is not None else epub.EpubBook()
    slug = re.sub(r'[^\w-]', '_', issue_label)[:60]
    book.set_identifier(f'frontline-{slug}')
    book.set_title(f'Frontline — {issue_label}')
    book.set_language('en')
    book.add_author('Frontline / The Hindu Group')

    if cover_url:
        cover_bytes, media_type, ext = fetch_cover(cover_url)
        if cover_bytes:
            book.set_cover(f'cover.{ext}', cover_bytes)
            print(f'Cover set ({media_type}, {len(cover_bytes):,} bytes)')
        else:
            print('Warning: cover download failed.')
    else:
        print('Warning: no cover URL — EPUB will have no cover.')

    style = epub.EpubItem(
        uid='main-css',
        file_name='style/main.css',
        media_type='text/css',
        content=CSS,
    )
    book.add_item(style)

    chapter_map = {}
    chapter_id = 0
    for section, articles in feeds.items():
        for article in articles:
            chapter_id += 1
            chapter_map[article['url']] = f'ch_{chapter_id:04d}.xhtml'

    section_index_page = epub.EpubHtml(
        title='Sections',
        file_name='section_index.xhtml',
        lang='en',
    )
    section_index_page.content = make_section_index_xhtml(feeds, issue_label)
    section_index_page.add_item(style)
    book.add_item(section_index_page)

    article_index_page = epub.EpubHtml(
        title='Index',
        file_name='article_index.xhtml',
        lang='en',
    )
    article_index_page.content = make_index_xhtml(feeds, issue_label, chapter_map)
    article_index_page.add_item(style)
    book.add_item(article_index_page)

    spine = [section_index_page, article_index_page]
    toc = []
    chapter_id = 0

    for section, articles in feeds.items():
        section_chapters = []
        for article in articles:
            chapter_id += 1
            print(f'  [{section}] {article["title"]}')
            if prefetched_bodies is not None:
                body = prefetched_bodies.get(
                    article['url'],
                    '<p><em>Content not available.</em></p>'
                )
            else:
                body, _ = fetch_article_content(article['url'], book, chapter_id)

            ch = epub.EpubHtml(
                title=article['title'],
                file_name=f'ch_{chapter_id:04d}.xhtml',
                lang='en',
            )
            ch.content = make_xhtml(
                article['title'],
                article.get('description', ''),
                body,
                f'ch_{chapter_id:04d}.xhtml',
            )
            ch.add_item(style)
            book.add_item(ch)
            section_chapters.append(ch)
            spine.append(ch)

        toc.append((epub.Section(section), section_chapters))

    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    return book


def build_html_reader(feeds, issue_label, article_bodies, cover_image_b64, cover_mime):
    """Build a self-contained single-file HTML magazine reader.

    Layer 1 — table of contents: a scrollable page with the cover image up
    top, followed by articles grouped by section as tap-target cards.

    Layer 2 — article view: tapping a card slides in a clean reading pane
    from the right, over a blurred background. A back arrow returns to the
    table of contents.

    All article HTML is embedded as JSON in a <script> tag so the file is
    fully self-contained (no server, no JS imports).

    Returns the HTML as a string.
    """
    articles_js = []
    art_idx = 0
    section_data = []
    for section, articles in feeds.items():
        sec_articles = []
        for art in articles:
            body_html = article_bodies.get(art['url'], '<p><em>Content not available.</em></p>')
            body_html = re.sub(r'<script[\s\S]*?</script>', '', body_html, flags=re.IGNORECASE)
            articles_js.append({
                'id':      art_idx,
                'title':   art['title'],
                'section': section,
                'teaser':  art.get('description', ''),
                'url':     art['url'],
                'body':    body_html,
            })
            sec_articles.append(art_idx)
            art_idx += 1
        section_data.append({'name': section, 'ids': sec_articles})

    articles_json = json.dumps(articles_js, ensure_ascii=False)
    sections_json = json.dumps(section_data, ensure_ascii=False)

    cover_html = ''
    if cover_image_b64:
        cover_html = f'<img id="cover-img" src="data:{cover_mime};base64,{cover_image_b64}" alt="Cover"/>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>Frontline — {html.escape(issue_label)}</title>
<script>
(function(){{
  var saved = localStorage.getItem('ng-theme');
  var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  if (saved === 'dark' || (!saved && prefersDark)) document.documentElement.classList.add('dark');
}})();
</script>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --ink:        #1a1a18;
  --ink-muted:  #4a4a45;
  --ink-faint:  #8a8a82;
  --paper:      #f8f6f0;
  --paper-warm: #f0ede4;
  --rule:       #c8c4b8;
  --teal:       #1a7a6e;
  --font-head:  'Georgia', 'Times New Roman', serif;
  --font-body:  'Georgia', 'Times New Roman', serif;
  --font-ui:    system-ui, -apple-system, sans-serif;
}}

html.dark {{
  --ink:        #e8e4dc;
  --ink-muted:  #a8a49c;
  --ink-faint:  #6a6660;
  --paper:      #181816;
  --paper-warm: #211f1c;
  --rule:       #38352f;
  --teal:       #3ab5a4;
}}

html, body {{
  height: 100%; width: 100%; overflow: hidden;
  background: var(--paper); color: var(--ink);
  font-family: var(--font-body);
  -webkit-font-smoothing: antialiased;
}}

/* ── Masthead ── */
#masthead {{
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  background: var(--ink); color: var(--paper);
  padding: 0 1rem;
  display: flex; align-items: center; justify-content: space-between;
  height: 52px;
  user-select: none;
}}
#masthead .nameplate {{
  font-family: var(--font-head);
  font-size: 1.3rem; font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1;
}}
#masthead .edition-date {{
  font-family: var(--font-ui);
  font-size: 0.7rem; color: #aaa;
  text-align: right; line-height: 1.35;
}}
#masthead .back-btn {{
  display: none; align-items: center; gap: 6px;
  background: none; border: none; color: var(--paper);
  font-family: var(--font-ui); font-size: 0.8rem;
  cursor: pointer; padding: 8px 0; min-width: 60px;
}}
#masthead .back-btn svg {{ flex-shrink: 0; }}

/* ── Theme toggle ── */
#theme-toggle {{
  background: none; border: none;
  color: var(--paper); cursor: pointer;
  padding: 6px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  opacity: 0.75; transition: opacity 0.15s;
  flex-shrink: 0;
}}
#theme-toggle:hover {{ opacity: 1; }}
#theme-toggle svg {{ display: block; }}

/* ── Table of contents ── */
#toc-viewport {{
  position: fixed;
  top: 52px; left: 0; right: 0; bottom: 0;
  overflow-y: auto; overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
}}
#toc-viewport::-webkit-scrollbar {{ width: 3px; }}
#toc-viewport::-webkit-scrollbar-thumb {{ background: var(--rule); }}

#cover-wrap {{
  padding: 1.5rem 1rem 1rem;
  text-align: center;
  border-bottom: 3px double var(--ink);
}}
#cover-img {{
  max-width: 220px; width: 60%; height: auto;
  border-radius: 4px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.25);
  margin: 0 auto 0.9rem;
  display: block;
}}
#issue-label {{
  font-family: var(--font-ui); font-size: 0.75rem;
  font-weight: 600; letter-spacing: 0.06em;
  color: var(--ink-muted);
}}

.sec-header {{
  padding: 1rem 1rem 0.5rem;
  margin-top: 0.3rem;
}}
.sec-header h2 {{
  font-family: var(--font-head);
  font-size: 1.0rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--teal);
  border-bottom: 2px solid var(--teal);
  display: inline-block;
  padding-bottom: 0.2rem;
}}

.article-row {{
  display: grid;
  grid-template-columns: 1fr;
  border-bottom: 0.5px solid var(--rule);
  padding: 0.75rem 1rem;
  cursor: pointer;
  transition: background 0.12s;
  gap: 0.25rem;
}}
.article-row:active {{ background: var(--paper-warm); }}
@media (hover: hover) {{
  .article-row:hover {{ background: var(--paper-warm); }}
}}

.art-headline {{
  font-family: var(--font-head);
  font-size: 1.02rem; font-weight: 700;
  line-height: 1.3; color: var(--ink);
}}
.art-teaser {{
  font-family: var(--font-body); font-size: 0.8rem;
  color: var(--ink-muted); line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.art-read-cue {{
  font-family: var(--font-ui); font-size: 0.68rem;
  color: var(--teal); font-weight: 600;
  letter-spacing: 0.03em;
  align-self: end;
  text-align: right;
}}

/* ── Article pane ── */
#article-pane {{
  position: fixed;
  top: 52px; left: 0; right: 0; bottom: 0;
  background: var(--paper);
  overflow-y: auto; overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  transform: translateX(100%);
  transition: transform 0.32s cubic-bezier(0.25, 0.46, 0.45, 0.94);
  will-change: transform;
  z-index: 50;
  padding: 1.5rem 1.1rem 3rem;
  max-width: 780px;
  margin: 0 auto;
}}
#article-pane::-webkit-scrollbar {{ width: 3px; }}
#article-pane::-webkit-scrollbar-thumb {{ background: var(--rule); }}

#article-pane .art-pane-section {{
  font-family: var(--font-ui); font-size: 0.68rem;
  font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--teal);
  margin-bottom: 0.5rem;
  display: flex; align-items: center; gap: 0.5rem;
}}
#article-pane .art-pane-section::after {{
  content: ''; flex: 1; height: 1px; background: var(--rule);
}}
#article-pane h1 {{
  font-family: var(--font-head);
  font-size: clamp(1.3rem, 4vw, 1.8rem);
  font-weight: 700; line-height: 1.25;
  color: var(--ink); margin-bottom: 0.75rem;
}}
#article-pane .art-pane-meta {{
  font-family: var(--font-ui); font-size: 0.72rem;
  color: var(--ink-faint); margin-bottom: 1rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--rule);
  display: flex; gap: 1rem; flex-wrap: wrap;
}}
#article-pane .art-pane-body {{
  font-size: 1rem; line-height: 1.75; color: var(--ink);
}}
#article-pane .art-pane-body p {{ margin-bottom: 0.9rem; }}
#article-pane .art-pane-body h2,
#article-pane .art-pane-body h3 {{
  font-family: var(--font-head);
  font-weight: 700; margin: 1.25rem 0 0.4rem;
  color: var(--ink);
}}
#article-pane .art-pane-body img {{
  max-width: 100%; height: auto;
  display: block; margin: 1rem 0;
}}
#article-pane .art-pane-body figcaption {{
  font-size: 0.75rem; color: var(--ink-faint);
  margin-top: -0.5rem; margin-bottom: 1rem;
  font-style: italic;
}}
#article-pane .art-source-link {{
  margin-top: 1.5rem;
  padding-top: 1rem;
  border-top: 1px solid var(--rule);
  font-family: var(--font-ui); font-size: 0.75rem;
  color: var(--ink-faint);
}}
#article-pane .art-source-link a {{ color: var(--teal); }}

/* ── Background blur overlay ── */
#blur-overlay {{
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  z-index: 49;
  backdrop-filter: blur(6px) brightness(0.85);
  -webkit-backdrop-filter: blur(6px) brightness(0.85);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.32s cubic-bezier(0.25, 0.46, 0.45, 0.94);
}}

@media (min-width: 700px) {{
  #article-pane {{
    left: 0; right: 0;
    padding: 2rem 2.5rem 4rem;
  }}
  .article-row {{ padding: 0.9rem 1.5rem; }}
  .sec-header {{ padding: 1rem 1.5rem 0.5rem; }}
  .art-headline {{ font-size: 1.1rem; }}
  #masthead .nameplate {{ font-size: 1.5rem; }}
}}
</style>
</head>
<body>

<header id="masthead">
  <button class="back-btn" id="back-btn" onclick="closeArticle()" aria-label="Back to contents">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="15 18 9 12 15 6"/>
    </svg>
    Back
  </button>
  <div class="nameplate">Frontline</div>
  <div style="display:flex;align-items:center;gap:4px;">
    <button id="theme-toggle" aria-label="Toggle dark mode" onclick="toggleTheme()">
      <svg id="icon-sun" width="18" height="18" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
           style="display:none">
        <circle cx="12" cy="12" r="5"/>
        <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
        <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
        <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
        <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
      </svg>
      <svg id="icon-moon" width="18" height="18" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
    </button>
    <div class="edition-date">Magazine<br/>{html.escape(issue_label)}</div>
  </div>
</header>

<div id="toc-viewport">
  <div id="cover-wrap">
    {cover_html}
    <div id="issue-label">{html.escape(issue_label)}</div>
  </div>
  <div id="sections"></div>
</div>

<div id="blur-overlay"></div>

<div id="article-pane" aria-label="Article reader">
  <div class="art-pane-section" id="pane-section"></div>
  <h1 id="pane-title"></h1>
  <div class="art-pane-meta" id="pane-meta"></div>
  <div class="art-pane-body" id="pane-body"></div>
  <div class="art-source-link" id="pane-source"></div>
</div>

<script>
const ARTICLES = {articles_json};
const SECTIONS = {sections_json};

let articlePaneOpen = false;
const sectionsEl = document.getElementById('sections');
const pane = document.getElementById('article-pane');
const backBtn = document.getElementById('back-btn');

function syncThemeIcon() {{
  const dark = document.documentElement.classList.contains('dark');
  document.getElementById('icon-sun').style.display  = dark ? 'block' : 'none';
  document.getElementById('icon-moon').style.display = dark ? 'none'  : 'block';
}}

function toggleTheme() {{
  const dark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('ng-theme', dark ? 'dark' : 'light');
  syncThemeIcon();
}}

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function buildUI() {{
  SECTIONS.forEach(sec => {{
    const header = document.createElement('div');
    header.className = 'sec-header';
    header.innerHTML = `<h2>${{sec.name}}</h2>`;
    sectionsEl.appendChild(header);

    sec.ids.forEach(aid => {{
      const art = ARTICLES[aid];
      const row = document.createElement('div');
      row.className = 'article-row';
      row.setAttribute('role', 'button');
      row.setAttribute('tabindex', '0');
      row.setAttribute('aria-label', art.title);

      row.innerHTML = `
        <div class="art-headline">${{escHtml(art.title)}}</div>
        ${{art.teaser ? `<div class="art-teaser">${{escHtml(art.teaser)}}</div>` : ''}}
        <div class="art-read-cue">Read &rsaquo;</div>
      `;
      row.onclick = () => openArticle(aid);
      row.onkeydown = e => {{ if (e.key === 'Enter' || e.key === ' ') openArticle(aid); }};
      sectionsEl.appendChild(row);
    }});
  }});
}}

function openArticle(aid) {{
  const art = ARTICLES[aid];
  document.getElementById('pane-section').textContent = art.section;
  document.getElementById('pane-title').textContent = art.title;
  document.getElementById('pane-meta').innerHTML = `<span>${{art.section}}</span>`;

  document.getElementById('pane-body').innerHTML = art.body ||
    '<p><em>Content not available.</em></p>';

  const srcEl = document.getElementById('pane-source');
  srcEl.innerHTML = art.url
    ? `Read on frontline.thehindu.com: <a href="${{escHtml(art.url)}}" target="_blank" rel="noopener">${{escHtml(art.url)}}</a>`
    : '';

  pane.scrollTop = 0;
  pane.style.transform = 'translateX(0)';
  document.getElementById('blur-overlay').style.opacity = '1';
  articlePaneOpen = true;
  backBtn.style.display = 'flex';
  history.pushState({{ article: aid }}, '');
}}

function closeArticle() {{
  pane.style.transform = 'translateX(100%)';
  document.getElementById('blur-overlay').style.opacity = '0';
  articlePaneOpen = false;
  backBtn.style.display = 'none';
}}

window.addEventListener('popstate', () => {{
  if (articlePaneOpen) closeArticle();
}});

document.addEventListener('keydown', e => {{
  if (articlePaneOpen && e.key === 'Escape') closeArticle();
}});

buildUI();
syncThemeIcon();
</script>
</body>
</html>'''


if __name__ == '__main__':
    issue = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None

    feeds, issue_label, cover_url = fetch_article_list(issue)

    # Derive a clean YYYY-MM-DD slug from the issue label (e.g. "June 20, 2025" → "2025-06-20")
    date_slug = issue_label_to_date_slug(issue_label)
    dated_path = f'frontline-{date_slug}.epub'

    # ── Fetch all article bodies once; share between EPUB and HTML reader ──
    # fetch_article_content returns (epub_body, html_body):
    #   epub_body  — images as EPUB-internal paths  (for ebooklib)
    #   html_body  — images as base64 data URIs     (for self-contained HTML)
    print('\nFetching article content...')
    temp_book = epub.EpubBook()
    epub_bodies = {}   # url -> epub body HTML
    html_bodies = {}   # url -> html body HTML (images as data URIs)
    chapter_id = 0
    for section, articles in feeds.items():
        for art in articles:
            chapter_id += 1
            epub_body, html_body = fetch_article_content(art['url'], temp_book, chapter_id)
            epub_bodies[art['url']] = epub_body
            html_bodies[art['url']] = html_body

    # ── Build EPUB ──
    book = build_epub(feeds, issue_label, cover_url,
                       prefetched_bodies=epub_bodies,
                       prefetched_book=temp_book)
    epub.write_epub(dated_path, book)
    print(f'\nSaved EPUB: {dated_path} (issue: {issue_label})')

    # ── Build HTML reader ──
    cover_b64, cover_mime = None, 'image/jpeg'
    if cover_url:
        cover_bytes, media_type, _ext = fetch_cover(cover_url)
        if cover_bytes:
            cover_b64 = _base64.b64encode(cover_bytes).decode('ascii')
            cover_mime = media_type

    html_path = dated_path.replace('.epub', '.html')
    html_content = build_html_reader(feeds, issue_label, html_bodies, cover_b64, cover_mime)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f'Saved HTML reader: {html_path}')
