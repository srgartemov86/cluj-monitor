#!/usr/bin/env python3
"""test_halooglasi.py — проверка, проходит ли halooglasi.com Cloudflare с DC-IP GitHub Actions.

Реплицирует прод-логику белградского sweep_halooglasi: ротация impersonate-профилей
curl_cffi по страницам списка izdavanje-poslovnog-prostora. Считает успехом status==200
+ len>=5000 + наличие QuidditaEnvironment.serverListData + непустой Ads.

Печатает человекочитаемую сводку в stdout (идёт в Telegram) и подробности в stderr (в лог Actions).
Exit 0 если хотя бы одна страница отдала объявления, иначе 1.
"""
import json, re, sys, time

LIST_URL = 'https://www.halooglasi.com/nekretnine/izdavanje-poslovnog-prostora/beograd?page={p}'
IMPS = ('chrome120', 'chrome124', 'chrome131', 'safari17_0')
PAGES = 2


def _balanced_brace(s, start):
    i = s.find('{', start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(s)):
        c = s[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return s[i:j + 1]
    return None


def main():
    try:
        from curl_cffi import requests as cffi_requests
    except Exception as e:
        print(f'❌ curl_cffi не установлен: {e}')
        return 1

    lines = ['🧪 halooglasi.com с DC-IP GitHub Actions:']
    total_ads = 0
    any_ok = False
    ip_seen = None

    # покажем сам IP раннера (для лога)
    try:
        ipr = cffi_requests.get('https://api.ipify.org', impersonate='chrome120', timeout=10)
        ip_seen = ipr.text.strip()
    except Exception as e:
        ip_seen = f'(не определён: {e})'
    print(f'runner IP: {ip_seen}', file=sys.stderr)

    for p in range(1, PAGES + 1):
        url = LIST_URL.format(p=p)
        res = None
        attempts = []
        t0 = time.time()
        for imp in IMPS:
            try:
                rr = cffi_requests.Session(impersonate=imp).get(url, timeout=15)
                attempts.append(f'{imp}:{rr.status_code}/{len(rr.content)//1024}KB')
                if rr.status_code == 200 and len(rr.content) >= 5000:
                    res = (imp, rr)
                    break
            except Exception as e:
                attempts.append(f'{imp}:ERR')
                print(f'  p{p} {imp}: EXC {e}', file=sys.stderr)
        dt = time.time() - t0
        print(f'  p{p} attempts: {" ".join(attempts)} ({dt:.1f}s)', file=sys.stderr)

        if res is None:
            lines.append(f'  стр.{p}: ❌ Cloudflare/блок ({" ".join(attempts)})')
            continue

        imp, r = res
        html = r.text
        m = re.search(r'QuidditaEnvironment\.serverListData\s*=', html)
        if not m:
            lines.append(f'  стр.{p}: ⚠️ 200 но нет serverListData (челлендж-страница), {imp}')
            continue
        blob = _balanced_brace(html, m.start())
        n_ads = 0
        if blob:
            try:
                n_ads = len(json.loads(blob).get('Ads', []))
            except Exception as e:
                print(f'  p{p}: JSON err {e}', file=sys.stderr)
        total_ads += n_ads
        if n_ads > 0:
            any_ok = True
            lines.append(f'  стр.{p}: ✅ {n_ads} объявл. (профиль {imp})')
        else:
            lines.append(f'  стр.{p}: ⚠️ serverListData есть, но Ads пуст ({imp})')

    lines.append('')
    if any_ok:
        lines.append(f'ВЕРДИКТ: ✅ РАБОТАЕТ с DC-IP — всего {total_ads} объявл. за {PAGES} стр. Перенос в облако возможен.')
    else:
        lines.append('ВЕРДИКТ: ❌ НЕ РАБОТАЕТ с DC-IP — Cloudflare банит сервер. Нужен прокси или гибрид.')
    lines.append(f'IP раннера: {ip_seen}')

    print('\n'.join(lines))
    return 0 if any_ok else 1


if __name__ == '__main__':
    sys.exit(main())
