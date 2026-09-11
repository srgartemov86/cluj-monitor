#!/usr/bin/env python3
"""Пробник v4: что содержит отрендеренный листинг imobiliare и какие JSON он
подгружает. Цель: взять описание/этаж/координаты/фото из листинга, не открывая
detail (он под DataDome 100%). Пишет listing_p1.html, listing_p2.html и
responses.json (url + первые 20 КБ каждого JSON-ответа)."""
import json, re, time

LIST = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/judetul-cluj/cluj-napoca'
CARD = re.compile(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-\d+')
resp_log = []


def on_response(r):
    try:
        ct = (r.headers or {}).get('content-type', '')
        if 'json' in ct and 'imobiliare' in r.url:
            body = r.text()
            resp_log.append({'url': r.url[:300], 'status': r.status, 'kb': len(body) // 1024, 'body': body[:20000]})
    except Exception as e:
        resp_log.append({'url': r.url[:300], 'err': type(e).__name__})


def wait_cards(page):
    for _ in range(10):
        page.wait_for_timeout(2500)
        html = page.content()
        if CARD.search(html):
            return html
    return page.content()


from camoufox.sync_api import Camoufox
t0 = time.time()
with Camoufox(headless=True, locale='ro-RO') as browser:
    page = browser.new_page()
    page.on('response', on_response)
    page.goto('https://www.imobiliare.ro/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(3000)
    page.goto(LIST, timeout=60000, wait_until='domcontentloaded')
    open('listing_p1.html', 'w', encoding='utf-8').write(wait_cards(page))
    page.wait_for_timeout(2000)
    page.goto(LIST + '?page=2', timeout=60000, wait_until='domcontentloaded')
    open('listing_p2.html', 'w', encoding='utf-8').write(wait_cards(page))
json.dump(resp_log, open('responses.json', 'w', encoding='utf-8'), ensure_ascii=False)
print('PROBE4 ' + json.dumps({'responses': len(resp_log), 'sec': round(time.time() - t0)}))
