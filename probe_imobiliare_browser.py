#!/usr/bin/env python3
"""Пробник: пробивает ли реальный браузер JS-челлендж DataDome на imobiliare.ro.
Usage: python probe_imobiliare_browser.py <playwright|camoufox> <direct|proxy>
Печатает строку PROBE {...} с числом карточек на странице листинга."""
import json, os, re, sys, time

LIST = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/judetul-cluj/cluj-napoca'
MODE, NET = sys.argv[1], sys.argv[2]
RAW = os.environ.get('HALO_PROXY', '').replace('__cr.rs', '__cr.ro').replace('__cr.hu', '__cr.ro')


def proxy_cfg():
    if NET != 'proxy' or not RAW:
        return None
    m = re.match(r'https?://([^:]+):([^@]+)@([^:]+):(\d+)', RAW)
    user = re.sub(r'^([^_]+)', r'\1__sid.probe%d' % int(time.time()), m.group(1), count=1) if '__sid' not in m.group(1) else m.group(1)
    return {'server': f'http://{m.group(3)}:{m.group(4)}', 'username': user, 'password': m.group(2)}


def evaluate(html):
    cards = len(set(re.findall(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-\d+', html)))
    return cards, ('captcha-delivery' in html or 'var dd=' in html)


def run_flow(page):
    out = {'home_ok': None, 'cards': 0, 'datadome': None, 'title': ''}
    try:
        page.goto('https://www.imobiliare.ro/', timeout=60000, wait_until='domcontentloaded')
        page.wait_for_timeout(5000)
        out['home_ok'] = True
    except Exception as e:
        out['home_ok'] = f'{type(e).__name__}'
    try:
        page.goto(LIST, timeout=60000, wait_until='domcontentloaded')
    except Exception as e:
        out['list_err'] = type(e).__name__
    for _ in range(8):  # до ~40 с на решение челленджа
        page.wait_for_timeout(5000)
        try:
            html = page.content()
        except Exception:
            continue
        out['cards'], out['datadome'] = evaluate(html)
        if out['cards']:
            break
    try:
        out['title'] = page.title()[:60]
    except Exception:
        pass
    return out


def main():
    res = {'mode': MODE, 'net': NET}
    t0 = time.time()
    try:
        if MODE == 'playwright':
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, proxy=proxy_cfg(),
                                      args=['--disable-blink-features=AutomationControlled'])
                ctx = b.new_context(locale='ro-RO', viewport={'width': 1366, 'height': 800},
                                    user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'))
                res.update(run_flow(ctx.new_page()))
                b.close()
        else:
            from camoufox.sync_api import Camoufox
            pc = proxy_cfg()
            with Camoufox(headless=True, proxy=pc, geoip=bool(pc), locale='ro-RO') as browser:
                res.update(run_flow(browser.new_page()))
    except Exception as e:
        res['error'] = f'{type(e).__name__}: {str(e)[:200]}'
    res['sec'] = round(time.time() - t0)
    print('PROBE ' + json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
