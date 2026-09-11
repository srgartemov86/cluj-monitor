#!/usr/bin/env python3
"""Пробник v3: как пройти на detail-страницу imobiliare. Одна сессия Camoufox:
листинг, затем по 2 объявления тремя способами: клик по карточке, goto с
referer, goto после паузы. Usage: python probe_imobiliare_browser3.py <true|virtual>"""
import json, re, sys, time

LIST = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/judetul-cluj/cluj-napoca'
CARD = re.compile(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-(\d+)')
BLOCK = ('captcha-delivery', 'var dd=', 'Attention Required', 'Doar un moment', 'Just a moment')
HEADLESS = sys.argv[1] if len(sys.argv) > 1 else 'true'


def verdict(page):
    for _ in range(8):
        page.wait_for_timeout(2500)
        try:
            html = page.content()
        except Exception:
            continue
        if len(html) > 50000 and not any(b in html for b in BLOCK):
            return 'OK', page.title()[:40]
    return 'BLOCK', (page.title() or '')[:40]


def open_list(page):
    page.goto(LIST, timeout=60000, wait_until='domcontentloaded')
    for _ in range(8):
        page.wait_for_timeout(2500)
        hrefs = CARD.findall(page.content())
        if hrefs:
            return list(dict.fromkeys(re.findall(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-\d+', page.content())))
    return []


def main():
    res = {'headless': HEADLESS, 'click': [], 'referer': [], 'pause': []}
    t0 = time.time()
    try:
        from camoufox.sync_api import Camoufox
        hl = 'virtual' if HEADLESS == 'virtual' else True
        with Camoufox(headless=hl, locale='ro-RO') as browser:
            page = browser.new_page()
            page.goto('https://www.imobiliare.ro/', timeout=60000, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            links = open_list(page)
            res['cards'] = len(links)
            pick = links[:6]
            for rel in pick[0:2]:  # 1) клик из листинга
                try:
                    open_list(page)
                    page.locator(f'a[href*="{rel}"]').first.click(timeout=15000)
                    page.wait_for_timeout(2000)
                    res['click'].append(verdict(page))
                except Exception as e:
                    res['click'].append(('ERR', type(e).__name__))
            for rel in pick[2:4]:  # 2) goto с referer листинга
                try:
                    page.goto('https://www.imobiliare.ro' + rel, referer=LIST, timeout=60000, wait_until='domcontentloaded')
                    res['referer'].append(verdict(page))
                except Exception as e:
                    res['referer'].append(('ERR', type(e).__name__))
            for rel in pick[4:6]:  # 3) пауза 20 с, затем goto
                try:
                    page.wait_for_timeout(20000)
                    page.goto('https://www.imobiliare.ro' + rel, timeout=60000, wait_until='domcontentloaded')
                    res['pause'].append(verdict(page))
                except Exception as e:
                    res['pause'].append(('ERR', type(e).__name__))
    except Exception as e:
        res['error'] = f'{type(e).__name__}: {str(e)[:200]}'
    res['sec'] = round(time.time() - t0)
    print('PROBE3 ' + json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
