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


def fetch_and_embed_images(article, book, chapter_id):
    """Download every image in the article and embed it into the EPUB."""
    img_counter = 0
    for img in article.find_all('img'):
        # Resolve the real src: prefer data-original (lazy-load), then src
        src = img.get('data-original') or img.get('src') or ''
        if not src:
            img.decompose()
            continue
        src = absurl(src)
        # Skip placeholder/spacer images
        if 'placeholder' in src or 'spacer' in src or src.endswith('.gif'):
            img.decompose()
            continue
        try:
            resp = requests.get(src, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
            if content_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
                content_type = 'image/jpeg'
            ext = content_type.split('/')[-1].replace('jpeg', 'jpg')
            img_counter += 1
            img_filename = f'images/ch{chapter_id:04d}_{img_counter:03d}.{ext}'
            img_item = epub.EpubItem(
                uid=f'img-{chapter_id}-{img_counter}',
                file_name=img_filename,
                media_type=content_type,
                content=resp.content,
            )
            book.add_item(img_item)
            # Rewrite src to local embedded path
            img['src'] = f'../{img_filename}'
            # Clean up attrs that EPUB readers don't need
            for attr in ['data-original', 'data-src', 'srcset', 'height', 'width']:
                if img.has_attr(attr):
                    del img[attr]
        except Exception as e:
            print(f'    Warning: could not embed image {src}: {e}')
            img.decompose()


def fetch_article_content(url, book, chapter_id):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        article = soup.find(class_='article-section')
        if not article:
            return '<p><em>Content not available.</em></p>'

        for cls in ['hide-mobile', 'comments-shares', 'share-page', 'editiondetails']:
            for el in article.find_all(class_=cls):
                el.decompose()

        for p in article.find_all('p', class_='caption'):
            p.name = 'figcaption'

        # Embed all images into the EPUB (replaces the old data-original fix)
        fetch_and_embed_images(article, book, chapter_id)

        return sanitize(article.decode_contents())

    except Exception as e:
        return f'<p><em>Failed to fetch article: {html.escape(str(e))}</em></p>'


def build_epub(feeds, today_str, cover_url, edition='delhi', fallback_notice=''):
    book = epub.EpubBook()
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
            body = fetch_article_content(article['url'], book, chapter_id)

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

    book = build_epub(feeds, today_str, cover_url, edition, fallback_notice=fallback_notice)

    # If an explicit output path was given, use it as-is. Otherwise, always name
    # the file after the *actual* edition date fetched (today_str), not the date
    # the script happened to run on — these can differ if a fallback to an
    # earlier available edition occurred.
    output_path = sys.argv[2] if len(sys.argv) > 2 else \
                  f'hindu-{edition}-{today_str}.epub'

    epub.write_epub(output_path, book)
    print(f'\nSaved: {output_path} (edition date: {today_str})')
