#!/usr/bin/env python3
"""curl_sweep.py — Cluj-Napoca commercial-rent sweep (аренда spații comerciale).

Источники (проверены 2026-07-10):
  • olx.ro        — открытый JSON API. category_id=710 (Birouri și spații comerciale),
                    city_id=52953 (Cluj-Napoca), region_id=2 (jud. Cluj),
                    filter_enum_alege[0]=inchiriere. ~1100 активных лотов.
                    Координаты в map.lat/lon, цена/площадь в params[].
  • storia.ro     — Next.js, __NEXT_DATA__ → props.pageProps.data.searchAds.items[].
                    URL /ro/rezultate/inchiriere/spatiu-comercial/cluj = ВЕСЬ жудец
                    (Florești/Apahida фильтруются дальше по городу/радиусу).
                    location.address.city.name часто = картье (Zorilor, Mănăștur...).
  • imobiliare.ro — DataDome; только curl_cffi impersonate=chrome124.
                    Район и площадь зашиты в slug: ...-cluj-napoca-manastur-94mp-273045215.
                    Цена — из чанка карточки рядом со ссылкой.

Курс RON→EUR ≈ 5.0 (лето 2026); лоты в RON конвертируем.
"""
import json, os, re, subprocess, sys, time
from urllib.parse import quote

VERBOSE = os.environ.get('PIZZA_QUIET') != '1'

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')
RON_PER_EUR = 5.0

STATE_PATH = os.path.join(os.environ.get('CLUJ_DATA', '/Users/dodo/cluj-location-monitor'), 'state.json')
_KEY_PREFIXES = ('olx_', 'storia_', 'imobiliare_')

# --- imobiliare.ro через резидентный прокси -------------------------------
# DataDome банит DC-IP GitHub; с резидентных румынских IP пробивается ~50%
# (замер 13.07.2026) → ретраи ×4 ≈ 94%/стр. Секрет HALO_PROXY общий с
# белградским монитором (DataImpulse), таргетинг подменяем __cr.rs → __cr.ro.
_RES_PROXY = os.environ.get('HALO_PROXY', '').replace('__cr.rs', '__cr.ro')
_imo_sid = [1]  # sticky-sid: инкремент при неудаче = новый exit-IP
_IMO_IMPS = ('chrome124', 'chrome131', 'chrome120', 'safari17_0')


class _ImoResp:
    """Минимальный ответ в форме curl_cffi.Response для вызывающих imo_get."""
    def __init__(self, text, status_code):
        self.text, self.status_code = text, status_code


def imo_get(url, timeout=30, attempts=8):
    # 4→8 попыток 18.07: DataDome ужесточился (~50%→~15% на попытку),
    # пауза между попытками сбивает rate-детект
    """GET imobiliare.ro: ретраи с ротацией exit-IP через прокси (если задан).
    Возвращает Response со status 200, либо последний Response/None."""
    # С 11.09.2026 на раннере imobiliare ходит через настоящий браузер (Camoufox):
    # сайт за Cloudflare + JS-челленджем DataDome, curl_cffi и прокси получают 403.
    import imo_browser
    if imo_browser.enabled():
        html = imo_browser.get_html(url, detail='/oferta/' in url)
        return _ImoResp(html, 200) if html else None
    from curl_cffi import requests as cffi
    last = None
    for i in range(attempts if _RES_PROXY else 1):
        imp = _IMO_IMPS[i % len(_IMO_IMPS)]
        proxies = None
        if _RES_PROXY:
            pr = re.sub(r'^(https?://[^:]+)', rf'\1__sid.imo{_imo_sid[0]}',
                        _RES_PROXY, count=1)
            proxies = {'http': pr, 'https': pr}
        try:
            r = cffi.get(url, impersonate=imp, timeout=timeout, proxies=proxies,
                         allow_redirects=True,
                         headers={'Accept-Language': 'ro-RO,ro;q=0.9'})
            if r.status_code == 200:
                return r
            last = r
        except Exception:
            pass
        _imo_sid[0] += 1
        time.sleep(1.5)
    return last


def _v(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


def _curl(url, timeout=25, extra=None):
    cmd = ['curl', '-sL', '-A', UA, '--compressed', '--max-time', str(timeout),
           '-H', 'Accept-Language: ro-RO,ro;q=0.9,en;q=0.8']
    if extra:
        cmd += extra
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    return r.stdout.decode('utf-8', errors='replace')


def _eur(value, currency):
    """Цена → EUR/мес (int)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if (currency or 'EUR').upper() in ('RON', 'LEI'):
        v = v / RON_PER_EUR
    return int(round(v))


# --------------------------------------------------------------------------
# olx.ro — JSON API
# --------------------------------------------------------------------------
_OLX_IMPS = ('chrome124', 'chrome131', 'chrome120', 'safari17_0')


def olx_get(url, timeout=25, attempts=3, html=False):
    """GET olx API: CloudFront с 04.08.2026 режет голый curl по TLS-отпечатку
    (403 Request blocked) — ходим браузерным отпечатком curl_cffi. Прокси не
    нужен: блок по fingerprint, не по IP."""
    from curl_cffi import requests as cffi
    for i in range(attempts):
        try:
            r = cffi.get(url, impersonate=_OLX_IMPS[i % len(_OLX_IMPS)], timeout=timeout,
                         headers={'Accept': 'application/json',
                                  'Accept-Language': 'ro-RO,ro;q=0.9',
                                  'Referer': 'https://www.olx.ro/'})
            ct = r.headers.get('content-type') or ''
            if r.status_code == 200 and (html or 'json' in ct):
                return r.text
        except Exception:
            pass
        time.sleep(1.0)
    return ''


def sweep_olx(pages=4):
    """40/страницу, сортировка по свежести (sort_by=created_at:desc).
    OLX подмешивает promoted — их id всё равно уникальны, дедуп по state."""
    out = []
    for p in range(pages):
        offset = p * 40
        url = ('https://www.olx.ro/api/v1/offers/?offset=%d&limit=40'
               '&category_id=710&region_id=2&city_id=52953'
               '&filter_enum_alege%%5B0%%5D=inchiriere'
               '&sort_by=created_at%%3Adesc' % offset)
        t0 = time.time()
        body = olx_get(url)
        try:
            data = json.loads(body)
        except Exception:
            _v(f'  olx p{p+1}: JSON parse fail ({len(body)}b)')
            if p == 0:
                raise RuntimeError('olx API returned non-JSON')
            break
        items = data.get('data') or []
        n = 0
        for it in items:
            oid = it.get('id')
            if not oid:
                continue
            price = area = None
            rent = True
            for prm in it.get('params') or []:
                k = prm.get('key')
                val = prm.get('value') or {}
                if k == 'price':
                    price = _eur(val.get('value'), val.get('currency'))
                elif k == 'm':
                    try:
                        area = int(float(str(val.get('key') or '').replace(',', '.')))
                    except (TypeError, ValueError):
                        pass
                elif k == 'alege':
                    rent = (val.get('key') == 'inchiriere')
            if not rent:
                continue
            mp = it.get('map') or {}
            loc = it.get('location') or {}
            out.append({
                'source': 'olx.ro', 'id': str(oid),
                'url': it.get('url'),
                'title': it.get('title') or '',
                'area': area, 'price': price,
                'street': '', 'municipality': (loc.get('city') or {}).get('name') or '',
                'type': None, 'floor': None,
                'lat': mp.get('lat'), 'lon': mp.get('lon'),
                # OLX: продавец скрыл точное место → пин = центр круга ~radius км
                'geo_approx': bool(mp.get('radius')) or mp.get('show_detailed') is False,
                'date': it.get('last_refresh_time') or it.get('created_time') or '',
            })
            n += 1
        _v(f'  olx p{p+1}: {n} listings ({time.time()-t0:.2f}s)')
        if not items:
            break
    return out


# --------------------------------------------------------------------------
# storia.ro — __NEXT_DATA__
# --------------------------------------------------------------------------
def sweep_storia(pages=4):
    """72/страницу с ?limit=72. by=LATEST → свежие первыми (как на otodom;
    если параметр перестанет работать — сортировка дефолтная, поднять pages)."""
    out = []
    for p in range(1, pages + 1):
        url = ('https://www.storia.ro/ro/rezultate/inchiriere/spatiu-comercial/cluj'
               f'?limit=72&page={p}&by=LATEST&direction=DESC')
        t0 = time.time()
        html = _curl(url)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                      html, re.DOTALL)
        if not m:
            _v(f'  storia p{p}: no __NEXT_DATA__ ({len(html)//1024}KB)')
            if p == 1:
                raise RuntimeError('storia: __NEXT_DATA__ not found')
            break
        try:
            data = json.loads(m.group(1))
            ads = data['props']['pageProps']['data']['searchAds']
            items = ads.get('items') or []
        except Exception as e:
            _v(f'  storia p{p}: JSON path fail ({e})')
            if p == 1:
                raise
            break
        n = 0
        for it in items:
            iid = it.get('id')
            slug = it.get('slug')
            if not iid or not slug:
                continue
            tp = it.get('totalPrice') or it.get('rentPrice') or {}
            price = _eur(tp.get('value'), tp.get('currency')) if tp else None
            area = it.get('areaInSquareMeters')
            try:
                area = int(round(float(area))) if area is not None else None
            except (TypeError, ValueError):
                area = None
            addr = ((it.get('location') or {}).get('address') or {})
            city = ((addr.get('city') or {}).get('name') or '').strip()
            street = ((addr.get('street') or {}).get('name') or '').strip()
            out.append({
                'source': 'storia.ro', 'id': str(iid),
                'url': f'https://www.storia.ro/ro/oferta/{slug}',
                'title': it.get('title') or '',
                'area': area, 'price': price,
                'street': street, 'municipality': city,
                'type': None, 'floor': None,
                'lat': None, 'lon': None,
                'date': it.get('dateCreated') or it.get('createdAtFirst') or '',
            })
            n += 1
        _v(f'  storia p{p}: {n} listings ({time.time()-t0:.2f}s, {len(html)//1024}KB)')
        total_pages = ((ads.get('pagination') or {}).get('totalPages')
                       if isinstance(ads, dict) else None)
        if total_pages and p >= total_pages:
            break
    return out


# --------------------------------------------------------------------------
# imobiliare.ro — curl_cffi (DataDome)
# --------------------------------------------------------------------------
IMOB_HREF_RE = re.compile(
    r'href="(?:https://www\.imobiliare\.ro)?(/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-(\d+))"')
IMOB_SLUG_AREA_RE = re.compile(r'-(\d{2,4})mp-\d+$')
IMOB_SLUG_DISTRICT_RE = re.compile(
    r'/oferta/spatiu-comercial-de-inchiriat-cluj-napoca-([a-z0-9\-]+?)-\d{2,4}mp-\d+$')


def _sweep_imobiliare_regex(pages=3):
    from curl_cffi import requests as cffi
    out, seen = [], set()
    for p in range(1, pages + 1):
        # ВАЖНО: ?page=1 отдаёт 404 — первая страница только без параметра
        url = ('https://www.imobiliare.ro/inchirieri-spatii-comerciale/'
               'judetul-cluj/cluj-napoca' + (f'?page={p}' if p > 1 else ''))
        t0 = time.time()
        try:
            # p1 — 8 попыток: DataDome охраняет листинги агрессивнее карточек,
            # 4 ротаций exit-IP порой не хватает (ложный sources_down 2026-07-16)
            r = imo_get(url, timeout=30, attempts=8 if p == 1 else 4)
            if r is None:
                raise RuntimeError('no response after retries')
        except Exception as e:
            _v(f'  imobiliare p{p}: fetch fail {type(e).__name__}')
            if p == 1:
                raise
            break
        if r.status_code != 200:
            _v(f'  imobiliare p{p}: HTTP {r.status_code}')
            if p == 1:
                raise RuntimeError(f'imobiliare HTTP {r.status_code}')
            break
        html = r.text
        matches = list(IMOB_HREF_RE.finditer(html))
        n = 0
        for i, m in enumerate(matches):
            rel, lid = m.group(1), m.group(2)
            if lid in seen:
                continue
            seen.add(lid)
            area = None
            am = IMOB_SLUG_AREA_RE.search(rel)
            if am:
                area = int(am.group(1))
            district = ''
            dm = IMOB_SLUG_DISTRICT_RE.search(rel)
            if dm:
                district = ' '.join(w.capitalize() for w in dm.group(1).split('-'))
            # цена — из чанка HTML между этой ссылкой и следующей
            chunk_end = matches[i + 1].start() if i + 1 < len(matches) else min(
                m.end() + 4000, len(html))
            chunk = html[m.end():chunk_end]
            price = None
            pm = (re.search(r'([\d.,]{3,9})\s*(?:€|EUR)', chunk)
                  or re.search(r'(?:€|EUR)\s*([\d.,]{3,9})', chunk))
            if pm:
                raw = pm.group(1).replace('.', '').replace(',', '.')
                try:
                    price = int(round(float(raw)))
                except ValueError:
                    pass
            out.append({
                'source': 'imobiliare.ro', 'id': lid,
                'url': 'https://www.imobiliare.ro' + rel,
                'title': '',
                'area': area, 'price': price,
                'street': '', 'municipality': district,
                'type': None, 'floor': None,
                'lat': None, 'lon': None, 'date': '',
            })
            n += 1
        _v(f'  imobiliare p{p}: {n} listings ({time.time()-t0:.2f}s, {len(html)//1024}KB)')
        if not matches:
            break
        time.sleep(1.0)  # DataDome не любит бурсты
    return out



# id -> данные карточки из листинга (описание, фото, координаты). Detail-страницы
# imobiliare под DataDome закрыты 100% (пробы 11.09.2026), поэтому cycle.fetch_html
# собирает из этого кеша мини-HTML для parse_imobiliare. Живёт в процессе цикла.
IMO_EMBED = {}
IMO_LIST_URL = 'https://www.imobiliare.ro/inchirieri-spatii-comerciale/judetul-cluj/cluj-napoca'


def _imo_page_data(html):
    """(listings, jsonld_by_id, searchMeta) из отрендеренного листинга (Inertia data-page + JSON-LD)."""
    import html as _H
    m = re.search(r'id="app" data-page="([^"]+)"', html)
    if not m:
        raise ValueError('no data-page')
    dp = json.loads(_H.unescape(m.group(1)))
    props = dp.get('props') or {}
    listings = []
    for s in props.get('sections') or []:
        if s.get('type') == 'results-list':
            listings = (s.get('data') or {}).get('listings') or []
    ld = {}
    lm = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    if lm:
        try:
            for x in json.loads(lm.group(1)).get('@graph') or []:
                if x.get('@type') == 'SearchResultsPage':
                    for el in (x.get('mainEntity') or {}).get('itemListElement') or []:
                        it = el.get('item') or {}
                        im = re.search(r'item-(\d+)', it.get('@id') or '')
                        if im:
                            ld[im.group(1)] = it
        except Exception:
            pass
    return listings, ld, props.get('searchMeta') or {}


def _imo_candidates(listings, ld, coords):
    """Карточки spatiu-comercial -> кандидаты свипа; попутно наполняет IMO_EMBED."""
    import html as _H
    out = []
    for L in listings:
        rel = L.get('url') or ''
        lid = str(L.get('id') or '')
        if 'spatiu-comercial-de-inchiriat' not in rel or not lid:
            continue
        it = ld.get(lid) or {}
        area = None
        for hl in L.get('highlights') or []:
            if hl.get('key') == 'total_usable_surface':
                mm = re.search(r'(\d+(?:[.,]\d+)?)', hl.get('label') or '')
                if mm:
                    # румынский формат: точка разделяет тысячи, запятая дробную часть
                    area = int(round(float(mm.group(1).replace('.', '').replace(',', '.'))))
        if area is None:
            am = IMOB_SLUG_AREA_RE.search(rel)
            area = int(am.group(1)) if am else None
        price = None
        ps = (it.get('offers') or {}).get('priceSpecification') or {}
        if ps.get('price') is not None:
            try:
                price = float(ps['price'])
                if (ps.get('priceCurrency') or 'EUR').upper() == 'RON':
                    price = price / RON_PER_EUR
                price = int(round(price))
            except (TypeError, ValueError):
                price = None
        if price is None:
            pm = re.search(r'([\d.,]+)', L.get('price') or '')
            if pm:
                try:
                    price = int(round(float(pm.group(1).replace('.', '').replace(',', '.'))))
                    if 'lei' in (L.get('price') or '').lower():
                        price = int(round(price / RON_PER_EUR))
                except ValueError:
                    price = None
        district = ''
        dm = IMOB_SLUG_DISTRICT_RE.search(rel)
        if dm:
            district = ' '.join(w.capitalize() for w in dm.group(1).split('-'))
        elif L.get('location'):
            district = L['location'].split(',')[0].strip()
        lat, lon = coords.get(lid, (None, None))
        desc = _H.unescape(it.get('description') or '') or (L.get('descriptionPreview') or '')
        imgs = [x.get('src') for x in (L.get('images') or []) if isinstance(x, dict) and x.get('src')]
        IMO_EMBED[lid] = {'title': L.get('title') or it.get('name') or '', 'description': desc,
                          'images': imgs, 'lat': lat, 'lon': lon}
        out.append({
            'source': 'imobiliare.ro', 'id': lid,
            'url': ('https://www.imobiliare.ro' + rel) if rel.startswith('/') else rel,
            'title': L.get('title') or '', 'area': area, 'price': price,
            'street': '', 'municipality': district, 'type': None, 'floor': None,
            'lat': lat, 'lon': lon, 'date': '',
        })
    return out


def sweep_imobiliare(pages=6):
    """imobiliare через браузер (IMO_BROWSER=1): карточки из JSON листинга, без detail.
    Без браузера или если JSON не разобрался на стр.1 — старый регэксп-путь."""
    import imo_browser
    if not imo_browser.enabled():
        return _sweep_imobiliare_regex(pages)
    out, seen, last_page = [], set(), None
    for p in range(1, pages + 1):
        if last_page and p > last_page:
            break
        url = IMO_LIST_URL + (f'?page={p}' if p > 1 else '')  # ?page=1 -> 404
        t0 = time.time()
        html = imo_browser.get_html(url)
        if not html:
            if p == 1:
                raise RuntimeError('imobiliare listing blocked')
            _v(f'  imobiliare p{p}: blocked, stop')
            break
        try:
            listings, ld, meta = _imo_page_data(html)
        except Exception as e:
            _v(f'  imobiliare p{p}: embedded parse fail {type(e).__name__}')
            if p == 1:
                return _sweep_imobiliare_regex(pages)
            break
        last_page = meta.get('lastPage') or last_page
        n = 0
        for c in _imo_candidates(listings, ld, imo_browser.coords()):
            if c['id'] in seen:
                continue
            seen.add(c['id'])
            out.append(c)
            n += 1
        _v(f'  imobiliare p{p}: {n} listings (embedded, {len(listings)} cards on page, '
           f'lastPage={last_page}, {time.time()-t0:.1f}s)')
    with_geo = sum(1 for c in out if c.get('lat') is not None)
    _v(f'  imobiliare: {len(out)} commercial listings, {with_geo} with map coordinates')
    return out


def known_ids():
    s = json.load(open(STATE_PATH))
    out = set()
    for k in s.get('listings', {}):
        out.add(k)
        for pref in _KEY_PREFIXES:
            if k.startswith(pref):
                out.add(k[len(pref):])
                break
    return out


def main():
    a = sweep_olx(pages=4)
    st = sweep_storia(pages=4)
    im = sweep_imobiliare(pages=3)
    allr = a + st + im
    print(json.dumps({'olx': len(a), 'storia': len(st), 'imobiliare': len(im),
                      'total': len(allr)}, ensure_ascii=False))
    return allr


if __name__ == '__main__':
    main()
