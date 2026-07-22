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


def issue_label_to_slug(issue_label):
    """Convert Frontline's issue label to a canonical slug for filenames.

    Frontline labels look like:
      "Volume 42, Issue 13 | June 20, 2025"
      "Volume 41, Issue 1 | January 6, 2024"

    Returns: "2025-06-20-vol42-iss13"
    This uniquely identifies an issue by both its publish date and its
    volume/issue number, preventing duplicate downloads on re-runs.
    Falls back to "YYYY-MM-DD-vol0-iss0" using today's date if parsing fails.
    """
    try:
        vol_m   = re.search(r'[Vv]olume\s+(\d+)', issue_label)
        iss_m   = re.search(r'[Ii]ssue\s+(\d+)', issue_label)
        date_part = issue_label.split('|')[-1].strip()
        date_m  = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', date_part)

        vol = int(vol_m.group(1)) if vol_m else 0
        iss = int(iss_m.group(1)) if iss_m else 0

        if date_m:
            month_name = date_m.group(1).lower()
            day   = int(date_m.group(2))
            year  = int(date_m.group(3))
            month = _MONTHS.get(month_name[:3], 0)
            if month:
                return f'{year}-{month:02d}-{day:02d}-vol{vol}-iss{iss}'
    except Exception:
        pass
    return f'{date.today().strftime("%Y-%m-%d")}-vol0-iss0'


def slug_to_vol_iss(slug):
    """Extract (vol, iss) integers from a slug like "2025-06-20-vol42-iss13"."""
    m = re.search(r'vol(\d+)-iss(\d+)', slug)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


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
    """Download all images once. Returns list of (img_tag, src, raw_bytes, content_type).
    Handles Frontline's 1x1 spacer pattern. Failed images are decomposed."""
    results = []
    for img in list(article.find_all('img')):
        src = img.get('data-original') or img.get('src') or ''
        if not src:
            img.decompose()
            continue
        if src.endswith('1x1_spacer.png'):
            source = img.find_previous('source', srcset=True)
            img.decompose()
            if source:
                src = absurl(source.get('srcset', '').replace('_320', '_1200'))
                source.decompose()
            else:
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
    """Fetch a single article. Returns (epub_body, html_body).

    epub_body  — images as EPUB-internal paths (for ebooklib).
    html_body  — images as base64 data URIs (for self-contained HTML reader).
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

        image_data = _download_images(article)

        # ── EPUB version: local paths ──
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

        # ── HTML version: base64 data URIs ──
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


def build_epub(feeds, issue_label, cover_url,
               prefetched_bodies=None, prefetched_book=None):
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


def build_html_reader(feeds, issue_label, article_bodies,
                      cover_image_b64=None, cover_mime='image/jpeg'):
    """Build a self-contained single-file HTML reader for a Frontline issue.

    Layout: fixed header with issue title + dark/light toggle. Below it, a
    scrollable table of contents grouped by section — each article is a card
    showing title and description. Tapping a card slides in a full-screen
    reading pane from the right. A blur overlay darkens the TOC behind it.

    All article HTML and the cover image are embedded so the file is fully
    self-contained.
    """
    cover_html = ''
    if cover_image_b64:
        cover_html = (
            f'<div id="cover-wrap">'
            f'<img id="cover-img" src="data:{cover_mime};base64,{cover_image_b64}" '
            f'alt="Cover"/></div>'
        )

    # Build article list for JS
    articles_js = []
    art_idx = 0
    section_data = []
    for section, articles in feeds.items():
        sec_ids = []
        for art in articles:
            body = article_bodies.get(art['url'], '<p><em>Content not available.</em></p>')
            body = re.sub(r'<script[\s\S]*?</script>', '', body, flags=re.IGNORECASE)
            articles_js.append({
                'id':          art_idx,
                'title':       art['title'],
                'section':     section,
                'description': art.get('description', ''),
                'body':        body,
                'url':         art['url'],
            })
            sec_ids.append(art_idx)
            art_idx += 1
        section_data.append({'name': section, 'ids': sec_ids})

    articles_json = json.dumps(articles_js, ensure_ascii=False)
    sections_json = json.dumps(section_data, ensure_ascii=False)
    issue_esc = html.escape(issue_label)

    return f'''<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>Frontline — {issue_esc}</title>
<script>
(function(){{
  var s=localStorage.getItem('ng-theme');
  var pd=window.matchMedia('(prefers-color-scheme: dark)').matches;
  if(s==='light') document.documentElement.classList.remove('dark');
  else if(!s && !pd) document.documentElement.classList.remove('dark');
}})();
</script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}

:root{{
  --bg:       #111110;
  --surface:  #1c1b19;
  --surface-h:#232220;
  --border:   #2e2c28;
  --ink:      #e8e4dc;
  --muted:    #7a7670;
  --teal:     #1a7a6e;
  --teal-s:   #3ab5a4;
  --fh: 'Georgia','Times New Roman',serif;
  --fu: system-ui,-apple-system,sans-serif;
}}
html:not(.dark){{
  --bg:       #f5f3ef;
  --surface:  #ffffff;
  --surface-h:#f9f7f4;
  --border:   #d8d4cc;
  --ink:      #1a1a18;
  --muted:    #918d85;
  --teal:     #1a7a6e;
  --teal-s:   #0e6358;
}}

html,body{{height:100%;width:100%;overflow:hidden;background:var(--bg);color:var(--ink);font-family:var(--fh);-webkit-font-smoothing:antialiased;transition:background 0.25s,color 0.25s}}

/* ── Header ── */
#hdr{{position:fixed;top:0;left:0;right:0;z-index:100;background:var(--surface);border-bottom:1px solid var(--border);height:54px;display:flex;align-items:center;justify-content:space-between;padding:0 1rem;transition:background 0.25s,border-color 0.25s}}
.hdr-left{{display:flex;align-items:center;gap:8px;min-width:0}}
#back-btn{{display:none;align-items:center;gap:5px;background:none;border:none;color:var(--muted);font-family:var(--fu);font-size:0.75rem;cursor:pointer;padding:6px 0;flex-shrink:0}}
#back-btn svg{{flex-shrink:0}}
.masthead-title{{font-family:var(--fh);font-size:1rem;font-weight:700;color:var(--teal);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.hdr-right{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.toggle-wrap{{display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}}
.theme-btn{{width:34px;height:18px;border-radius:9px;border:1.5px solid var(--border);background:var(--surface);cursor:pointer;position:relative;transition:background 0.25s,border-color 0.25s;padding:0;flex-shrink:0}}
.theme-btn::after{{content:'';position:absolute;top:2px;left:2px;width:10px;height:10px;border-radius:50%;background:var(--muted);transition:transform 0.25s,background 0.25s}}
html.dark .theme-btn::after{{transform:translateX(16px);background:var(--ink)}}
.toggle-txt{{font-family:var(--fu);font-size:0.55rem;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);transition:color 0.25s}}

/* ── TOC viewport ── */
#toc-view{{position:fixed;top:54px;left:0;right:0;bottom:0;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch}}
#toc-view::-webkit-scrollbar{{width:3px}}
#toc-view::-webkit-scrollbar-thumb{{background:var(--border)}}

/* ── Cover ── */
#cover-wrap{{padding:1.2rem 1rem 0.5rem;display:flex;justify-content:center}}
#cover-img{{max-width:200px;width:100%;border-radius:4px;border:1px solid var(--border)}}

/* ── Issue label ── */
.issue-label{{padding:0.75rem 1rem 1rem;text-align:center}}
.issue-eyebrow{{font-family:var(--fu);font-size:0.62rem;letter-spacing:0.14em;text-transform:uppercase;color:var(--teal);margin-bottom:0.25rem}}
.issue-name{{font-family:var(--fh);font-size:0.95rem;font-weight:700;color:var(--ink);line-height:1.3}}

/* ── Section groups ── */
.sec-group{{border-top:1px solid var(--border);padding-top:0.1rem;margin-bottom:0.5rem;transition:border-color 0.25s}}
.sec-label{{font-family:var(--fu);font-size:0.6rem;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--teal);padding:0.6rem 1rem 0.3rem}}

/* ── Article cards ── */
.art-card{{padding:0.75rem 1rem;border-bottom:0.5px solid var(--border);cursor:pointer;transition:background 0.12s;position:relative}}
.art-card:active,.art-card:hover{{background:var(--surface-h)}}
.art-title{{font-family:var(--fh);font-size:0.95rem;font-weight:700;color:var(--ink);line-height:1.3;margin-bottom:0.25rem;padding-right:1rem}}
.art-desc{{font-family:var(--fu);font-size:0.72rem;color:var(--muted);line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.art-cue{{position:absolute;right:1rem;top:50%;transform:translateY(-50%);color:var(--border);font-size:1rem;transition:color 0.15s}}
.art-card:hover .art-cue{{color:var(--muted)}}

/* ── Blur overlay ── */
#blur-overlay{{position:fixed;top:0;left:0;right:0;bottom:0;z-index:49;backdrop-filter:blur(6px) brightness(0.85);-webkit-backdrop-filter:blur(6px) brightness(0.85);opacity:0;pointer-events:none;transition:opacity 0.32s cubic-bezier(.25,.46,.45,.94)}}

/* ── Article pane ── */
@keyframes fl-to-pip{{0%{{opacity:1;transform:translateX(-50%) scale(1)}}100%{{opacity:0;transform:translateX(-50%) translate(calc(50vw - 150px),calc(50vh)) scale(0.18);border-radius:10px}}}}
@keyframes fl-from-pip{{0%{{opacity:0;transform:translateX(-50%) translate(calc(50vw - 150px),calc(50vh)) scale(0.18);border-radius:10px}}100%{{opacity:1;transform:translateX(-50%) scale(1)}}}}
#art-pane{{position:fixed;top:60px;left:50%;bottom:12px;transform:translateX(-50%) scale(0.96);width:min(900px,calc(100vw - 1rem));background:var(--bg);border-radius:12px;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;opacity:0;pointer-events:none;transform-origin:center bottom;z-index:50;transition:opacity 0.28s ease,transform 0.28s cubic-bezier(0.25,0.46,0.45,0.94),background 0.25s}}
#art-pane::-webkit-scrollbar{{width:3px}}
#art-pane::-webkit-scrollbar-thumb{{background:var(--border)}}#art-pane.open{{opacity:1;pointer-events:auto;animation:none;transform:translateX(-50%) scale(1)}}#art-pane.flying-out{{pointer-events:none;animation:fl-to-pip 0.75s cubic-bezier(0.4,0,0.2,1) forwards}}#art-pane.flying-in{{pointer-events:none;animation:fl-from-pip 0.4s cubic-bezier(0.2,0,0,1) forwards}}
#pip-card{{position:fixed;bottom:14px;right:14px;width:200px;background:var(--surface);border-radius:10px;border:1px solid var(--border);overflow:hidden;opacity:0;pointer-events:none;transform:scale(0.88) translateY(8px);transform-origin:bottom right;transition:opacity 0.3s ease 0.52s,transform 0.35s cubic-bezier(0.34,1.4,0.64,1) 0.52s;z-index:103;cursor:pointer;box-shadow:0 4px 24px rgba(0,0,0,0.18)}}
#pip-card.open{{opacity:1;pointer-events:auto;transform:scale(1) translateY(0)}}
#pip-card:hover{{border-color:var(--muted)}}
.pip-hd{{background:var(--ink);padding:7px 10px;display:flex;align-items:center;justify-content:space-between}}
.pip-lg{{font-family:var(--fh);font-size:0.68rem;font-weight:700;color:var(--teal)}}
.pip-x{{background:none;border:none;color:#888;cursor:pointer;padding:2px;display:flex;align-items:center;border-radius:3px;transition:color 0.15s}}
.pip-x:hover{{color:var(--ink)}}
.pip-bd{{padding:8px 10px 10px}}
.pip-sec{{font-family:var(--fu);font-size:0.58rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:var(--teal);margin-bottom:4px}}
.pip-ttl{{font-family:var(--fh);font-size:0.72rem;font-weight:700;line-height:1.3;color:var(--ink);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:4px}}
.pip-hint{{font-family:var(--fu);font-size:0.58rem;color:var(--muted)}}
.pane-inner{{max-width:680px;margin:0 auto;padding:1.5rem 1.1rem 3rem}}
.pane-eyebrow{{font-family:var(--fu);font-size:0.62rem;font-weight:600;text-transform:uppercase;letter-spacing:0.1em;color:var(--teal);margin-bottom:0.5rem;display:flex;align-items:center;gap:0.5rem}}
.pane-eyebrow::after{{content:'';flex:1;height:1px;background:var(--border)}}
#pane-title{{font-family:var(--fh);font-size:clamp(1.25rem,4vw,1.75rem);font-weight:700;line-height:1.25;color:var(--ink);margin-bottom:0.6rem}}
.pane-desc{{font-family:var(--fu);font-size:0.72rem;color:var(--muted);padding-bottom:0.75rem;border-bottom:1px solid var(--border);margin-bottom:1rem;line-height:1.5}}
.pane-body{{font-size:1rem;line-height:1.8;color:var(--ink)}}
.pane-body p{{margin-bottom:0.9rem}}
.pane-body h2,.pane-body h3{{font-family:var(--fh);font-weight:700;margin:1.4rem 0 0.4rem;color:var(--ink)}}
.pane-body img{{max-width:100%;height:auto;display:block;margin:1rem auto}}
.pane-body figcaption{{font-size:0.75rem;color:var(--muted);margin-top:-0.5rem;margin-bottom:1rem;font-style:italic;text-align:center}}
.pane-src{{margin-top:1.5rem;padding-top:1rem;border-top:1px solid var(--border);font-family:var(--fu);font-size:0.7rem;color:var(--muted)}}
.pane-src a{{color:var(--teal)}}
.print-btn{{display:inline-flex;align-items:center;gap:6px;margin-top:1rem;background:none;border:1px solid var(--border);border-radius:6px;padding:6px 14px;font-family:var(--fu);font-size:0.75rem;color:var(--muted);cursor:pointer;transition:border-color 0.15s,color 0.15s}}.print-btn:hover{{border-color:var(--muted);color:var(--ink)}}.print-btn svg{{flex-shrink:0}}
@media print{{body,html{{overflow:visible!important;background:#fff!important}}#hdr,#toc-view,#blur-overlay,#pip-card,.print-btn{{display:none!important}}#art-pane{{position:static!important;transform:none!important;opacity:1!important;pointer-events:auto!important;width:100%!important;max-width:100%!important;height:auto!important;overflow:visible!important;border-radius:0!important;padding:2rem!important;background:#fff!important;color:#000!important;box-shadow:none!important}}.pane-eyebrow,#pane-title,.pane-desc,.pane-body,.pane-src{{display:block!important;color:#000!important}}.pane-body img{{max-width:100%!important}}a{{color:#000!important;text-decoration:none!important}}}}

@media(min-width:700px){{
  .pane-inner{{padding:2rem 2.5rem 4rem}}
  #cover-img{{max-width:240px}}
}}
</style>
</head>
<body>

<header id="hdr">
  <div class="hdr-left">
    <button id="back-btn" onclick="closeArticle(event)" aria-label="Back to contents">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
      Back
    </button>
    <span class="masthead-title">Frontline</span>
  </div>
  <div class="hdr-right">
    <label class="toggle-wrap" aria-label="Toggle dark mode">
      <button class="theme-btn" onclick="toggleTheme()" aria-label="Toggle theme"></button>
      <span class="toggle-txt" id="toggle-txt">Dark</span>
    </label>
  </div>
</header>

<div id="toc-view">
  {cover_html}
  <div class="issue-label">
    <div class="issue-eyebrow">Frontline Magazine</div>
    <div class="issue-name">{issue_esc}</div>
  </div>
  <div id="toc-body"></div>
</div>

<div id="blur-overlay" onclick="pipArticle()"></div>

<div id="pip-card" onclick="restoreArticle()">
  <div class="pip-hd"><span class="pip-lg">Frontline</span><button class="pip-x" onclick="closeArticle(event)" aria-label="Close"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button></div>
  <div class="pip-bd"><div class="pip-sec" id="pip-sec"></div><div class="pip-ttl" id="pip-ttl"></div><div class="pip-hint">Tap to continue reading</div></div>
</div>

<div id="art-pane" onclick="event.stopPropagation()">
  <div class="pane-inner">
    <div class="pane-eyebrow" id="pane-eyebrow"></div>
    <div id="pane-title"></div>
    <div class="pane-desc" id="pane-desc"></div>
    <div class="pane-body" id="pane-body"></div>
    <div class="pane-src" id="pane-src"></div>
    <button class="print-btn" onclick="window.print()" aria-label="Save as PDF">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
      Save as PDF
    </button>
  </div>
</div>

<script>
const ARTICLES={articles_json};
const SECTIONS={sections_json};
let paneOpen=false;
const pane=document.getElementById('art-pane');
const backBtn=document.getElementById('back-btn');
const overlay=document.getElementById('blur-overlay');

function escH(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}}

function buildTOC(){{
  const toc=document.getElementById('toc-body');
  SECTIONS.forEach(sec=>{{
    const grp=document.createElement('div');
    grp.className='sec-group';
    const lbl=document.createElement('div');
    lbl.className='sec-label';
    lbl.textContent=sec.name;
    grp.appendChild(lbl);
    sec.ids.forEach(aid=>{{
      const art=ARTICLES[aid];
      const card=document.createElement('div');
      card.className='art-card';
      card.setAttribute('role','button');
      card.setAttribute('tabindex','0');
      card.innerHTML=`<div class="art-title">${{escH(art.title)}}</div>`
        +(art.description?`<div class="art-desc">${{escH(art.description)}}</div>`:'')
        +`<span class="art-cue">›</span>`;
      card.onclick=()=>openArticle(aid);
      card.onkeydown=e=>{{if(e.key==='Enter'||e.key===' ')openArticle(aid);}};
      grp.appendChild(card);
    }});
    toc.appendChild(grp);
  }});
}}

function openArticle(aid){{
  const art=ARTICLES[aid];
  document.getElementById('pane-eyebrow').textContent=art.section;
  document.getElementById('pane-title').textContent=art.title;
  const desc=document.getElementById('pane-desc');
  desc.textContent=art.description||'';
  desc.style.display=art.description?'':'none';
  document.getElementById('pane-body').innerHTML=art.body||'<p><em>Content not available.</em></p>';
  const src=document.getElementById('pane-src');
  src.innerHTML=art.url?`Read on frontline.thehindu.com: <a href="${{escH(art.url)}}" target="_blank" rel="noopener">${{escH(art.url)}}</a>`:'';
  pane.scrollTop=0;
  pane.classList.remove('flying-out','flying-in');
  pane.classList.add('open');
  document.getElementById('pip-card').classList.remove('open');
  overlay.style.opacity='1';
  overlay.style.pointerEvents='auto';
  paneOpen=true;
  backBtn.style.display='flex';
  history.pushState({{article:aid}},'');
}}

function pipArticle(){{
  if(!paneOpen)return;
  document.getElementById('pip-sec').textContent=document.getElementById('pane-eyebrow').textContent;
  document.getElementById('pip-ttl').textContent=document.getElementById('pane-title').textContent;
  pane.classList.remove('open');
  pane.classList.add('flying-out');
  pane.addEventListener('animationend',()=>{{pane.classList.remove('flying-out');}},{{once:true}});
  overlay.style.opacity='0';
  overlay.style.pointerEvents='none';
  document.getElementById('pip-card').classList.add('open');
  paneOpen=false;
  backBtn.style.display='none';
}}

function restoreArticle(){{
  document.getElementById('pip-card').classList.remove('open');
  pane.classList.remove('open');
  pane.classList.add('flying-in');
  pane.addEventListener('animationend',()=>{{pane.classList.remove('flying-in');pane.classList.add('open');}},{{once:true}});
  setTimeout(()=>{{overlay.style.opacity='1';overlay.style.pointerEvents='auto';}},100);
  paneOpen=true;
  backBtn.style.display='flex';
}}

function closeArticle(e){{
  if(e&&e.stopPropagation)e.stopPropagation();
  pane.classList.remove('open','flying-out','flying-in');
  document.getElementById('pip-card').classList.remove('open');
  overlay.style.opacity='0';
  overlay.style.pointerEvents='none';
  paneOpen=false;
  backBtn.style.display='none';
}}

window.addEventListener('popstate',()=>{{if(paneOpen)pipArticle();else closeArticle();}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape'){{if(paneOpen)pipArticle();else closeArticle();return;}}}});

function syncTheme(){{
  document.getElementById('toggle-txt').textContent=
    document.documentElement.classList.contains('dark')?'Dark':'Light';
}}
function toggleTheme(){{
  const dark=document.documentElement.classList.toggle('dark');
  localStorage.setItem('ng-theme',dark?'dark':'light');
  syncTheme();
}}

buildTOC();
syncTheme();
</script>
</body>
</html>'''


if __name__ == '__main__':
    issue = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None

    feeds, issue_label, cover_url = fetch_article_list(issue)
    slug = issue_label_to_slug(issue_label)
    vol, iss = slug_to_vol_iss(slug)

    print(f'\nIssue label : {issue_label}')
    print(f'Slug        : {slug}')
    print(f'Volume/Issue: vol{vol} iss{iss}')

    # ── Duplicate detection ──────────────────────────────────────────────
    # The workflow passes EXISTING_SLUG (the vol/iss of the last deployed
    # issue) via an environment variable. If it matches, we exit cleanly
    # so the workflow skips the deploy step entirely.
    import os
    existing_slug = os.environ.get('EXISTING_SLUG', '')
    if existing_slug:
        ex_vol, ex_iss = slug_to_vol_iss(existing_slug)
        if ex_vol is not None and ex_vol == vol and ex_iss == iss:
            print(f'\nSame issue already published ({existing_slug}). Nothing to do.')
            # Signal to workflow that this is a duplicate
            with open('DUPLICATE', 'w') as f:
                f.write(existing_slug)
            sys.exit(0)

    # ── Fetch all article bodies once ────────────────────────────────────
    print('\nFetching article content...')
    temp_book = epub.EpubBook()
    epub_bodies = {}
    html_bodies = {}
    chapter_id = 0
    for section, articles in feeds.items():
        for art in articles:
            chapter_id += 1
            epub_body, html_body = fetch_article_content(art['url'], temp_book, chapter_id)
            epub_bodies[art['url']] = epub_body
            html_bodies[art['url']] = html_body

    # ── Cover image ──────────────────────────────────────────────────────
    cover_b64 = None
    cover_mime = 'image/jpeg'
    if cover_url:
        cover_bytes, cover_mime_dl, _ = fetch_cover(cover_url)
        if cover_bytes:
            cover_b64 = _base64.b64encode(cover_bytes).decode('ascii')
            cover_mime = cover_mime_dl or 'image/jpeg'

    # ── Build EPUB ───────────────────────────────────────────────────────
    epub_path = f'frontline-{slug}.epub'
    book = build_epub(feeds, issue_label, cover_url,
                      prefetched_bodies=epub_bodies,
                      prefetched_book=temp_book)
    epub.write_epub(epub_path, book)
    print(f'\nSaved EPUB: {epub_path}')

    # ── Build HTML reader ────────────────────────────────────────────────
    html_path = f'frontline-{slug}.html'
    html_content = build_html_reader(feeds, issue_label, html_bodies,
                                     cover_image_b64=cover_b64,
                                     cover_mime=cover_mime)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f'Saved HTML reader: {html_path}')

    # Write slug to file so workflow can read it
    with open('ISSUE_SLUG', 'w') as f:
        f.write(slug)
    print(f'\nSlug written: {slug}')
