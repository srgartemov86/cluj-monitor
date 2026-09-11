#!/usr/bin/env python3
"""Пробник v2: надёжность Camoufox напрямую. Одна сессия браузера:
главная -> листинг стр.1..6 -> одна detail-страница. Печатает PROBE2 {...}."""
import json, re, sys, time

BASE = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/judetul-cluj/cluj-napoca'
CARD = re.compile(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-\d+')


def wait_cards(page, rounds=8):
    for _ in range(rounds):
        page.wait_for_timeout(2500)
        try:
            html = page.content()
        except Exception:
            continue
        cards = set(CARD.findall(html))
        if cards:
            return cards, html
    return set(), ''


def main():
    res = {'attempt': sys.argv[1] if len(sys.argv) > 1 else '?', 'pages': {}, 'detail': None}
    t0 = time.time()
    try:
        from camoufox.sync_api import Camoufox
        with Camoufox(headless=True, locale='ro-RO') as browser:
            page = browser.new_page()
            page.goto('https://www.imobiliare.ro/', timeout=60000, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            allc = set()
            for p in range(1, 7):
                url = BASE + (f'?page={p}' if p > 1 else '')
                try:
                    page.goto(url, timeout=60000, wait_until='domcontentloaded')
                    cards, _ = wait_cards(page)
                    res['pages'][p] = len(cards)
                    allc |= cards
                except Exception as e:
                    res['pages'][p] = f'ERR {type(e).__name__}'
                page.wait_for_timeout(1500)
            res['unique_cards'] = len(allc)
            if allc:
                d = 'https://www.imobiliare.ro' + sorted(allc)[0]
                try:
                    page.goto(d, timeout=60000, wait_until='domcontentloaded')
                    page.wait_for_timeout(4000)
                    html = page.content()
                    res['detail'] = {'kb': len(html) // 1024,
                                     'has_price': bool(re.search(r'EUR|€|lei', html)),
                                     'blocked': ('captcha-delivery' in html or 'Attention Required' in html
                                                 or 'Doar un moment' in html),
                                     'title': page.title()[:50]}
                except Exception as e:
                    res['detail'] = f'ERR {type(e).__name__}'
    except Exception as e:
        res['error'] = f'{type(e).__name__}: {str(e)[:200]}'
    res['sec'] = round(time.time() - t0)
    print('PROBE2 ' + json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
