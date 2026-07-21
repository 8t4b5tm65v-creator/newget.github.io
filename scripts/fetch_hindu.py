#!/usr/bin/env python3
"""
Standalone script replicating the Calibre TheHindu recipe.
Fetches the Delhi print edition and outputs an EPUB.
Usage: python fetch_hindu.py [edition] [output_path]
"""
import json
import re
import sys
import html
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
from ebooklib import epub


HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    )
}

CSS = '''
    body { font-family: Georgia, serif; margin: 1em 2em; }
    h1 { font-size: 1.4em; }
    .caption { font-size: small; text-align: center; }
    .author, .dateLine { font-size: small; color: #555; }
    .subhead, .subhead_lead, .bold { font-weight: bold; }
    img { display: block; margin: 0 auto; max-width: 100%; }
    .italic, .sub-title { font-style: italic; color: #202020; }
'''


def absurl(url):
    if url.startswith('/'):
        return 'https://www.thehindu.com' + url
    return url


def sanitize(content):
    """Strip control characters and ensure content is non-empty valid text."""
    if not content:
        return '<p><em>Content not available.</em></p>'
    content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', content)
    return content or '<p><em>Content not available.</em></p>'


def make_xhtml(title, page, teaser, body, chapter_file):
    """Wrap content in a minimal valid XHTML document for ebooklib."""
    anchor = chapter_file.replace('.xhtml', '')  # e.g. "ch_0001"
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head>'
        f'<title>{html.escape(title)}</title>'
        '<link rel="stylesheet" href="../style/main.css" type="text/css"/>'
        '</head>'
        '<body>'
        f'<h1>{html.escape(title)}</h1>'
        f'<p class="dateLine">Page {html.escape(page)} — {html.escape(teaser)}</p>'
        '<hr/>'
        f'{body}'
        '<hr/>'
        '<p style="text-align:center;font-size:small;">'
        f'<a href="../article_index.xhtml#{anchor}">&#8592; Back to Index</a>'
        '</p>'
        '</body>'
        '</html>'
    )


def make_section_index_xhtml(feeds, today_str, fallback_notice=''):
    """Page 1 — high-level section index linking to anchors in the article index.
    fallback_notice, if set, is rendered as a banner above the section list."""
    section_links = ''
    for section in feeds.keys():
        anchor = re.sub(r'\s+', '_', section)
        section_links += (
            f'<li><a href="article_index.xhtml#{html.escape(anchor)}">'
            f'{html.escape(section)}</a></li>'
        )
    notice_html = (
        f'<p style="background:#fff8dc;border:1px solid #e0c000;padding:0.4em 0.7em;'
        f'border-radius:4px;font-size:small;color:#555;">{fallback_notice}</p>'
    ) if fallback_notice else ''
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head>'
        f'<title>Sections — The Hindu, {html.escape(today_str)}</title>'
        '<link rel="stylesheet" href="../style/main.css" type="text/css"/>'
        '<style>'
        'ul{list-style:none;padding:0;margin:0.5em 0;}'
        'li{margin:0.6em 0;}'
        'li a{text-decoration:none;color:#1a0dab;font-size:1.1em;}'
        '</style>'
        '</head>'
        '<body>'
        f'<h1>The Hindu — Delhi, {html.escape(today_str)}</h1>'
        '<hr/>'
        f'{notice_html}'
        '<ul>'
        f'{section_links}'
        '</ul>'
        '</body>'
        '</html>'
    )


def make_index_xhtml(feeds, today_str, chapter_map):
    """Page 2 — granular article index with anchored section headings and teaser previews."""
    sections_html = ''
    for section, articles in feeds.items():
        section_anchor = re.sub(r'\s+', '_', section)
        previews = ''
        for article in articles:
            fname = chapter_map[article['url']]
            article_anchor = fname.replace('.xhtml', '')  # e.g. "ch_0001"
            teaser = article.get('teaser', '').strip()
            sentences = re.split(r'(?<=[.!?])\s+', teaser)
            preview_text = ' '.join(sentences[:2])
            previews += (
                f'<li id="{article_anchor}">'
                f'<a href="{html.escape(fname)}">{html.escape(article["title"])}</a>'
                + (f'<br/><span style="font-size:small;color:#444;">{html.escape(preview_text)}</span>' if preview_text else '')
                + f'</li>'
            )
        sections_html += (
            f'<h2 id="{html.escape(section_anchor)}">{html.escape(section)}</h2>'
            f'<ul>{previews}</ul>'
        )
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head>'
        f'<title>Index — The Hindu, {html.escape(today_str)}</title>'
        '<link rel="stylesheet" href="../style/main.css" type="text/css"/>'
        '<style>'
        'h2{font-size:1.1em;margin-top:1.2em;border-bottom:1px solid #ccc;padding-bottom:0.2em;}'
        'ul{list-style:none;padding:0;margin:0.3em 0;}'
        'li{margin:0.4em 0;}'
        'li a{text-decoration:none;color:#1a0dab;}'
        '</style>'
        '</head>'
        '<body>'
        f'<h1>The Hindu — Delhi, {html.escape(today_str)}</h1>'
        '<p style="font-size:small;"><a href="section_index.xhtml">&#8592; Back to Sections</a></p>'
        '<hr/>'
        f'{sections_html}'
        '</body>'
        '</html>'
    )


def _fetch_single_day(edition, target_date):
    """Try to fetch one day's edition. Returns (feeds, today_str, cover_url) or None
    if that day's edition isn't available (404, or no grouped_articles found)."""
    today_str = target_date.strftime('%Y-%m-%d')
    url = f'https://www.thehindu.com/todays-paper/{today_str}/th_{edition}/'
    print(f'Fetching index: {url}')

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f'  -> request failed: {e}')
        return None

    if resp.status_code == 404:
        print(f'  -> 404, no edition published for {today_str}')
        return None
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    # Mirror: cover = soup.find(attrs={'class':'hindu-ad'}); self.cover_url = cover.img['src']
    cover_url = None
    cover_el = soup.find(attrs={'class': 'hindu-ad'})
    if cover_el and cover_el.find('img'):
        cover_url = absurl(cover_el.find('img')['src'])
        print(f'Cover image: {cover_url}')
    else:
        print('Cover image not found on index page.')

    for script in soup.find_all('script'):
        text = script.string or ''
        if 'grouped_articles = {"' not in text:
            continue
        match = re.search(r'grouped_articles = ({".*)', text)
        if not match:
            continue
        data = json.JSONDecoder().raw_decode(match.group(1))[0]
        feeds = defaultdict(list)
        for sec in data:
            for item in data[sec]:
                feeds[sec.replace('TH_', '')].append({
                    'title':  item['articleheadline'],
                    'url':    absurl(item['href']),
                    'teaser': item.get('teaser_text', ''),
                    'page':   item.get('pageno', ''),
                })
        total = sum(len(v) for v in feeds.values())
        if total == 0:
            print(f'  -> grouped_articles present but empty for {today_str}')
            return None
        print(f'Found {total} articles across {len(feeds)} sections')
        return dict(feeds), today_str, cover_url

    print(f'  -> grouped_articles not found for {today_str}')
    return None


# ---------------------------------------------------------------------------
# RSS fallback — used when the print edition is unavailable for all lookback
# days (e.g. extended public holiday, site restructure).
#
# Each tuple: (section_display_name, rss_url, max_articles)
# max_articles is the average per-section article count in a normal Delhi
# print edition, so the RSS EPUB stays comparable in volume.
# ---------------------------------------------------------------------------
RSS_SECTION_FEEDS = [
    ('Front Page',    'https://www.thehindu.com/news/national/feeder/default.rss',        8),
    ('National',      'https://www.thehindu.com/news/national/feeder/default.rss',       10),
    ('International', 'https://www.thehindu.com/news/international/feeder/default.rss',   8),
    ('Business',      'https://www.thehindu.com/business/feeder/default.rss',             8),
    ('Opinion',       'https://www.thehindu.com/opinion/feeder/default.rss',              5),
    ('Editorial',     'https://www.thehindu.com/opinion/editorial/feeder/default.rss',    3),
    ('Sport',         'https://www.thehindu.com/sport/feeder/default.rss',                6),
    ('Science',       'https://www.thehindu.com/sci-tech/feeder/default.rss',             4),
    ('Arts',          'https://www.thehindu.com/entertainment/feeder/default.rss',        4),
]


def _parse_rss_pubdate(date_str):
    """Parse an RSS pubDate (RFC 2822) to a date in IST. Returns None on failure."""
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo('Asia/Kolkata')).date()
    except Exception:
        # Fallback: treat UTC offset naively
        return dt.date()


def _fetch_rss_section(section_name, feed_url, max_articles, target_date):
    """Download one RSS feed and return up to max_articles items published on
    target_date (compared in IST).  Returns a list of article dicts compatible
    with the existing feeds dict format used by build_epub."""
    print(f'  RSS [{section_name}] {feed_url}')
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f'    -> request failed: {e}')
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f'    -> RSS parse error: {e}')
        return []

    articles = []
    seen_urls = set()
    for item in root.iter('item'):
        if len(articles) >= max_articles:
            break

        pub_el = item.find('pubDate')
        if pub_el is None or not pub_el.text:
            continue
        if _parse_rss_pubdate(pub_el.text) != target_date:
            continue

        title_el = item.find('title')
        link_el  = item.find('link')
        desc_el  = item.find('description')

        title = (title_el.text or '').strip() if title_el is not None else ''
        url   = (link_el.text  or '').strip() if link_el  is not None else ''
        teaser_raw = (desc_el.text or '')     if desc_el  is not None else ''
        teaser = BeautifulSoup(teaser_raw, 'html.parser').get_text(strip=True)

        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        articles.append({
            'title':  title,
            'url':    url,
            'teaser': teaser,
            'page':   '',   # RSS has no page number
        })

    kept = len(articles)
    print(f'    -> {kept} article(s) kept (cap {max_articles}, target date {target_date})')
    return articles


def fetch_rss_fallback(target_date):
    """Build a feeds dict from RSS for the day *before* target_date.
    Returns (feeds, today_str, cover_url) in the same shape as _fetch_single_day,
    or raises ValueError if no articles were found at all."""
    fallback_date = target_date - timedelta(days=1)
    fallback_str  = fallback_date.strftime('%Y-%m-%d')
    print(f'\nPrint edition unavailable. Switching to RSS fallback for {fallback_str}.')

    feeds = defaultdict(list)
    seen_urls = set()

    for section_name, feed_url, max_articles in RSS_SECTION_FEEDS:
        articles = _fetch_rss_section(section_name, feed_url, max_articles, fallback_date)
        # Deduplicate across sections (National and Front Page share the same feed)
        unique = [a for a in articles if a['url'] not in seen_urls]
        seen_urls.update(a['url'] for a in unique)
        if unique:
            feeds[section_name] = unique

    total = sum(len(v) for v in feeds.values())
    if total == 0:
        raise ValueError(
            f'RSS fallback also returned 0 articles for {fallback_str}. '
            f'The Hindu site may be down or the feeds restructured.'
        )

    print(f'RSS fallback: {total} articles across {len(feeds)} sections for {fallback_str}')
    return dict(feeds), fallback_str, None   # no cover image from RSS


def fetch_article_list(edition='delhi', target_date=None, max_lookback_days=0):
    """Fetch the print edition for target_date (default: today IST). If that
    edition isn't available, falls back immediately to the RSS feeds for the
    previous day — no lookback to earlier print editions."""
    if target_date is None:
        try:
            from zoneinfo import ZoneInfo
            target_date = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        except Exception:
            target_date = date.today()

    for offset in range(max_lookback_days + 1):
        candidate = target_date - timedelta(days=offset)
        result = _fetch_single_day(edition, candidate)
        if result is not None:
            if offset > 0:
                print(f'Note: latest available edition is {offset} day(s) '
                      f'behind the target date — using {result[1]}.')
            return result

    # All lookback days failed — fall back to RSS for the day before target_date.
    return fetch_rss_fallback(target_date)


def fetch_cover(cover_url):
    """Download the cover image, return (bytes, media_type) or (None, None)."""
    try:
        resp = requests.get(cover_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        # Normalise to a supported image media type
        if content_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            content_type = 'image/jpeg'
        ext = content_type.split('/')[-1].replace('jpeg', 'jpg')
        return resp.content, content_type, ext
    except Exception as e:
        print(f'Warning: could not download cover image: {e}')
        return None, None, None


import base64 as _base64

def _download_images(article):
    """Download all images in an article BeautifulSoup tag once.

    Returns a list of (img_tag, src_url, raw_bytes, content_type) for every
    image that was successfully fetched, with the img tag already cleaned of
    lazy-load attributes.  Images that fail to download are decomposed from
    the tree and excluded from the list.
    """
    results = []
    for img in list(article.find_all('img')):
        src = img.get('data-original') or img.get('src') or ''
        if not src:
            img.decompose()
            continue
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

        article = soup.find(class_='article-section')
        if not article:
            stub = '<p><em>Content not available.</em></p>'
            return stub, stub

        for cls in ['hide-mobile', 'comments-shares', 'share-page', 'editiondetails']:
            for el in article.find_all(class_=cls):
                el.decompose()

        for p in article.find_all('p', class_='caption'):
            p.name = 'figcaption'

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
        # Restore the downloaded images onto the same tags (src was just set
        # to the EPUB path above; overwrite it with the data URI now).
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


def build_epub(feeds, today_str, cover_url, edition='delhi', fallback_notice='',
               prefetched_bodies=None, prefetched_book=None):
    """Build an EpubBook from feeds.

    If prefetched_bodies (url->html) and prefetched_book (EpubBook with
    images already embedded) are supplied, article content is taken from
    there instead of re-fetching from the network.
    """
    book = prefetched_book if prefetched_book is not None else epub.EpubBook()
    book.set_identifier(f'thehindu-{edition}-{today_str}')
    display_date = datetime.strptime(today_str, '%Y-%m-%d').strftime('%-d %b %Y')
    book.set_title(f'The Hindu - {edition.title()} - {display_date}')
    book.set_language('en')
    book.add_author('The Hindu')

    # Set cover image
    if cover_url:
        cover_bytes, media_type, ext = fetch_cover(cover_url)
        if cover_bytes:
            book.set_cover(f'cover.{ext}', cover_bytes)
            print(f'Cover set ({media_type}, {len(cover_bytes)} bytes)')

    style = epub.EpubItem(
        uid='main-css',
        file_name='style/main.css',
        media_type='text/css',
        content=CSS,
    )
    book.add_item(style)

    # First pass: assign filenames to every article so the index can link to them
    chapter_map = {}
    chapter_id = 0
    for section, articles in feeds.items():
        for article in articles:
            chapter_id += 1
            chapter_map[article['url']] = f'ch_{chapter_id:04d}.xhtml'

    # Build and add Page 1 — section index
    section_index_page = epub.EpubHtml(
        title='Sections',
        file_name='section_index.xhtml',
        lang='en',
    )
    section_index_page.content = make_section_index_xhtml(feeds, today_str, fallback_notice)
    section_index_page.add_item(style)
    book.add_item(section_index_page)

    # Build and add Page 2 — granular article index
    article_index_page = epub.EpubHtml(
        title='Index',
        file_name='article_index.xhtml',
        lang='en',
    )
    article_index_page.content = make_index_xhtml(feeds, today_str, chapter_map)
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
                article['page'],
                article['teaser'],
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


def build_html_reader(feeds, today_str, article_bodies, fallback_notice=''):
    """Build a self-contained single-file HTML newspaper reader.

    Layer 1 — section pages (horizontal swipe): each section is a full-screen
    "page" styled as a broadsheet column block. Headlines are listed with
    column rules between them.

    Layer 2 — article view: tapping a headline slides in a clean reading pane
    from the right. A back arrow returns to the section page.

    All article HTML is embedded as JSON in a <script> tag so the file is
    fully self-contained (no server, no JS imports).

    Returns the HTML as a string.
    """
    display_date = datetime.strptime(today_str, '%Y-%m-%d').strftime('%-d %B %Y')

    # Serialise article data for JS — sanitise titles/teasers for JSON embedding
    articles_js = []
    art_idx = 0
    section_data = []
    for section, articles in feeds.items():
        sec_articles = []
        for art in articles:
            body_html = article_bodies.get(art['url'], '<p><em>Content not available.</em></p>')
            # Strip script tags from body for safety
            body_html = re.sub(r'<script[\s\S]*?</script>', '', body_html, flags=re.IGNORECASE)
            articles_js.append({
                'id':      art_idx,
                'title':   art['title'],
                'section': section,
                'page':    art.get('page', ''),
                'teaser':  art.get('teaser', ''),
                'body':    body_html,
            })
            sec_articles.append(art_idx)
            art_idx += 1
        section_data.append({'name': section, 'ids': sec_articles})

    articles_json = json.dumps(articles_js, ensure_ascii=False)
    sections_json = json.dumps(section_data, ensure_ascii=False)

    notice_html = ''
    if fallback_notice:
        notice_html = (
            f'<div class="fallback-notice">{html.escape(fallback_notice)}</div>'
        )

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>The Hindu — Delhi — {html.escape(display_date)}</title>
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
  --red:        #b00020;
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
  --red:        #e05070;
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

/* ── Section nav strip ── */
#section-nav {{
  position: fixed; top: 52px; left: 0; right: 0; z-index: 99;
  background: var(--paper-warm);
  border-bottom: 2px solid var(--ink);
  overflow-x: auto; overflow-y: hidden;
  white-space: nowrap;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
  transition: opacity 0.2s, transform 0.2s;
}}
#section-nav::-webkit-scrollbar {{ display: none; }}
#section-nav .nav-inner {{
  display: inline-flex; padding: 0 0.5rem;
}}
.sec-tab {{
  display: inline-block;
  font-family: var(--font-ui); font-size: 0.72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-muted);
  padding: 9px 10px;
  border-bottom: 3px solid transparent;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  white-space: nowrap;
}}
.sec-tab.active {{
  color: var(--ink);
  border-bottom-color: var(--red);
}}

/* ── Pages viewport ── */
#pages-viewport {{
  position: fixed;
  top: 93px; left: 0; right: 0; bottom: 0;
  overflow: hidden;
}}
#pages-track {{
  display: flex; height: 100%;
  transition: transform 0.38s cubic-bezier(0.25, 0.46, 0.45, 0.94);
  will-change: transform;
}}

/* ── Individual section page ── */
.section-page {{
  flex: 0 0 100vw; width: 100vw; height: 100%;
  overflow-y: auto; overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  padding: 1.2rem 0 2rem;
}}
.section-page::-webkit-scrollbar {{ width: 3px; }}
.section-page::-webkit-scrollbar-thumb {{ background: var(--rule); }}

.sec-header {{
  padding: 0 1rem 0.6rem;
  border-bottom: 3px double var(--ink);
  margin-bottom: 0.1rem;
}}
.sec-header h2 {{
  font-family: var(--font-head);
  font-size: 1.05rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--red);
}}
.sec-header .art-count {{
  font-family: var(--font-ui); font-size: 0.7rem;
  color: var(--ink-faint); margin-top: 1px;
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

.art-meta {{
  font-family: var(--font-ui); font-size: 0.65rem;
  color: var(--ink-faint); letter-spacing: 0.04em;
  text-transform: uppercase;
  display: flex; gap: 0.5rem; align-items: center;
}}
.art-meta .page-tag {{
  background: var(--ink); color: var(--paper);
  padding: 1px 5px; border-radius: 2px;
  font-size: 0.6rem;
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
  color: var(--red); font-weight: 600;
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
  letter-spacing: 0.08em; color: var(--red);
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
#article-pane .art-source-link a {{ color: var(--red); }}

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

/* ── Fallback notice ── */
.fallback-notice {{
  font-family: var(--font-ui); font-size: 0.75rem;
  background: #fff8dc; border: 1px solid #e0c000;
  color: #555; padding: 0.5rem 1rem;
  margin: 0.5rem 1rem 0;
  border-radius: 4px;
}}

/* ── Swipe hint (first load) ── */
#swipe-hint {{
  position: fixed; bottom: 1.2rem; left: 50%;
  transform: translateX(-50%);
  background: rgba(26,26,24,0.75); color: #f8f6f0;
  font-family: var(--font-ui); font-size: 0.72rem;
  padding: 6px 14px; border-radius: 20px;
  pointer-events: none;
  animation: fadeout 3s ease 1.5s forwards;
  white-space: nowrap; z-index: 200;
}}
@keyframes fadeout {{ to {{ opacity: 0; }} }}

/* ── Desktop: wider article reading column ── */
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

@media (min-width: 700px) {{
  #article-pane {{
    left: 0; right: 0;
    padding: 2rem 2.5rem 4rem;
  }}
  .article-row {{
    grid-template-columns: 1fr;
    padding: 0.9rem 1.5rem;
  }}
  .sec-header {{ padding: 0 1.5rem 0.7rem; }}
  .section-page {{ padding: 1.4rem 0 2rem; }}
  .art-headline {{ font-size: 1.1rem; }}
  #masthead .nameplate {{ font-size: 1.5rem; }}
  .fallback-notice {{ margin: 0.5rem 1.5rem 0; }}
}}
</style>
</head>
<body>

<header id="masthead">
  <button class="back-btn" id="back-btn" onclick="closeArticle()" aria-label="Back to section">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="15 18 9 12 15 6"/>
    </svg>
    Back
  </button>
  <div class="nameplate">The Hindu</div>
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
    <div class="edition-date">Delhi<br/>{html.escape(display_date)}</div>
  </div>
</header>

<nav id="section-nav"><div class="nav-inner" id="nav-inner"></div></nav>

{notice_html}

<div id="pages-viewport">
  <div id="pages-track" id="pages-track"></div>
</div>

<div id="blur-overlay"></div>

<div id="article-pane" aria-label="Article reader">
  <div class="art-pane-section" id="pane-section"></div>
  <h1 id="pane-title"></h1>
  <div class="art-pane-meta" id="pane-meta"></div>
  <div class="art-pane-body" id="pane-body"></div>
  <div class="art-source-link" id="pane-source"></div>
</div>

<div id="swipe-hint">Swipe left / right to change section</div>

<script>
const ARTICLES = {articles_json};
const SECTIONS = {sections_json};

let currentSection = 0;
let articlePaneOpen = false;
const track = document.getElementById('pages-track');
const navInner = document.getElementById('nav-inner');
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

function buildUI() {{
  SECTIONS.forEach((sec, si) => {{
    const tab = document.createElement('div');
    tab.className = 'sec-tab' + (si === 0 ? ' active' : '');
    tab.textContent = sec.name;
    tab.onclick = () => goToSection(si);
    navInner.appendChild(tab);

    const page = document.createElement('div');
    page.className = 'section-page';
    page.id = 'page-' + si;

    const header = document.createElement('div');
    header.className = 'sec-header';
    header.innerHTML = `<h2>${{sec.name}}</h2><div class="art-count">${{sec.ids.length}} article${{sec.ids.length !== 1 ? 's' : ''}}</div>`;
    page.appendChild(header);

    sec.ids.forEach(aid => {{
      const art = ARTICLES[aid];
      const row = document.createElement('div');
      row.className = 'article-row';
      row.setAttribute('role', 'button');
      row.setAttribute('tabindex', '0');
      row.setAttribute('aria-label', art.title);

      let metaHtml = '';
      if (art.page) metaHtml += `<span class="page-tag">P.${{art.page}}</span>`;
      metaHtml += `<span>${{sec.name}}</span>`;

      row.innerHTML = `
        <div class="art-meta">${{metaHtml}}</div>
        <div class="art-headline">${{escHtml(art.title)}}</div>
        ${{art.teaser ? `<div class="art-teaser">${{escHtml(art.teaser)}}</div>` : ''}}
        <div class="art-read-cue">Read &rsaquo;</div>
      `;
      row.onclick = () => openArticle(aid);
      row.onkeydown = e => {{ if (e.key === 'Enter' || e.key === ' ') openArticle(aid); }};
      page.appendChild(row);
    }});

    track.appendChild(page);
  }});
}}

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function goToSection(idx) {{
  if (articlePaneOpen) return;
  currentSection = idx;
  track.style.transform = `translateX(${{-idx * 100}}vw)`;
  document.querySelectorAll('.sec-tab').forEach((t, i) => {{
    t.classList.toggle('active', i === idx);
  }});
  const activeTab = navInner.children[idx];
  if (activeTab) activeTab.scrollIntoView({{ inline: 'center', behavior: 'smooth', block: 'nearest' }});
}}

function openArticle(aid) {{
  const art = ARTICLES[aid];
  document.getElementById('pane-section').textContent = art.section;
  document.getElementById('pane-title').textContent = art.title;

  let metaHtml = '';
  if (art.page) metaHtml += `<span>Page ${{art.page}}</span>`;
  metaHtml += `<span>${{art.section}}</span>`;
  document.getElementById('pane-meta').innerHTML = metaHtml;

  document.getElementById('pane-body').innerHTML = art.body ||
    '<p><em>Content not available.</em></p>';

  const srcEl = document.getElementById('pane-source');
  srcEl.innerHTML = art.url
    ? `Read on thehindu.com: <a href="${{escHtml(art.url)}}" target="_blank" rel="noopener">${{escHtml(art.url)}}</a>`
    : '';

  pane.scrollTop = 0;
  pane.style.transform = 'translateX(0)';
  document.getElementById('blur-overlay').style.opacity = '1';
  articlePaneOpen = true;
  backBtn.style.display = 'flex';
  document.getElementById('section-nav').style.opacity = '0.4';
  document.getElementById('section-nav').style.pointerEvents = 'none';
  history.pushState({{ article: aid }}, '');
}}

function closeArticle() {{
  pane.style.transform = 'translateX(100%)';
  document.getElementById('blur-overlay').style.opacity = '0';
  articlePaneOpen = false;
  backBtn.style.display = 'none';
  document.getElementById('section-nav').style.opacity = '';
  document.getElementById('section-nav').style.pointerEvents = '';
}}

window.addEventListener('popstate', () => {{
  if (articlePaneOpen) closeArticle();
}});

// Touch swipe on pages viewport
let touchStartX = 0, touchStartY = 0, swiping = false;
const viewport = document.getElementById('pages-viewport');

viewport.addEventListener('touchstart', e => {{
  touchStartX = e.touches[0].clientX;
  touchStartY = e.touches[0].clientY;
  swiping = false;
}}, {{ passive: true }});

viewport.addEventListener('touchmove', e => {{
  const dx = e.touches[0].clientX - touchStartX;
  const dy = e.touches[0].clientY - touchStartY;
  if (!swiping && Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 8) {{
    swiping = true;
  }}
  if (swiping) e.preventDefault();
}}, {{ passive: false }});

viewport.addEventListener('touchend', e => {{
  if (!swiping || articlePaneOpen) return;
  const dx = e.changedTouches[0].clientX - touchStartX;
  if (Math.abs(dx) > 45) {{
    const next = currentSection + (dx < 0 ? 1 : -1);
    if (next >= 0 && next < SECTIONS.length) goToSection(next);
  }}
}}, {{ passive: true }});

// Keyboard navigation
document.addEventListener('keydown', e => {{
  if (articlePaneOpen) {{
    if (e.key === 'Escape') closeArticle();
    return;
  }}
  if (e.key === 'ArrowRight' && currentSection < SECTIONS.length - 1) goToSection(currentSection + 1);
  if (e.key === 'ArrowLeft' && currentSection > 0) goToSection(currentSection - 1);
}});

buildUI();
goToSection(0);
syncThemeIcon();
</script>
</body>
</html>'''


if __name__ == '__main__':
    edition = sys.argv[1] if len(sys.argv) > 1 else 'delhi'

    try:
        from zoneinfo import ZoneInfo
        target_date = datetime.now(ZoneInfo('Asia/Kolkata')).date()
    except Exception:
        target_date = date.today()

    feeds, today_str, cover_url = fetch_article_list(edition, target_date=target_date)

    # Detect whether we ended up on the RSS fallback path:
    # today_str will be yesterday's date relative to target_date.
    fetched_date = datetime.strptime(today_str, '%Y-%m-%d').date()
    is_rss_fallback = (fetched_date == target_date - timedelta(days=1) and cover_url is None)

    fallback_notice = ''
    if is_rss_fallback:
        target_str = target_date.strftime('%-d %b %Y')
        fallback_notice = (
            f'Note: the print edition for {target_str} was not yet available. '
            f'This issue contains articles from the RSS feeds dated {today_str}.'
        )
        print(f'RSS fallback active — notice: {fallback_notice}')

    # ── Fetch all article bodies once; share between EPUB and HTML reader ──
    # fetch_article_content now returns (epub_body, html_body):
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
    book = build_epub(feeds, today_str, cover_url, edition,
                      fallback_notice=fallback_notice,
                      prefetched_bodies=epub_bodies,
                      prefetched_book=temp_book)

    output_path = sys.argv[2] if len(sys.argv) > 2 else \
                  f'hindu-{edition}-{today_str}.epub'
    epub.write_epub(output_path, book)
    print(f'\nSaved EPUB: {output_path}')

    # ── Build HTML reader ──
    html_path = output_path.replace('.epub', '.html')
    html_content = build_html_reader(feeds, today_str, html_bodies,
                                     fallback_notice=fallback_notice)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f'Saved HTML reader: {html_path}')
    print(f'\nEdition date: {today_str}')
