#!/usr/bin/env python3
"""test_halooglasi.py — проверка halooglasi/imobiliare с DC-IP GitHub Actions,
напрямую и через резидентный прокси (env HALO_PROXY, DataImpulse).

Меряет success rate с ретраями — как будет работать прод.
Exit 0 если halooglasi доступен хоть каким-то путём.
"""
import json, os, re, sys, time

from curl_cffi import requests as cffi

HALO = 'https://www.halooglasi.com/nekretnine/izdavanje-poslovnog-prostora/beograd?page={p}'
IMO = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/cluj-napoca'
PROXY = os.environ.get('HALO_PROXY', '')


def attempt(url, marker, use_proxy, imp='chrome120'):
    kw = {'impersonate': imp, 'timeout': 30}
    if use_proxy and PROXY:
        kw['proxies'] = {'http': PROXY, 'https': PROXY}
    try:
        r = cffi.get(url, **kw)
        return r.status_code == 200 and marker in r.text, r.status_code
    except Exception as e:
        return False, type(e).__name__


def series(label, url, marker, use_proxy, n=8):
    ok = 0
    codes = []
    for i in range(n):
        good, code = attempt(url, marker, use_proxy)
        ok += good
        codes.append(str(code))
        time.sleep(0.3)
    return ok, n, codes


lines = ['🧪 halooglasi/imobiliare с GitHub-раннера:']

ok_d, n_d, codes_d = series('halo-direct', HALO.format(p=1), 'serverListData', False, n=3)
lines.append(f'  halooglasi напрямую: {ok_d}/{n_d} ({" ".join(codes_d)})')

if PROXY:
    ok_p, n_p, codes_p = series('halo-proxy', HALO.format(p=1), 'serverListData', True, n=8)
    lines.append(f'  halooglasi через прокси: {ok_p}/{n_p} ({" ".join(codes_p)})')
    # вторая страница — прогрев не влияет?
    ok_p2, n_p2, codes_p2 = series('halo-proxy-p2', HALO.format(p=2), 'serverListData', True, n=4)
    lines.append(f'  halooglasi p2 через прокси: {ok_p2}/{n_p2} ({" ".join(codes_p2)})')
    imo_proxy = PROXY.replace('__cr.rs', '__cr.ro')
    globals()['PROXY'] = imo_proxy
    ok_i, n_i, codes_i = series('imo-proxy', IMO, 'inchirieri', True, n=6)
    lines.append(f'  imobiliare через прокси (RO): {ok_i}/{n_i} ({" ".join(codes_i)})')
else:
    lines.append('  HALO_PROXY не задан — прокси-тест пропущен')
    ok_p = 0

lines.append('')
if ok_p:
    per_page = 1 - (1 - ok_p / 8) ** 4
    lines.append(f'ВЕРДИКТ: ✅ halooglasi работает через прокси; с 4 ретраями надёжность/стр ≈ {per_page * 100:.0f}%')
else:
    lines.append('ВЕРДИКТ: ❌ halooglasi недоступен и через прокси')

print('\n'.join(lines))
sys.exit(0 if (ok_p or ok_d) else 1)
