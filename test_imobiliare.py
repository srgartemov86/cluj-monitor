#!/usr/bin/env python3
"""Пробник imobiliare через HALO_PROXY с DC-IP: постранично, по попыткам.
Диагноз для sources_down=['imobiliare']: прокси умер / DataDome ужесточился /
разовые сбои. stdout — краткий отчёт, stderr — лог попыток."""
import os, sys, time
from curl_cffi import requests as cffi

PROXY = os.environ.get('HALO_PROXY', '').replace('__cr.rs', '__cr.ro')
BASE = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/judetul-cluj/cluj-napoca'

lines = []
ok_pages = 0
for p in range(1, 7):
    url = BASE + (f'?page={p}' if p > 1 else '')
    verdicts = []
    got = False
    for attempt in range(1, 5):
        try:
            r = cffi.get(url, impersonate='chrome124', timeout=30,
                         proxies={'http': PROXY, 'https': PROXY} if PROXY else None)
            body = r.text or ''
            if r.status_code == 200 and 'oferta/spatiu-comercial' in body:
                verdicts.append(f'{attempt}:OK({len(body)//1024}KB)')
                got = True
                break
            marker = 'datadome' if ('datadome' in body.lower() or 'captcha' in body.lower()) else f'{r.status_code}'
            verdicts.append(f'{attempt}:{marker}')
        except Exception as e:
            verdicts.append(f'{attempt}:{type(e).__name__}')
        time.sleep(1.5)
    ok_pages += got
    lines.append(f"p{p}: {'✅' if got else '❌'} {' '.join(verdicts)}")
    print(lines[-1], file=sys.stderr)

status = '✅ imobiliare OK' if ok_pages >= 5 else ('⚠️ imobiliare partial' if ok_pages else '❌ imobiliare DOWN')
print(f"🔬 imobiliare probe (proxy={'yes' if PROXY else 'NO'}): {status}, {ok_pages}/6 pages\n" + '\n'.join(lines))
