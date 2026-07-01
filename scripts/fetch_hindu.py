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
from datetime import date, datetime
from collections import defaultdict

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


def make_section_index_xhtml(feeds, today_str):
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


def fetch_article_list(edition='delhi', target_date=None):
    if target_date is None:
        target_date = date.today()

    today_str = target_date.strftime('%Y-%m-%d')
    url = f'https://www.thehindu.com/todays-paper/{today_str}/th_{edition}/'
    print(f'Fetching index: {url}')

    resp = requests.get(url, headers=HEADERS, timeout=30)
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
        print(f'Found {total} articles across {len(feeds)} sections')
        return dict(feeds), today_str, cover_url

    raise ValueError('Could not find grouped_articles — The Hindu may not have published today.')


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


def build_epub(feeds, today_str, cover_url, edition='delhi'):
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
    section_index_page.content = make_section_index_xhtml(feeds, today_str)
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
    edition     = sys.argv[1] if len(sys.argv) > 1 else 'delhi'
    output_path = sys.argv[2] if len(sys.argv) > 2 else \
                  f'hindu-{edition}-{date.today()}.epub'

    feeds, today_str, cover_url = fetch_article_list(edition)
    book = build_epub(feeds, today_str, cover_url, edition)
    epub.write_epub(output_path, book)
    print(f'\nSaved: {output_path}')
