#!/usr/bin/env python3
"""Проба всех белградских источников с DC-IP GitHub Actions — точные прод-эндпоинты curl_sweep.py."""
import json, sys
from urllib.parse import urlencode, quote
from curl_cffi import requests as cffi


def check(name, url, marker):
    try:
        r = cffi.get(url, impersonate='chrome120', timeout=20)
        has = marker in r.text if marker else True
        ok = r.status_code == 200 and has
        return ok, f'{name}: {"✅" if ok else "❌"} status={r.status_code} len={len(r.content)//1024}KB marker={"да" if has else "НЕТ"}'
    except Exception as e:
        return False, f'{name}: ❌ EXC {type(e).__name__}'


nek_params = {
    'fkRegione': 'RS_1', 'idProvincia': 'RS_3', 'idComune': '324',
    'idNazione': 'RS', 'idContratto': '2', 'idCategoria': '26',
    '__lang': 'sr', 'path': '/izdavanje-lokala/beograd/',
    'criterio': 'data', 'ordine': 'desc', 'pag': 1,
}
ce_req = {'ptId': [4], 'cityId': 1, 'rentOrSale': 'r',
          'searchSource': 'regular', 'sort': 'datedsc', 'currentPage': 1}

tests = [
    ('4zida.rs (список)', 'https://www.4zida.rs/izdavanje-poslovnih-prostora/beograd?strana=1', 'oglas'),
    ('nekretnine.rs (API)', 'https://www.nekretnine.rs/api-next/search-list/listings/?' + urlencode(nek_params), 'realEstate'),
    ('cityexpert.rs (API)', 'https://cityexpert.rs/api/Search?req=' + quote(json.dumps(ce_req, separators=(',', ':'))), 'prop'),
    ('halooglasi (список)', 'https://www.halooglasi.com/nekretnine/izdavanje-poslovnog-prostora/beograd?page=1', 'serverListData'),
    ('img.halooglasi (CDN)', 'https://img.halooglasi.com/', ''),
]

lines = ['🧪 Белградские источники с DC-IP GitHub Actions:']
oks = 0
for name, url, marker in tests:
    ok, line = check(name, url, marker)
    oks += ok
    lines.append('  ' + line)

lines.append('')
lines.append(f'Итог: {oks}/{len(tests)} доступны с датацентрового IP.')
print('\n'.join(lines))
sys.exit(0)
