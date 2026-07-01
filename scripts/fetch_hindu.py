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
from datetime import date
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
    # Remove null bytes and ASCII control chars (except tab, newline, CR)
    content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', content)
    return content or '<p><em>Content not available.</em></p>'


def make_xhtml(title, page, teaser, body):
    """Wrap content in a minimal valid XHTML document for ebooklib."""
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
        return dict(feeds), today_str

    raise ValueError('Could not find grouped_articles — The Hindu may not have published today.')


def fetch_article_content(url):
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
        for img in article.find_all('img', attrs={'data-original': True}):
            img['src'] = img['data-original']

        # Use BeautifulSoup's own serializer — handles malformed HTML gracefully
        return sanitize(article.decode_contents())

    except Exception as e:
        return f'<p><em>Failed to fetch article: {html.escape(str(e))}</em></p>'


def build_epub(feeds, today_str, edition='delhi'):
    book = epub.EpubBook()
    book.set_identifier(f'thehindu-{edition}-{today_str}')
    book.set_title(f'The Hindu ({edition.title()}) — {today_str}')
    book.set_language('en')
    book.add_author('The Hindu')

    style = epub.EpubItem(
        uid='main-css',
        file_name='style/main.css',
        media_type='text/css',
        content=CSS,
    )
    book.add_item(style)

    spine = ['nav']
    toc = []
    chapter_id = 0

    for section, articles in feeds.items():
        section_chapters = []
        for article in articles:
            chapter_id += 1
            print(f'  [{section}] {article["title"]}')
            body = fetch_article_content(article['url'])

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

    feeds, today_str = fetch_article_list(edition)
    book = build_epub(feeds, today_str, edition)
    epub.write_epub(output_path, book)
    print(f'\nSaved: {output_path}')
