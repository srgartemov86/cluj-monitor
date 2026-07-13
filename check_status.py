#!/usr/bin/env python3
"""
Live-status check для лотов, помеченных in_sheet=True.

Запускается из цикла pizzeria-monitor ПОСЛЕ sweep, ДО gen_map.
Помечает мёртвые объявления `removed_from_sheet=True` чтобы они исчезли с карты.

Mortality markers (Cluj v1, УТОЧНИТЬ на реальных мёртвых лотах):
  • olx.ro:        404/410 при снятии; live-страница содержит title объявления.
                   Маркер деактивации: "nu mai este disponibil" / redirect на категорию.
  • storia.ro:     404/410; live имеет __NEXT_DATA__ с ad. Маркер: title
                   "Anunțul nu a fost găsit" / pageProps без ad.
  • imobiliare.ro: DataDome, только curl_cffi. 404/410; маркер: "anunt inactiv" /
                   "expirat" в title.

Anti-rate-limit:
  • Throttle до MAX_PER_CYCLE_PER_SOURCE, пауза DELAY_SEC между запросами
    одного источника. Параллельно по разным источникам.
  • 429 — НЕ dead, помечаем `rate_limited`, пропускаем до следующего цикла.

Stale-grace:
  • Не проверяем лоты с last_seen_at моложе MIN_AGE_HOURS — могут быть свежими.
"""

import json, re, subprocess, time, sys, os, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE_PATH = os.path.join(os.environ.get('CLUJ_DATA', '/Users/dodo/cluj-location-monitor'), 'state.json')

# Throttling per source (per cycle)
MAX_PER_CYCLE_PER_SOURCE = {'olx.ro': 20, 'storia.ro': 20, 'imobiliare.ro': 12}
DELAY_SEC_PER_SOURCE = {'olx.ro': 0.5, 'storia.ro': 0.5, 'imobiliare.ro': 1.5}

# Don't check lots seen < this many hours ago (avoid checking fresh sweep results)
MIN_AGE_HOURS = 6

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'


def is_dead(src, http_code, size, title, valid_to=None):
    """Returns one of: 'dead', 'alive', 'rate_limited', 'unknown'."""
    title_l = title.lower()
    if '429' in title or "exceeded the limits" in title_l or http_code == '429':
        return 'rate_limited'
    if http_code in ('404', '410'):
        return 'dead'
    if http_code != '200':
        return 'unknown'

    if src == 'olx.ro':
        # valid_to: OLX API/страница содержит valid_to_time — истёк → снят
        if valid_to:
            try:
                vt = dt.datetime.fromisoformat(valid_to.replace('Z', '+00:00'))
                if vt < dt.datetime.now(dt.timezone.utc):
                    return 'dead'
            except Exception:
                pass
        if ('nu mai este disponibil' in title_l or 'nu a fost g' in title_l
                or 'anunturi olx.ro' == title_l.strip()):
            return 'dead'
        if title:
            return 'alive'
        return 'unknown'

    if src == 'storia.ro':
        if 'nu a fost g' in title_l or 'nu mai este disponibil' in title_l:
            return 'dead'
        if title:
            return 'alive'
        return 'unknown'

    if src == 'imobiliare.ro':
        if ('inactiv' in title_l or 'expirat' in title_l
                or 'nu mai este disponibil' in title_l):
            return 'dead'
        if title:
            return 'alive'
        return 'unknown'

    return 'unknown'


def fetch(url, timeout=20):
    """Returns (http_code, size, title, valid_to) or ('ERR', 0, '<error>', None).

    halooglasi.com is behind Cloudflare and rejects plain curl (returns 403
    'Just a moment...'). For it we use curl_cffi with Chrome TLS fingerprint
    и дополнительно вытаскиваем ValidTo (срок размещения) — единственный надёжный
    признак мёртвого лота (title не меняется).
    """
    if 'imobiliare.ro' in url:
        if not HAS_CFFI:
            return 'ERR', 0, 'curl_cffi missing — cannot bypass DataDome', None
        try:
            import curl_sweep
            # imo_get: ретраи + резидентный прокси. None/не-200 = DataDome-блок,
            # это НЕ признак снятия лота — вернём код как есть, is_dead даст unknown
            r = curl_sweep.imo_get(url, timeout=timeout)
            if r is None:
                return 'ERR', 0, 'DataDome block after retries', None
            body = r.text
            m = re.search(r'<title>([^<]*)</title>', body)
            title = m.group(1).strip() if m else ''
            if r.status_code != 200:
                return str(r.status_code), len(body), '', None
            return str(r.status_code), len(body), title, None
        except Exception as e:
            return 'ERR', 0, str(e)[:80], None

    try:
        r = subprocess.run(
            ['curl', '-sL', '-A', UA, '--max-time', str(timeout),
             '-w', '\n__HTTP_CODE__%{http_code}', url],
            capture_output=True, timeout=timeout + 5
        )
        body = r.stdout.decode('utf-8', errors='replace')
        # Split off http_code marker
        idx = body.rfind('\n__HTTP_CODE__')
        if idx >= 0:
            http_code = body[idx + len('\n__HTTP_CODE__'):].strip()
            body = body[:idx]
        else:
            http_code = '???'
        m = re.search(r'<title>([^<]*)</title>', body)
        title = m.group(1).strip() if m else ''
        valid_to = None
        if 'olx.ro' in url:
            vm = re.search(r'"valid_to_time"\s*:\s*"([^"]+)"', body)
            valid_to = vm.group(1) if vm else None
        return http_code, len(body), title, valid_to
    except Exception as e:
        return 'ERR', 0, str(e)[:80], None


def check_one(key, src, url):
    http_code, size, title, valid_to = fetch(url)
    verdict = is_dead(src, http_code, size, title, valid_to)
    return key, src, url, http_code, size, title, verdict


def main():
    print('=== check_status ===')
    t0 = time.time()
    with open(STATE_PATH) as f:
        state = json.load(f)

    now = dt.datetime.now(dt.timezone.utc)
    min_age = dt.timedelta(hours=MIN_AGE_HOURS)

    # Group candidates by source
    by_source = {}
    for k, v in state['listings'].items():
        if not v.get('in_sheet'): continue
        if v.get('removed_from_sheet'): continue
        if v.get('rejected'): continue
        url = v.get('url', '')
        if not url: continue
        src = v.get('source', '')
        if src not in MAX_PER_CYCLE_PER_SOURCE: continue
        # Grace для свежих НОВЫХ лотов: пропускаем недавно ОБНАРУЖЕННЫЕ (first_seen_at),
        # а НЕ недавно увиденные. last_seen sweep обновляет каждый час, в т.ч. на протухших
        # объявлениях, которые ещё висят в выдаче halooglasi — из-за last_seen-grace проверка
        # до них никогда не доходила, и мёртвые лоты жили вечно (баг 2026-06-20).
        first_seen = v.get('first_seen_at')
        if first_seen:
            try:
                fso = dt.datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
                if now - fso < min_age: continue
            except Exception: pass
        by_source.setdefault(src, []).append((k, src, url))

    # Cap per source — оldest-first so we cycle through eventually
    plan = []
    for src, items in by_source.items():
        items.sort(key=lambda x: str(state['listings'][x[0]].get('last_seen_at') or ''))
        cap = MAX_PER_CYCLE_PER_SOURCE.get(src, 10)
        plan.append((src, items[:cap], items[cap:]))
        print(f'  {src:15} candidates={len(items):>3} checking={min(cap,len(items)):>2} deferred={max(0,len(items)-cap):>3}')

    # Run per-source serial (with delay), across sources in parallel
    def run_source(src, items, delay):
        out = []
        for i, (k, s, u) in enumerate(items):
            if i > 0: time.sleep(delay)
            out.append(check_one(k, s, u))
        return out

    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(run_source, src, items, DELAY_SEC_PER_SOURCE.get(src, 1.0))
                   for src, items, _ in plan]
        for f in as_completed(futures):
            results.extend(f.result())

    # Apply verdicts
    killed = []
    killed_urls = []
    rate_limited = []
    alive_count = 0
    unknown_count = 0
    err_count = 0
    iso_now = now.isoformat()
    human_now = now.astimezone().strftime('%Y-%m-%d %H:%M')
    for k, src, url, code, size, title, verdict in results:
        v = state['listings'].get(k)
        if v is None: continue
        if verdict == 'dead':
            v['removed_from_sheet'] = True
            v['stale_marked_at'] = iso_now
            v['removed_human'] = human_now
            v['dead_reason'] = f'status_check title={title[:80]!r} size={size} http={code}'
            killed.append((k, src, title[:60]))
            killed_urls.append((url, human_now))
        elif verdict == 'rate_limited':
            v['rate_limited_at'] = iso_now
            rate_limited.append((k, src))
        elif verdict == 'alive':
            v['last_seen_at'] = iso_now
            v.pop('rate_limited_at', None)
            alive_count += 1
        elif verdict == 'unknown':
            unknown_count += 1
        else:
            err_count += 1

    # Save state
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # Write back to Google Sheets: J="Снят с сайта", K=timestamp
    sheet_updated = 0
    if killed_urls:
        try:
            sheet_updated = mark_dead_in_sheet(killed_urls)
        except Exception as e:
            print(f'  sheet update failed: {e}', file=sys.stderr)

    print(f'\nResult: {len(killed)} killed · {alive_count} alive · {len(rate_limited)} rate-limited · '
          f'{unknown_count} unknown · {err_count} err · sheet_updated={sheet_updated} · {time.time()-t0:.1f}s')
    if killed:
        print('\nKILLED:')
        for k, src, t in killed[:30]:
            print(f'  {src:15} {k[:35]:<35} title={t!r}')
    return {'killed': len(killed), 'alive': alive_count, 'rate_limited': len(rate_limited),
            'unknown': unknown_count, 'err': err_count, 'sheet_updated': sheet_updated}


# Sheet column indices (1-indexed for update_cells).
# Реальная схема (подтверждено 2026-05-14):
#   I (9)  = Дата снятия с сайта  (bot owns — timestamp когда detected)
#   J (10) = Комментарий          (user-only — НИКОГДА не трогаем)
#   K (11) = Статус               (user-only, кроме "Снят с сайта" если пусто)
COL_REMOVAL_DATE = 9   # I
COL_COMMENT      = 10  # J — DO NOT WRITE
COL_STATUS       = 11  # K
COL_MANUAL_COORD = 12  # L — user override: Google Maps URL / "lat,lon"
HEADER_REMOVAL_DATE = 'Removed from site'
HEADER_STATUS = 'Status'
HEADER_MANUAL_COORD = 'Exact location'


def mark_dead_in_sheet(dead_urls):
    """Для каждого URL находит строку в Sheets и пишет:
       I = timestamp (всегда — это служебная колонка бота)
       K = "Снят с сайта" (ТОЛЬКО если ячейка пустая — не перетираем пользовательский статус)
       Колонка J (Комментарий) — НИКОГДА не трогаем.
       Возвращает количество строк, в которые мы реально что-то записали.
    """
    import gen_map
    from sheets_append import update_cells
    sheet = gen_map.fetch_sheet_rows()
    if not sheet:
        print('  sheet empty / unfetchable — skipping writeback', file=sys.stderr)
        return 0

    cells = []
    # Идемпотентно ставим заголовки I1/K1/L1 (Apps Script overwrites — overhead 3 cells/run).
    cells.append({'row': 1, 'col': COL_REMOVAL_DATE, 'value': HEADER_REMOVAL_DATE})
    cells.append({'row': 1, 'col': COL_STATUS,       'value': HEADER_STATUS})
    cells.append({'row': 1, 'col': COL_MANUAL_COORD, 'value': HEADER_MANUAL_COORD})

    touched_rows = 0
    for url, human_ts in dead_urls:
        srow = sheet.get(url)
        if not srow:
            print(f'  no sheet row for dead url: {url[:80]}', file=sys.stderr)
            continue
        row = srow['row']
        current_status = (srow.get('status') or '').strip()
        # K: пишем только если пусто или уже наше же значение
        if not current_status or current_status.lower() in ('снят с сайта', 'снят', 'removed', 'removed from site'):
            cells.append({'row': row, 'col': COL_STATUS, 'value': 'Removed from site'})
        else:
            print(f'  row {row}: keeping user status {current_status!r}, only writing date')
        # I: timestamp (всегда)
        cells.append({'row': row, 'col': COL_REMOVAL_DATE, 'value': human_ts})
        touched_rows += 1

    if touched_rows == 0:
        return 0
    res = update_cells(cells)
    print(f'  sheet writeback: {res}')
    return touched_rows


if __name__ == '__main__':
    main()
