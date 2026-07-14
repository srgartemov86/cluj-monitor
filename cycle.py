#!/usr/bin/env python3
"""
cycle.py — Cluj-Napoca location-monitor cycle (клон белградского pizzeria-monitor).
Источники: olx.ro / storia.ro / imobiliare.ro. Reduces ~30 agent tool-calls/cycle to 2.

Usage:
  python3 cycle.py                          # phase 1: sweep + filter + state-updates-for-rejects
                                            # + photo download for passes; outputs JSON
  python3 cycle.py --mark-sent KEY MSG_ID   # called by agent after Telegram send_file
                                            # succeeds; updates state + inserts to Sheets
  python3 cycle.py --finalize               # phase 2: check_status + gen_map + runs.log
"""
import argparse, fcntl, json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote
from math import radians, sin, cos, asin, sqrt

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Suppress per-page pagination prints in sweep (failure lines still appear).
os.environ.setdefault('PIZZA_QUIET', '1')
import curl_sweep
from sheets_append import insert_lots, update_cells, _sheets_service, SPREADSHEET_ID
import scoring

# В Клуже пиццерий Dodo пока нет — штраф каннибализации не применяется.
DODO_POINTS = []


def score_and_cache(rec):
    """Считает скоринг локации для прошедшего лота и кладёт в rec (кэш в state).
    Возвращает dict скоринга или None (нет координат / Overpass недоступен)."""
    lat, lon = rec.get('geo_lat'), rec.get('geo_lon')
    if lat is None or lon is None:
        return None
    try:
        sc = scoring.score_location(lat, lon, dodo_points=DODO_POINTS)
    except Exception:
        return None
    if sc.get('score') is None:
        return None
    sc['geo_source'] = rec.get('geo_source')  # точность координат → достоверность скоринга
    rec['score'] = sc['score']
    rec['score_data'] = sc
    rec['scored_at'] = now_iso()
    return sc

STATE_PATH = os.path.join(os.environ.get('CLUJ_DATA', '/Users/dodo/cluj-location-monitor'), 'state.json')
RUNS_LOG = os.path.join(os.environ.get('CLUJ_DATA', '/Users/dodo/cluj-location-monitor'), 'runs.log')
PHOTO_DIR = Path(os.getcwd()) / '.cluj-photos'
CHAT_ID = 3828339567  # supergroup (migrated 2026-07-10 from basic group 5328997952)

AREA_MIN, AREA_MAX = 100, 220
PRICE_MIN, PRICE_MAX = 1200, 5000
PRICE_PER_M2_MIN = 10.0  # €/м²: центр Клужа ~10–18 €/м²/мес; дешевле — окраина/качество
CEILING_MIN = 3.0
TRG_LAT, TRG_LON = 46.7694, 23.5893  # Piața Unirii
RADIUS_KM = 6.0
MAX_DETAILS_PER_CYCLE = 30
MAX_DETAILS_PER_SOURCE = 10
# Пригороды-спальники за границей города (задача = только Cluj-Napoca).
# Florești формально 7-8 км от Unirii — радиус его тоже режет, slug для надёжности.
ZEMUN_SLUGS = ('floresti', 'baciu', 'apahida', 'sannicoara')

# Дальние города/коммуны жудеца (storia отдаёт весь jud. Cluj) — тихий реджект.
FAR_MUNI_SLUGS = ('turda', 'dej', 'campia-turzii', 'gherla', 'huedin', 'gilau',
                  'feleacu', 'chinteni', 'jucu', 'dezmir', 'apahida', 'floresti',
                  'baciu', 'savadisla', 'tureni', 'aiton', 'cojocna')
# «Near-miss» коридор цены для листа реджектов: рядом с целевым 1200–5000.
PRICE_NEAR_LOW, PRICE_NEAR_HIGH = 900, 6000

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# Description-level reject patterns (румынский; диакритика опциональна — многие
# объявления пишут без ă/â/î/ș/ț, поэтому символ-классы [ăa] и т.д.)
OFFICE_PATS = [
    r'cl[ăa]dire\s+de\s+birouri', r'spa[țt]i[iu]+\s+(?:de\s+)?birou',
    r'\bbirouri\b', r'\bopen[\s-]?space\b', r'business\s+cent(?:er|re)',
    r'\bcorp\s+de\s+birouri', r'imobil\s+de\s+birouri',
]
MALL_PATS = [
    r'centru[l]?\s+comercial', r'\bmall\b', r'galeri[ei]\s+comercial',
    r'\bIulius\b', r'\bVIVO\b', r'\bPlatinia\b', r'shopping\s+cent(?:er|re)',
]
COURT_PATS = [r'curte[a]?\s+interioar[ăa]', r'[îi]n\s+curtea\s+(?:blocului|imobilului|cl[ăa]dirii)',
              r'acces\s+(?:doar\s+)?prin\s+curte', r'f[ăa]r[ăa]\s+vad']
DARK_PATS = [r'\bsubsol\b', r'\bdemisol\b',
             r'f[ăa]r[ăa]\s+(?:lumin[ăa]\s+natural[ăa]|ferestre|geamuri)']

# Apartment detection: жилые квартиры протекают в commercial-выдачу (хозяева
# сдают «под офис/кабинет»). Реджект если жилые маркеры есть И нет коммерческого
# контр-маркера.
RESIDENTIAL_PATS = [
    r'\bapartament', r'\bgarsonier', r'\bdormito(?:r|are)',
    r'\b(?:semi)?decomandat', r'\bcamer[ăa]\s+de\s+zi', r'living\s+(?:si|și)\s+',
    r'ideal\s+pentru\s+locuit', r'\bde\s+locuit\b',
]
# Коммерческие контр-маркеры: если есть — лот настоящий commercial, оставляем
# даже при жилом слове во flavour-тексте.
COMMERCIAL_KEEP = [
    r'spa[țt]iu\s+comercial', r'\bvad(?:\s+comercial|\s+bun|\s+pietonal)?\b',
    r'vitrin[ăa]', r'\bstradal', r'\bla\s+strad[ăa]\b', r'restaurant',
    r'pizzeri', r'fast\s*food', r'cafenea', r'coffee', r'brut[ăa]rie',
    r'patiserie', r'cofet[ăa]rie', r'\bmagazin\b', r'showroom', r'horeca',
    r'alimenta[țt]ie\s+public[ăa]', r'\bteras[ăa]', r'\bsalon\b',
    r'farmaci[ei]', r'\bcomer[țt]\b', r'activit[ăa][țt]i\s+comerciale',
]

# Картье (районы) Клуж-Напоки: канон. название + центроид (для district по
# координатам OLX и для карты). Центроиды сняты с OSM, точность ~300 м — ок для района.
CARTIERE = {
    'centru': ('Centru', 46.7699, 23.5899),
    'marasti': ('Mărăști', 46.7815, 23.6110),
    'gheorgheni': ('Gheorgheni', 46.7676, 23.6249),
    'zorilor': ('Zorilor', 46.7539, 23.5964),
    'manastur': ('Mănăștur', 46.7570, 23.5533),
    'grigorescu': ('Grigorescu', 46.7659, 23.5501),
    'iris': ('Iris', 46.7963, 23.6083),
    'gruia': ('Gruia', 46.7793, 23.5748),
    'andrei-muresanu': ('Andrei Mureșanu', 46.7570, 23.6070),
    'dambul-rotund': ('Dâmbul Rotund', 46.7861, 23.5741),
    'bulgaria': ('Bulgaria', 46.7822, 23.6004),
    'someseni': ('Someșeni', 46.7791, 23.6600),
    'intre-lacuri': ('Între Lacuri', 46.7746, 23.6301),
    'plopilor': ('Plopilor', 46.7666, 23.5698),
    'europa': ('Europa', 46.7473, 23.5794),
    'buna-ziua': ('Bună Ziua', 46.7442, 23.6046),
    'borhanci': ('Borhanci', 46.7502, 23.6420),
    'faget': ('Făget', 46.7280, 23.5860),
    'becas': ('Becaș', 46.7530, 23.6280),
    'central': ('Centru', 46.7699, 23.5899),  # imobiliare slug-вариант
    'semicentral': ('Semicentral', 46.7730, 23.5960),
    'ultracentral': ('Centru', 46.7699, 23.5899),
}

# Топоним → картье (нижний регистр, без диакритики) — для district из текста.
SUBDISTRICT_TO_MUNI = {
    'piata unirii': 'Centru', 'piata mihai viteazul': 'Centru',
    'piata muzeului': 'Centru', 'piata avram iancu': 'Centru',
    'eroilor': 'Centru', 'memorandumului': 'Centru', 'napoca': 'Centru',
    'horea': 'Centru', 'regele ferdinand': 'Centru', 'motilor': 'Centru',
    'piata cipariu': 'Andrei Mureșanu', 'titulescu': 'Gheorgheni',
    'observatorului': 'Zorilor', 'calea turzii': 'Zorilor',
    'calea manastur': 'Mănăștur', 'calea floresti': 'Mănăștur',
    'calea dorobantilor': 'Mărăști', 'piata marasti': 'Mărăști',
    'aurel vlaicu': 'Mărăști', 'fabricii': 'Mărăști',
    'baisoara': 'Gheorgheni', 'interservisan': 'Gheorgheni',
    'horticultorilor': 'Iris', 'oasului': 'Iris', 'corneliu coposu': 'Iris',
}


def _norm_sub(s):
    """Lowercase, strip diacritics для матча в SUBDISTRICT_TO_MUNI.
    Đ/đ нужно мапить вручную — NFKD их теряет."""
    import unicodedata
    s = s.replace('Đ', 'D').replace('đ', 'd')
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[()]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


class StateLock:
    """Advisory file lock — second concurrent cycle exits cleanly with empty result."""
    def __init__(self, path=STATE_PATH + '.lock'):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, 'w')
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fd.close()
            print(json.dumps({
                'concurrent_cycle_running': True,
                'passes': [], 'rejects': [], 'summary': {},
            }))
            sys.exit(0)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        finally:
            self.fd.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(s):
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def decode_escapes(s):
    """Decode JSON-style \\uXXXX escapes while preserving raw UTF-8 multibyte chars."""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        pass
    try:
        return bytes(s, 'utf-8').decode('unicode_escape', errors='ignore')
    except Exception:
        return s


def fetch_html(url, timeout=20):
    """Returns (html, http_status). http_status=0 on error."""
    if 'imobiliare.ro' in url:
        # imo_get: ретраи + резидентный прокси (HALO_PROXY→ro) против DataDome
        r = curl_sweep.imo_get(url, timeout=timeout)
        return (r.text, r.status_code) if r is not None else ('', 0)
    try:
        r = subprocess.run(
            ['curl', '-sL', '-A', UA, '--max-time', str(timeout), '--compressed',
             '-w', '\n__HTTP_CODE__%{http_code}', url],
            capture_output=True, timeout=timeout + 5,
        )
        body = r.stdout.decode('utf-8', errors='replace')
        idx = body.rfind('\n__HTTP_CODE__')
        if idx >= 0:
            code = body[idx + len('\n__HTTP_CODE__'):].strip()
            body = body[:idx]
            try:
                return body, int(code)
            except ValueError:
                return body, 0
        return body, 200
    except Exception:
        return '', 0


def _floor_from_text(text):
    """Этаж из румынского описания. '' если не найден."""
    t = text.lower()
    if re.search(r'\bdemisol\b', t):
        return 'demisol'
    if re.search(r'\bsubsol\b', t):
        return 'subsol'
    if re.search(r'\bmezanin\b', t):
        return 'mezanin'
    if re.search(r'\bmansard[ăa]\b', t):
        return 'mansarda'
    if re.search(r'parter\s+[îi]nalt', t):
        return 'parter inalt'
    if re.search(r'\bla\s+parter\b|\bparter\b', t):
        return 'parter'
    fm = (re.search(r'\bla\s+etajul?\s+(\d+)', t)
          or re.search(r'\betaj(?:ul)?\s+(\d+)\b', t)
          or re.search(r'\betaj\s+(\d+)\s*/', t))
    if fm:
        return f'etaj {fm.group(1)}'
    return ''


def _ceiling_from_text(text):
    t = text.lower()
    cm = (re.search(r'[îi]n[ăa]l[țt]ime[a]?[^0-9]{0,30}(\d[\.,]?\d*)\s*m', t)
          or re.search(r'\bh\s*[=:]\s*(\d[\.,]?\d*)\s*m', t)
          or re.search(r'tavan[e]?[^0-9]{0,25}(\d[\.,]?\d*)\s*m', t))
    if cm:
        try:
            v = float(cm.group(1).replace(',', '.'))
            if 2.0 <= v <= 8.0:  # защита от «înălțime 2026» и площадей
                return v
        except Exception:
            pass
    return None


def parse_olx(html, cand):
    """OLX detail: описание в __PRERENDERED_STATE__ (JSON-escaped), фото apollo.olxcdn,
    координаты уже есть в sweep (map.lat/lon)."""
    out = {'title': cand.get('title') or '', 'street': '', 'subdistrict': ''}

    # Самое длинное description из embedded JSON (skip шаблонный мусор)
    longest = ''
    for d in re.findall(r'"description":"((?:[^"\\]|\\.)*)"', html):
        s = decode_escapes(d)
        if len(s) > len(longest):
            longest = s
    out['description'] = re.sub(r'<[^>]+>', ' ', longest)
    out['description'] = re.sub(r'\s+', ' ', out['description']).strip()

    if cand.get('lat') is not None and cand.get('lon') is not None:
        out['lat'] = float(cand['lat'])
        out['lon'] = float(cand['lon'])
        out['geo_approx'] = bool(cand.get('geo_approx'))

    if cand.get('date'):
        out['refreshed_at'] = cand['date']

    og = re.search(r'og:image"\s+content="([^"]+)"', html) \
        or re.search(r'<meta property="og:image" content="([^"]+)"', html)
    out['photo_url'] = og.group(1) if og else None

    haystack = out['description'] + ' ' + out['title']
    f = _floor_from_text(haystack)
    if f:
        out['floor'] = f
    c = _ceiling_from_text(haystack)
    if c:
        out['ceiling'] = c
    return out


def parse_storia(html, cand):
    """Storia detail: __NEXT_DATA__ → props.pageProps.ad. Координаты, описание,
    images[], characteristics (m, price), createdAt/modifiedAt."""
    out = {'title': cand.get('title') or '', 'description': '', 'street': '',
           'subdistrict': ''}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return out
    try:
        ad = json.loads(m.group(1))['props']['pageProps'].get('ad') or {}
    except Exception:
        return out

    out['title'] = ad.get('title') or out['title']
    desc = ad.get('description') or ''
    desc = re.sub(r'<[^>]+>', ' ', desc)
    out['description'] = re.sub(r'\s+', ' ', desc).strip()

    coords = (ad.get('location') or {}).get('coordinates') or {}
    lat, lon = coords.get('latitude'), coords.get('longitude')
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        out['lat'], out['lon'] = float(lat), float(lon)

    addr = ((ad.get('location') or {}).get('address') or {})
    street = ((addr.get('street') or {}).get('name') or '').strip()
    out['street'] = street or (cand.get('street') or '')
    out['subdistrict'] = ((addr.get('city') or {}).get('name') or '').strip()

    if ad.get('modifiedAt'):
        out['refreshed_at'] = ad['modifiedAt']
    if ad.get('createdAt'):
        out['published_at'] = ad['createdAt']

    imgs = ad.get('images') or []
    if imgs:
        i0 = imgs[0]
        out['photo_url'] = i0.get('large') or i0.get('medium') or i0.get('thumbnail')

    # floor: characteristics.floor_no ("parter"/"1"), иначе текст
    chars = {c.get('key'): (c.get('value') or c.get('localizedValue') or '')
             for c in (ad.get('characteristics') or [])}
    fl = str(chars.get('floor_no') or '').lower().strip()
    if fl:
        if 'parter' in fl or fl in ('ground_floor', 'ground'):
            out['floor'] = 'parter'
        elif fl.lstrip('floor_').isdigit():
            out['floor'] = f"etaj {fl.lstrip('floor_')}"
        else:
            out['floor'] = fl
    if not out.get('floor'):
        f = _floor_from_text(out['description'] + ' ' + out['title'])
        if f:
            out['floor'] = f
    c = _ceiling_from_text(out['description'])
    if c:
        out['ceiling'] = c
    return out


def parse_imobiliare(html, cand):
    """imobiliare.ro detail (curl_cffi): описание из JSON-LD/og, координаты из
    embedded 'latitude'. Площадь/район приходят из slug ещё в sweep."""
    out = {'title': '', 'street': '', 'subdistrict': ''}
    tm = re.search(r'<title>([^<]+)</title>', html)
    out['title'] = tm.group(1).strip() if tm else ''

    # самое длинное description-поле из embedded JSON
    longest = ''
    for d in re.findall(r'"description":\s*"((?:[^"\\]|\\.)*)"', html):
        s = decode_escapes(d)
        if len(s) > len(longest):
            longest = s
    if not longest:
        ogd = re.search(r'og:description"\s+content="([^"]+)"', html)
        longest = ogd.group(1) if ogd else ''
    longest = re.sub(r'<[^>]+>', ' ', longest)
    out['description'] = re.sub(r'\s+', ' ', longest).strip()

    lat = re.search(r'"latitude"\s*:\s*"?([\d.]+)"?', html)
    lon = re.search(r'"longitude"\s*:\s*"?([\d.]+)"?', html)
    if lat and lon:
        try:
            la, lo = float(lat.group(1)), float(lon.group(1))
            if 46.0 < la < 47.5 and 23.0 < lo < 24.2:  # sanity: границы жудеца
                out['lat'], out['lon'] = la, lo
        except ValueError:
            pass

    og = re.search(r'og:image"\s+content="([^"]+)"', html) \
        or re.search(r'<meta property="og:image" content="([^"]+)"', html)
    out['photo_url'] = og.group(1) if og else None

    haystack = out['description'] + ' ' + out['title']
    f = _floor_from_text(haystack)
    if f:
        out['floor'] = f
    c = _ceiling_from_text(haystack)
    if c:
        out['ceiling'] = c
    return out


def _district_by_coords(lat, lon):
    """Ближайший картье по центроиду (≤2.5 км, иначе '')."""
    if lat is None or lon is None:
        return ''
    best, best_d = '', 99
    for _slug, (name, clat, clon) in CARTIERE.items():
        d = haversine_km(lat, lon, clat, clon)
        if d < best_d:
            best, best_d = name, d
    return best if best_d <= 2.5 else ''


def _district_from_text(text):
    """Картье из текста (название района или топоним-улица)."""
    norm = _norm_sub(text)
    for slug, (name, _la, _lo) in CARTIERE.items():
        if slug.replace('-', ' ') in norm:
            return name
    for topo, name in SUBDISTRICT_TO_MUNI.items():
        if topo in norm:
            return name
    return ''


def extract_district(cand, detail):
    src = cand.get('source')

    if src == 'imobiliare.ro':
        # район зашит в slug URL: ...cluj-napoca-manastur-94mp-123
        m = re.search(r'cluj-napoca-([a-z0-9\-]+?)-\d{2,4}mp-\d+$', cand.get('url', ''))
        if m:
            slug = m.group(1)
            if slug in CARTIERE:
                return CARTIERE[slug][0]
            return ' '.join(w.capitalize() for w in slug.split('-'))

    if src == 'storia.ro':
        # location.address.city.name у storia часто = картье (Zorilor, Mănăștur)
        sub = (detail.get('subdistrict') or cand.get('municipality') or '').strip()
        if sub and sub.lower() not in ('cluj-napoca', 'cluj napoca', 'cluj'):
            d = _district_from_text(sub)
            return d or sub

    # OLX и фолбэки: координаты → ближайший центроид; затем текст
    lat = detail.get('lat', cand.get('lat'))
    lon = detail.get('lon', cand.get('lon'))
    d = _district_by_coords(lat, lon)
    if d:
        return d
    d = _district_from_text((cand.get('title') or '') + ' ' +
                            (detail.get('description') or '')[:500])
    if d:
        return d
    return 'Unknown'


def extract_photo_urls(html, source, max_n=10):
    """Все фото объявления (hotlink-able URLs) для галереи на карте."""
    urls = []
    try:
        if source == 'olx.ro':
            cand = re.findall(
                r'https://[a-z]+\.apollo\.olxcdn\.com(?::443)?/v1/files/[A-Za-z0-9_\-]+-RO/image(?:;s=\d+x\d+)?',
                html)
            urls = list(dict.fromkeys(cand))
        elif source == 'storia.ro':
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m:
                ad = json.loads(m.group(1))['props']['pageProps'].get('ad') or {}
                for im in ad.get('images') or []:
                    # medium first: large (2000px+) грузится секундами в попапе карты
                    u = im.get('medium') or im.get('large') or im.get('thumbnail')
                    if u:
                        urls.append(u)
        elif source == 'imobiliare.ro':
            cand = re.findall(r'https://[a-z0-9.\-]+/(?:image|images|photos)[^"\\\s]+?\.(?:jpe?g|webp)[^"\\\s]*', html)
            og = re.search(r'og:image"\s+content="([^"]+)"', html)
            if og:
                cand = [og.group(1)] + cand
            urls = list(dict.fromkeys(cand))
    except Exception:
        pass
    # Canonical dedup: OLX отдаёт один файл в нескольких размерах
    # (…/image, …/image;s=1271x962, …;s=389x272) — по полному URL они разные,
    # но это одно фото. Дедуп по базе, обрезав размерный/качественный суффикс.
    seen, out = set(), []
    for u in urls:
        base = re.sub(r';s=\d+x\d+(?:;q=\d+)?$', '', u)
        if base in seen:
            continue
        seen.add(base)
        out.append(u)
    return out[:max_n]


def apply_filters(cand, detail, skip_price=False):
    """Returns (passed_bool, flags_list, reject_reason).

    skip_price=True пропускает area/price/€m² гейты и проверяет только структуру
    (этаж/zemun/назначение/офис/молл/двор/квартира). Нужно price-only пути, чтобы
    перед публикацией price-реджекта в дайджест отсеять структурный брак
    (1.+ sprat, стан, офис) — иначе в дайджест попадают квартиры на верхних этажах."""
    flags = []
    a = cand.get('area')
    p = cand.get('price')
    if not skip_price:
        if a is None or not (AREA_MIN <= a <= AREA_MAX):
            return False, flags, f'area={a}'
        if p is None or not (PRICE_MIN <= p <= PRICE_MAX):
            return False, flags, f'price={p}'
        if a and p / a < PRICE_PER_M2_MIN:
            return False, flags, f'price_per_m2={p/a:.1f}'

    floor = ((detail.get('floor') or cand.get('floor') or '')).lower().strip().rstrip('.')
    if not floor:
        flags.append('uncertain_floor')
    elif floor == 'parter':
        pass
    elif 'inalt' in floor or 'înalt' in floor:  # parter înalt ≈ visoko prizemlje
        return False, flags, f'floor={floor}'
    elif (re.search(r'etaj\s*\d+', floor) or re.search(r'^[1-9]$', floor)
          or floor in ('demisol', 'subsol', 'mezanin', 'mansarda', 'penthouse')):
        return False, flags, f'floor={floor}'

    url = (cand.get('url') or '').lower()
    if any(s in url for s in ZEMUN_SLUGS):
        return False, flags, 'suburb'
    muni_norm = _norm_sub(cand.get('municipality') or '').replace(' ', '-')
    if muni_norm and any(s in muni_norm for s in ZEMUN_SLUGS):
        return False, flags, 'suburb'

    type_ = (cand.get('type') or '').lower()
    if 'birou' in type_ or 'depozit' in type_ or 'industrial' in type_ or 'hala' in type_:
        return False, flags, f'type={type_[:30]}'

    haystack = ((detail.get('description', '') or '') + ' ' +
                (detail.get('title', '') or '')).lower()

    for pat in OFFICE_PATS:
        if re.search(pat, haystack):
            return False, flags, f'office:{pat[:25]}'
    for pat in MALL_PATS:
        if re.search(pat, haystack):
            return False, flags, 'mall'
    for pat in COURT_PATS:
        if re.search(pat, haystack):
            return False, flags, 'courtyard'
    for pat in DARK_PATS:
        if re.search(pat, haystack):
            return False, flags, f'dark:{pat[:25]}'

    if (any(re.search(p, haystack) for p in RESIDENTIAL_PATS)
            and not any(re.search(m, haystack) for m in COMMERCIAL_KEEP)):
        return False, flags, 'apartment'

    # фильтр по потолку убран 2026-07-11 по решению Сергея;
    # высота, если указана, по-прежнему показывается в caption (✅ ceiling X m)

    if 'lat' in detail and 'lon' in detail:
        d = haversine_km(detail['lat'], detail['lon'], TRG_LAT, TRG_LON)
        if d > RADIUS_KM:
            return False, flags, f'distance={d:.1f}km'
    else:
        flags.append('uncertain_distance')

    return True, flags, ''


def download_photo(url, listing_key):
    PHOTO_DIR.mkdir(exist_ok=True)
    ext_m = re.search(r'\.(jpe?g|webp|avif|png)(?:[?#]|$)', url, re.IGNORECASE)
    ext = (ext_m.group(1).lower() if ext_m else 'jpg').replace('jpeg', 'jpg')
    raw_path = PHOTO_DIR / f"{listing_key}.{ext}"
    try:
        subprocess.run(['curl', '-sL', '-A', UA, '--max-time', '30', url,
                        '-o', str(raw_path)],
                       capture_output=True, timeout=35)
    except Exception:
        return None
    if not raw_path.exists() or raw_path.stat().st_size < 1000:
        return None
    if ext in ('webp', 'avif', 'png'):
        jpg = PHOTO_DIR / f"{listing_key}.jpg"
        try:
            if sys.platform == 'darwin':
                subprocess.run(['sips', '-s', 'format', 'jpeg', str(raw_path),
                                '--out', str(jpg)],
                               capture_output=True, timeout=10)
            else:  # Linux (GitHub Actions): sips нет — Pillow
                from PIL import Image
                Image.open(raw_path).convert('RGB').save(jpg, 'JPEG', quality=88)
            if jpg.exists() and jpg.stat().st_size > 1000:
                return jpg
        except Exception:
            pass
    return raw_path


def yandex_pano_url(lat, lon):
    """Google Street View в точке (в Румынии покрытие хорошее; Яндекс-панорам нет).
    Имя функции сохранено ради вызовов, унаследованных от белградского кода."""
    return (f"https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={lat}%2C{lon}")


def yandex_search_url(addr):
    return f"https://www.google.com/maps/search/?api=1&query={quote(f'{addr}, Cluj-Napoca, Romania')}"


def reason_ru(reason):
    """Reject reason code → (English label, badge color). Имя функции оставлено
    ради унаследованных вызовов; тексты — английские (команда партнёра)."""
    r = reason or ''
    if r.startswith('floor='):
        return (f'not ground floor ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('distance='):
        return (f'too far from center ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('ceiling='):
        return (f'low ceiling ({r.split("=", 1)[1]})', '#d97706')
    if r.startswith('price_per_m2='):
        return (f'too cheap per m² ({r.split("=", 1)[1]})', '#d97706')
    if r.startswith('area='):
        return (f'area outside 100–220 ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('price='):
        return (f'price outside 1200–5000 ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('office:'):
        return ('office / business center', '#7c3aed')
    if r == 'mall':
        return ('inside shopping mall', '#7c3aed')
    if r == 'courtyard':
        return ('courtyard entrance', '#7c3aed')
    if r.startswith('dark:'):
        return ('no windows / basement', '#374151')
    if r == 'apartment':
        return ('residential, not commercial', '#7c3aed')
    if r == 'suburb':
        return ('suburb (out of zone)', '#dc2626')
    if r.startswith('type='):
        return ('office / warehouse', '#7c3aed')
    if r.startswith('fetch_fail'):
        return ('page failed to load', '#9ca3af')
    if r == 'district_blacklist':
        return ('blacklisted district', '#dc2626')
    return (r or 'rejected', '#6b7280')


def _esc_html(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _reject_map_links(r):
    """(google_url, yandex_url) для реджекта по координатам или адресу."""
    lat, lon = r.get('lat'), r.get('lon')
    if lat and lon:
        return (f"https://www.google.com/maps/?q={lat},{lon}", yandex_pano_url(lat, lon))
    addr = r.get('address') or ''
    q = quote(f"{addr}, Cluj-Napoca, Romania")
    return (f"https://www.google.com/maps/search/?api=1&query={q}", yandex_search_url(addr))


def build_reject_digest(rejects):
    """HTML-сообщение по новым лотам, не прошедшим фильтр (одной строкой причина).
    Реджекты в карту/таблицу НЕ попадают — это только обзор «текущей картины»."""
    items = list(rejects or [])
    if not items:
        return None

    # Сортировка по €/м² от высокой к низкой (дорогие за метр — выше).
    def _ppm(r):
        a, p = r.get('area'), r.get('price')
        return (p / a) if (a and p) else -1
    items.sort(key=_ppm, reverse=True)

    n = len(items)

    def word(k):
        return 'лот' if k % 10 == 1 and k % 100 != 11 else \
               ('лота' if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14 else 'лотов')

    lines = [f"🔍 <b>Не прошли фильтр: {n} {word(n)}</b> · не в таблице/карте"]
    shown = 0
    for i, r in enumerate(items, 1):
        label, _ = reason_ru(r['reason'])
        a, p = r.get('area'), r.get('price')
        ppm = f"{p / a:.0f} €/м²" if (a and p) else '—'
        district = (r.get('district') or '').strip()
        address = (r.get('address') or '').strip()
        if district.lower() in ('unknown', 'unknown district', '—'):
            district = ''
        title = address or district or '—'
        suffix = ''
        if district and address and district.lower() not in address.lower():
            suffix = f" <i>({_esc_html(district)})</i>"
        url = _esc_html(r.get("url") or '')
        head = f'<a href="{url}"><b>{_esc_html(title)}</b></a>' if url else f"<b>{_esc_html(title)}</b>"
        sc = r.get('score')
        score_str = f" · {scoring.score_emoji(sc)} {sc}/100" if sc is not None else " · ❔ скоринг —"
        block = (f"{i}. {head}{suffix} · "
                 f"{a or '—'}м² · {p or '—'}€ · {ppm}{score_str} ❌ {_esc_html(label)}")
        # safety: держим сообщение под лимитом Telegram (4096)
        if sum(len(x) + 1 for x in lines) + len(block) > 3900 and shown:
            lines.append(f"…ещё {n - shown}, см. следующий прогон")
            break
        lines.append(block)
        shown += 1

    return {'message': '\n'.join(lines), 'count': n}


def write_rejects_to_sheet(rejects):
    """Записать не прошедшие фильтр лоты в лист «не прошли фильтр» (gid 1460013302)
    через Sheets API. Раньше они уходили reject-дайджестом в Telegram — теперь копятся
    в таблице для анализа. Append-only: дедуп держит state (rejected-лоты не пересылаются).
    Колонки A..H: Дата и время | Адрес | Район | Площадь | Цена | Скоринг | Ссылка | Причина."""
    if not rejects:
        return {'ok': True, 'inserted': 0}
    try:
        from zoneinfo import ZoneInfo
        now_bg = datetime.now(ZoneInfo('Europe/Belgrade')).strftime('%Y-%m-%d %H:%M')
    except Exception:
        now_bg = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    rows = []
    for r in rejects:
        label, _ = reason_ru(r.get('reason'))
        district = (r.get('district') or '').strip()
        if district.lower() in ('unknown', 'unknown district', '—'):
            district = ''
        a, p, sc = r.get('area'), r.get('price'), r.get('score')
        rows.append([
            now_bg,
            (r.get('address') or '').strip(),
            district,
            a if a is not None else '',
            p if p is not None else '',
            sc if sc is not None else '',
            r.get('url') or '',
            label,
        ])
    try:
        import sheets_append
        return sheets_append.append_reject_rows(rows)
    except Exception as e:
        print(f'  reject-sheet write failed: {e}', file=sys.stderr)
        return {'ok': False, 'error': str(e), 'inserted': 0}


def build_caption(cand, detail, district_str, flags):
    src = cand.get('source', '?').replace('.ro', '')
    area = cand.get('area')
    price = cand.get('price')
    floor = detail.get('floor') or 'parter'
    addr = (detail.get('street') or cand.get('street') or '').strip() or 'no exact address'

    pano = None
    approx = detail.get('geo_approx')
    if 'lat' in detail and 'lon' in detail:
        maps = f"https://www.google.com/maps/?q={detail['lat']},{detail['lon']}"
        if approx:
            # пин = центр круга/района, не помещение: Street View оттуда бессмыслен
            maps += ' (approximate area)'
        else:
            pano = yandex_pano_url(detail['lat'], detail['lon'])
    else:
        q_text = addr if addr != 'no exact address' else district_str.split(' (')[0]
        q = quote(f"{q_text}, Cluj-Napoca, Romania")
        maps = f"https://www.google.com/maps/search/?api=1&query={q}"

    plus = []
    if detail.get('ceiling'):
        plus.append(f'ceiling {detail["ceiling"]}m')

    cap = f"🍕 Location · {district_str}\n"
    cap += f"📍 {addr}\n"
    cap += f"🗺 {maps}\n"
    if pano:
        cap += f"🌐 Street View: {pano}\n"
    cap += f"📐 {area} m² · 💶 {price} €/mo · 🏢 {floor}\n"
    if plus:
        cap += f"✅ {' · '.join(plus)}\n"
    cap += f"🔗 {src}: {cand['url']}\n"
    if flags:
        cap += ' '.join(f'⚠️ {f}' for f in flags) + '\n'

    desc = (detail.get('description') or '').strip()
    if desc:
        snippet = desc[:300].rstrip()
        cap += f"\n📝 Summary: {snippet}\n"

    return cap[:1024]


def parse_detail(html, cand):
    src = cand.get('source')
    if src == 'olx.ro': return parse_olx(html, cand)
    if src == 'storia.ro': return parse_storia(html, cand)
    if src == 'imobiliare.ro': return parse_imobiliare(html, cand)
    return {}


def _price_similar(p1, p2):
    """Цены «про одно и то же»: разница ≤ max(100€, 3%)."""
    return abs(p1 - p2) <= max(100, 0.03 * max(p1, p2))


def find_active_duplicate(s, rec, self_key):
    """Ищет активный in_sheet лот с тем же физическим помещением: гео <200 м +
    площадь ±3 м² + цена ±max(100€,3%). Агентства перезаливают объявления с новыми
    ID и кросс-постят на другие сайты — по ключу source_id это «новые» лоты.
    Fallback без гео: точное совпадение непустого адреса. Возвращает (key, rec)
    канонического лота или (None, None)."""
    a, p = rec.get('area_m2'), rec.get('price_eur')
    if not a or not p:
        return None, None
    lat, lon = rec.get('geo_lat'), rec.get('geo_lon')
    addr = (rec.get('address') or '').strip().lower()
    for k, v in s['listings'].items():
        if k == self_key or not v.get('in_sheet') or v.get('removed_from_sheet'):
            continue
        va, vp = v.get('area_m2'), v.get('price_eur')
        if not va or not vp or abs(va - a) > 3 or not _price_similar(p, vp):
            continue
        vlat, vlon = v.get('geo_lat'), v.get('geo_lon')
        if lat is not None and vlat is not None:
            if haversine_km(lat, lon, vlat, vlon) <= 0.2:
                return k, v
            continue  # оба с гео, но далеко — соседняя похожая площадь, не дубль
        vaddr = (v.get('address') or '').strip().lower()
        if addr and vaddr and addr == vaddr:
            return k, v
    return None, None


def detect_price_changes(s, all_l):
    """Сравнивает цены из sweep с активными in_sheet лотами. Изменение
    ≥ max(50€, 5%) → обновляет state (price_eur + price_history) и кол. D в
    Sheets, возвращает список изменений — агент шлёт по ним сообщения
    «💶 Цена изменилась». Порог 5% отсекает шум конверсии RSD→EUR."""
    changes, seen = [], set()
    for l in all_l:
        cid, src = l.get('id'), l.get('source', '?')
        newp = l.get('price')
        if not cid or not newp or not (200 <= newp <= 20000):
            continue
        key = f"{src.split('.')[0]}_{cid}"
        if key in seen:
            continue
        seen.add(key)
        rec = s['listings'].get(key)
        if not rec or not rec.get('in_sheet') or rec.get('removed_from_sheet'):
            continue
        oldp = rec.get('price_eur')
        if not oldp or abs(newp - oldp) < max(50, 0.05 * oldp):
            continue
        rec.setdefault('price_history', []).append(
            {'at': now_iso(), 'old': oldp, 'new': newp})
        rec['price_eur'] = newp
        changes.append({'key': key, 'old': oldp, 'new': newp,
                        'district': rec.get('district') or '',
                        'address': rec.get('address') or '',
                        'url': rec.get('url') or '', 'source': rec.get('source') or '',
                        'reply_to_message_id': rec.get('telegram_message_id'),
                        'sheet_updated': False})
    if changes:
        try:
            svc = _sheets_service()
            urls = svc.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range='E2:E2000'
            ).execute().get('values', [])
            row_by_url = {r[0].strip(): i for i, r in enumerate(urls, start=2) if r and r[0]}
            cells, targets = [], []
            for c in changes:
                row = row_by_url.get(c['url'].strip())
                if row:
                    cells.append({'row': row, 'col': 4, 'value': c['new']})
                    targets.append(c)
            if cells:
                update_cells(cells)  # при исключении sheet_updated останется False
                for c in targets:
                    c['sheet_updated'] = True
        except Exception as e:
            print(f'  price-change sheet update failed: {e}', file=sys.stderr)
    return changes


# Статусы кол. K основного листа → категория фидбека (startswith, lower).
# Команда партнёра англоязычная — принимаем оба языка.
FEEDBACK_LIKE = ('в работе', 'ок', 'in progress', 'ok', 'wip', 'working')
FEEDBACK_DISLIKE = ('не подходит', 'отказ', 'not suitable', 'rejected', 'no', 'pass')


def collect_sheet_feedback():
    """Фидбек-луп через Google Sheets: читает кол. B (район) и K (статус,
    Сергей ставит из попапа карты) и агрегирует лайки/дизлайки по районам.
    Ключи: полная строка «Општина (Подрайон)» и отдельно општина."""
    svc = _sheets_service()
    vals = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range='A2:K2000').execute().get('values', [])
    by_district = {}
    for r in vals:
        district = r[1].strip() if len(r) > 1 and r[1] else ''
        status = r[10].strip().lower() if len(r) > 10 and r[10] else ''
        if not district or not status:
            continue
        if status.startswith(FEEDBACK_LIKE):
            cat = 'like'
        elif status.startswith(FEEDBACK_DISLIKE):
            cat = 'dislike'
        else:
            continue
        for dkey in {district, district.split(' (')[0].strip()}:
            agg = by_district.setdefault(dkey, {'like': 0, 'dislike': 0})
            agg[cat] += 1
    return {'by_district': by_district, 'source': 'sheet_col_K',
            'updated_at': now_iso()}


def run_process():
    with StateLock():
        s = load_state()
        s.setdefault('listings', {})

        # Clean old photos
        try:
            subprocess.run(['find', str(PHOTO_DIR), '-type', 'f', '-mtime', '+7', '-delete'],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        PHOTO_DIR.mkdir(exist_ok=True)

        t0 = time.time()
        all_l, sources_down, sweep_errors = [], [], []

        for name, fn, pages in [
            ('olx', curl_sweep.sweep_olx, 4),
            ('storia', curl_sweep.sweep_storia, 4),
            ('imobiliare', curl_sweep.sweep_imobiliare, 3),
        ]:
            try:
                all_l.extend(fn(pages=pages))
            except Exception as e:
                sources_down.append(name)
                sweep_errors.append(f'{name}:{type(e).__name__}')

        # Prefilter — same logic as curl_sweep.main()
        filtered = []
        for l in all_l:
            if not l.get('area') or not l.get('price'): continue
            if l['area'] < AREA_MIN or l['area'] > AREA_MAX: continue
            if l['price'] < PRICE_MIN or l['price'] > PRICE_MAX: continue
            if l['price'] / l['area'] < PRICE_PER_M2_MIN: continue
            url = (l.get('url') or '').lower()
            if any(slug in url for slug in ZEMUN_SLUGS): continue
            type_ = (l.get('type', '') or '').lower()
            if any(b in type_ for b in ['birou', 'depozit', 'industrial', 'hala']): continue
            # storia отдаёт весь жудец — города/коммуны вне Клужа режем по
            # municipality (city.name) и по url-слагу.
            mun = _norm_sub(l.get('municipality') or '')
            mun_slug = mun.replace(' ', '-')
            if any(x == mun_slug or x in url for x in FAR_MUNI_SLUGS):
                continue
            filtered.append(l)

        # Изменения цены на активных лотах видны только в sweep-выдаче (по ключу
        # они «известные» и в detail-цикл не попадают) — ловим их здесь.
        price_changes = detect_price_changes(s, all_l)

        known = curl_sweep.known_ids()
        new_lots = [l for l in filtered if l.get('id') and l['id'] not in known]

        pending = list(s.get('pending_candidates', []))
        candidates = new_lots + pending

        # Cap per source
        per_src, capped, leftover = {}, [], []
        for c in candidates:
            src = c.get('source', '?')
            if (per_src.get(src, 0) >= MAX_DETAILS_PER_SOURCE
                    or len(capped) >= MAX_DETAILS_PER_CYCLE):
                leftover.append(c)
                continue
            per_src[src] = per_src.get(src, 0) + 1
            capped.append(c)
        s['pending_candidates'] = leftover

        passes, rejects, duplicates = [], [], []
        done_this_cycle = set()  # лот мог быть и в new, и в pending → не обрабатывать дважды

        for cand in capped:
            src = cand.get('source', '?')
            cid = cand.get('id', '')
            src_prefix = src.split('.')[0]
            key = f"{src_prefix}_{cid}"
            if key in done_this_cycle:
                continue
            done_this_cycle.add(key)

            existing = s['listings'].get(key)
            if existing and (existing.get('in_sheet') or existing.get('rejected')
                             or existing.get('removed_from_sheet')):
                existing['last_seen_at'] = now_iso()
                continue
            # else: new OR zombie (no terminal status) — fall through to detail-fetch

            html, status = fetch_html(cand['url'])
            if not html:
                rejects.append({
                    'key': key, 'reason': f'fetch_fail:http={status}',
                    'district': '', 'address': (cand.get('street') or '').strip(),
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src, 'lat': None, 'lon': None,
                })
                continue

            detail = parse_detail(html, cand)
            passed, flags, reason = apply_filters(cand, detail)
            district_str = extract_district(cand, detail)

            rec = {
                'source': src, 'id': cid, 'url': cand['url'],
                'area_m2': cand.get('area'), 'price_eur': cand.get('price'),
                'floor': detail.get('floor') or cand.get('floor'),
                'address': (detail.get('street') or cand.get('street') or '').strip(),
                'district': district_str,
                'subdistrict': None,
                'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                'in_sheet': False, 'alerted': False,
                'flags': flags,
                'description': (detail.get('description') or '')[:2000],
            }
            if 'lat' in detail:
                rec['geo_lat'] = detail['lat']
                rec['geo_lon'] = detail['lon']
                rec['geo_source'] = 'detail_approx' if detail.get('geo_approx') else 'detail'
            if detail.get('refreshed_at'): rec['refreshed_at'] = detail['refreshed_at']
            if detail.get('published_at'): rec['published_at'] = detail['published_at']
            if detail.get('photo_url'): rec['photo_url'] = detail['photo_url']
            purls = extract_photo_urls(html, src)
            if purls: rec['photo_urls'] = purls

            if not passed:
                rec['rejected'] = True
                rec['flags'] = [reason] + flags
                s['listings'][key] = rec
                rejects.append({
                    'key': key, 'reason': reason,
                    'district': district_str, 'address': rec['address'],
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src,
                    'lat': detail.get('lat'), 'lon': detail.get('lon'),
                })
                continue

            rec['rejected'] = False
            s['listings'][key] = rec

            # Ручной blacklist районов (state.district_blacklist, substring-match).
            bl = s.get('district_blacklist') or []
            if district_str and any(b.lower() in district_str.lower() for b in bl):
                rec['rejected'] = True
                rec['flags'] = ['district_blacklist'] + flags
                rejects.append({
                    'key': key, 'reason': 'district_blacklist',
                    'district': district_str, 'address': rec['address'],
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src,
                    'lat': detail.get('lat'), 'lon': detail.get('lon'),
                })
                continue

            # Дубль-детект: то же помещение уже в таблице под другим ID/источником.
            dup_key, dup_rec = find_active_duplicate(s, rec, self_key=key)
            if dup_key:
                rec['rejected'] = True
                rec['duplicate_of'] = dup_key
                rec['flags'] = [f'duplicate_of:{dup_key}'] + flags
                alt = dup_rec.setdefault('alt_urls', [])
                if rec['url'] and rec['url'] != dup_rec.get('url') and rec['url'] not in alt:
                    alt.append(rec['url'])
                duplicates.append({
                    'key': key, 'duplicate_of': dup_key,
                    'url': rec['url'], 'canonical_url': dup_rec.get('url'),
                    'district': district_str,
                    'area': cand.get('area'), 'price': cand.get('price'),
                })
                continue

            sc = score_and_cache(rec)

            # Все фото лота (до 10) — Telegram-альбом одним сообщением.
            photo_paths = []
            gallery = purls or ([detail['photo_url']] if detail.get('photo_url') else [])
            for pi, pu in enumerate(gallery[:10]):
                pp = download_photo(pu, f'{key}_{pi}')
                if pp:
                    photo_paths.append(str(pp))
            photo_path = photo_paths[0] if photo_paths else None

            caption = build_caption(cand, detail, district_str, flags)
            if sc:
                caption = caption + "\n\n" + scoring.score_line(sc)

            passes.append({
                'listing_key': key,
                'photo_path': photo_path,
                'photo_paths': photo_paths,
                'caption': caption,
                'chat_id': CHAT_ID,
                'district': district_str,
                'address': rec['address'],
                'area': cand.get('area'),
                'price': cand.get('price'),
                'url': cand['url'],
                'flags': flags,
            })

        # --- Реджекты по цене / цене за м² ---
        # Метраж в норме (100–220), но цена вне 1300–6000 или €/м² ниже минимума.
        # В дайджест идут только настоящие price-near-miss: валидный prizemlje-локал,
        # не прошедший ТОЛЬКО по цене. Поэтому перед публикацией фетчим detail и гоняем
        # структурные фильтры (skip_price=True): этаж, назначение, офис/молл/двор/квартира.
        # Структурный брак (квартира на 1.+ spratu, стан, офис) → молча в state как
        # rejected, в дайджест НЕ публикуем — это не «почти подошло», это просто не то.
        # Дедуп через state. Кэп публикаций = ≤8 строк суммарно с detail-реджектами;
        # отдельный бюджет фетчей, чтобы просканить мимо квартир и найти реальные near-miss.
        MAX_REJECTS_SHOWN = 20
        price_cap = max(0, MAX_REJECTS_SHOWN - len(rejects))
        struct_fetch_budget = 25
        pr_seen = set()
        for l in all_l:
            if price_cap <= 0 or struct_fetch_budget <= 0:
                break
            a, p = l.get('area'), l.get('price')
            if not a or not p:
                continue
            if a < AREA_MIN or a > AREA_MAX:
                continue
            p_ok = PRICE_MIN <= p <= PRICE_MAX
            ppm_ok = (p / a) >= PRICE_PER_M2_MIN
            if p_ok and ppm_ok:
                continue
            cid = l.get('id')
            if not cid:
                continue
            src = l.get('source', '?')
            key = f"{src.split('.')[0]}_{cid}"
            if key in pr_seen:
                continue
            pr_seen.add(key)
            url = (l.get('url') or '').lower()
            if any(slug in url for slug in ZEMUN_SLUGS):
                continue
            if any(s in _norm_sub(l.get('municipality') or '').replace(' ', '-')
                   for s in ZEMUN_SLUGS):
                continue
            type_ = (l.get('type', '') or '').lower()
            if any(b in type_ for b in ['birou', 'depozit', 'industrial', 'hala']):
                continue
            existing = s['listings'].get(key)
            if existing and (existing.get('in_sheet') or existing.get('rejected')
                             or existing.get('removed_from_sheet')):
                existing['last_seen_at'] = now_iso()
                continue
            price_reason = f'price={p}' if not p_ok else f'price_per_m2={p / a:.1f}'
            addr = (l.get('street') or l.get('name') or '').strip()
            district = (l.get('municipality') or '').strip()

            def _silent_reject(reason_flag):
                """Записать молчаливый реджект в state (в дайджест НЕ идёт)."""
                s['listings'][key] = {
                    'source': src, 'id': cid, 'url': l.get('url'),
                    'area_m2': a, 'price_eur': p,
                    'address': addr, 'district': district,
                    'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                    'in_sheet': False, 'alerted': False,
                    'rejected': True, 'flags': [reason_flag, price_reason],
                }

            # Дальний пригород (Обреновац/Лазаревац/…) — вне зоны, не показываем.
            muni_l = (l.get('municipality') or '').strip().lower()
            if any(slug in url for slug in FAR_MUNI_SLUGS) or \
               any(muni_l == m or m in muni_l for m in FAR_MUNI_SLUGS):
                _silent_reject('far_muni')
                continue
            # Далёкий промах по цене (8000€ / 900€) — это шум, не «почти подошло».
            if not p_ok and not (PRICE_NEAR_LOW <= p <= PRICE_NEAR_HIGH):
                _silent_reject('price_far')
                continue

            # Фетч detail + структурная проверка перед публикацией.
            cand_l = {
                'source': src, 'id': cid, 'url': l.get('url'),
                'area': a, 'price': p, 'type': l.get('type'),
                'floor': l.get('floor'), 'street': l.get('street'),
            }
            struct_html, _st = fetch_html(l.get('url'))
            struct_fetch_budget -= 1
            struct_ok, _f, struct_reason = (True, [], None)
            detail_l = {}
            if struct_html:
                detail_l = parse_detail(struct_html, cand_l)
                struct_ok, _f, struct_reason = apply_filters(cand_l, detail_l, skip_price=True)
                d2 = extract_district(cand_l, detail_l)
                if d2:
                    district = d2
                if detail_l.get('street'):
                    addr = detail_l['street'].strip()
                # Пригород мог проявиться только в районе из detail.
                dl = _norm_sub(district).replace(' ', '-')
                if any(m in dl for m in FAR_MUNI_SLUGS) or \
                   any(m in dl for m in ZEMUN_SLUGS):
                    _silent_reject('far_muni')
                    continue

            if not struct_ok:
                # структурный брак — молча реджектим, в дайджест НЕ кладём
                s['listings'][key] = {
                    'source': src, 'id': cid, 'url': l.get('url'),
                    'area_m2': a, 'price_eur': p,
                    'floor': detail_l.get('floor') or l.get('floor'),
                    'address': addr, 'district': district,
                    'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                    'in_sheet': False, 'alerted': False,
                    'rejected': True, 'flags': [struct_reason, price_reason],
                }
                continue

            # настоящий price-near-miss — публикуем
            s['listings'][key] = {
                'source': src, 'id': cid, 'url': l.get('url'),
                'area_m2': a, 'price_eur': p,
                'floor': detail_l.get('floor') or l.get('floor'),
                'address': addr, 'district': district,
                'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                'in_sheet': False, 'alerted': False,
                'rejected': True, 'flags': [price_reason],
            }
            rejects.append({
                'key': key, 'reason': price_reason,
                'district': district, 'address': addr,
                'area': a, 'price': p,
                'url': l.get('url'), 'source': src,
                'lat': detail_l.get('lat'), 'lon': detail_l.get('lon'),
            })
            price_cap -= 1

        # --- Скоринг локаций для реджектов (по координатам) ---
        # Каждый score = 1 запрос в Overpass, поэтому бюджет, чтобы не словить
        # троттлинг и не растянуть часовой цикл. Скорим в порядке €/м² убыв.
        # (как в дайджесте) — при нехватке бюджета приоритет дорогим за метр.
        SCORE_BUDGET = 20
        _scored = 0
        for r in sorted(rejects,
                        key=lambda x: ((x.get('price') or 0) / (x.get('area') or 1)),
                        reverse=True):
            if _scored >= SCORE_BUDGET:
                break
            lat, lon = r.get('lat'), r.get('lon')
            if lat is None or lon is None:
                continue
            try:
                sc = scoring.score_location(lat, lon, dodo_points=DODO_POINTS)
            except Exception:
                continue
            if not sc or sc.get('score') is None:
                continue
            r['score'] = sc['score']
            lst = s['listings'].get(r['key'])
            if lst is not None:
                lst['score'] = sc['score']
                lst['score_data'] = sc
                lst['scored_at'] = now_iso()
            _scored += 1

        # Фидбек из Sheets (кол. K, собирается в finalize): районы, где есть
        # лоты «В работе»/«ОК», помечаем звездой и поднимаем в начало выдачи.
        fb = (s.get('feedback_aggregates') or {}).get('by_district', {})
        for p in passes:
            d = p.get('district') or ''
            agg = fb.get(d) or fb.get(d.split(' (')[0].strip()) or {}
            if agg.get('like', 0) > 0:
                p['feedback_like'] = True
                p['caption'] = ('⭐ District with locations already in progress\n'
                                + p['caption'])[:1024]
        passes.sort(key=lambda p: 0 if p.get('feedback_like') else 1)

        s['last_cycle_at'] = now_iso()
        save_state(s)

        elapsed = time.time() - t0
        summary = {
            'sweep_raw': len(all_l),
            'filtered': len(filtered),
            'new': len(new_lots),
            'pending_in': len(pending),
            'pending_out': len(leftover),
            'processed': len(capped),
            'passes': len(passes),
            'rejects': len(rejects),
            'duplicates': len(duplicates),
            'price_changes': len(price_changes),
            'sources_down': sources_down,
            'errors': sweep_errors[:5],
            'time_sec': round(elapsed, 1),
        }

        # Compact stderr summary for the agent
        print(f'\n=== cycle.py SUMMARY ({elapsed:.1f}s) ===', file=sys.stderr)
        print(f'sweep={summary["sweep_raw"]} filtered={summary["filtered"]} '
              f'new={summary["new"]} pending={len(pending)}→{len(leftover)}', file=sys.stderr)
        print(f'processed={summary["processed"]} passes={summary["passes"]} '
              f'rejects={summary["rejects"]}', file=sys.stderr)
        for r in rejects[:8]:
            print(f"  REJECT {r['key'][:40]}: {r['reason']}", file=sys.stderr)
        for p in passes[:5]:
            print(f"  PASS {p['listing_key'][:40]}: {p['district']} "
                  f"{p['area']}m² {p['price']}€", file=sys.stderr)
        for d in duplicates[:5]:
            print(f"  DUP {d['key'][:40]} == {d['duplicate_of'][:40]}", file=sys.stderr)
        for c in price_changes[:5]:
            print(f"  PRICE {c['key'][:40]}: {c['old']}€ → {c['new']}€", file=sys.stderr)
        if sources_down:
            print(f'  sources_down={sources_down}', file=sys.stderr)

        # Реджекты теперь не в Telegram, а в лист «не прошли фильтр».
        reject_sheet = write_rejects_to_sheet(rejects)
        if reject_sheet.get('inserted'):
            print(f'  reject-sheet: +{reject_sheet["inserted"]} строк', file=sys.stderr)

        print(json.dumps({
            'passes': passes,
            'rejects': rejects,
            'duplicates': duplicates,
            'price_changes': price_changes,
            'reject_digest': None,
            'reject_sheet': reject_sheet,
            'summary': summary,
        }, ensure_ascii=False))


def cmd_mark_sent(listing_key, message_id, desc_ru=None):
    """Called by agent after Telegram send_file succeeds.
    desc_ru — русское «Кратко» от агента; идёт в Sheets кол. F (карта показывает её).
    Без него падаем на сырое сербское описание (хуже — карта будет на сербском)."""
    with StateLock():
        s = load_state()
        rec = s.get('listings', {}).get(listing_key)
        if not rec:
            print(json.dumps({'ok': False, 'error': f'listing_key {listing_key} not in state'}))
            return 1

        rec['alerted'] = True
        if desc_ru:
            rec['description_ru'] = desc_ru[:1000]  # gen_map fallback когда Sheets F пуст
        try:
            rec['telegram_message_id'] = int(message_id)
        except (ValueError, TypeError):
            pass
        rec['sent_at'] = now_iso()
        save_state(s)  # save Telegram metadata first

        try:
            sheet_resp = insert_lots([{
                'address': rec.get('address') or '',
                'district': rec.get('district') or '',
                'area': rec.get('area_m2'),
                'price': rec.get('price_eur'),
                'url': rec.get('url'),
                'description_ru': (desc_ru or rec.get('description') or '')[:1000],
                'date_posted': (rec.get('refreshed_at')
                                or rec.get('published_at')
                                or rec.get('first_seen_at') or '')[:10],
            }])
        except Exception as e:
            print(json.dumps({'ok': False, 'sheets_error': str(e),
                              'state_updated': True, 'in_sheet': False}))
            return 1

        rec['in_sheet'] = True

        # Находим строку лота по URL один раз (insert_at_top кладёт наверх):
        # нужна и для скоринга в кол. M, и для named range (ссылка из Telegram).
        sheet_link = None
        lot_row = None
        if rec.get('url'):
            try:
                svc = _sheets_service()
                urls = svc.spreadsheets().values().get(
                    spreadsheetId=SPREADSHEET_ID, range='E2:E1000'
                ).execute().get('values', [])
                target = rec['url'].strip()
                for i, row in enumerate(urls, start=2):
                    if row and row[0].strip() == target:
                        lot_row = i
                        break
            except Exception:
                pass
        sc = rec.get('score')
        if sc is not None and lot_row:
            try:
                update_cells([{'row': lot_row, 'col': 13, 'value': sc}])
            except Exception:
                pass  # скоринг в M не критичен, не валим mark-sent
        if lot_row:
            try:
                from sheets_append import create_lot_named_range
                sheet_link = create_lot_named_range(lot_row, listing_key)
                rec['sheet_link'] = sheet_link
            except Exception:
                pass  # ссылка — nice-to-have

        s.setdefault('sent_messages', {})[str(message_id)] = {
            'listing_key': listing_key, 'sent_at': rec['sent_at'],
        }
        save_state(s)
        print(json.dumps({'ok': True, 'sheets': sheet_resp,
                          'listing_key': listing_key,
                          'sheet_link': sheet_link,
                          'in_sheet': True}))
    return 0


def cmd_finalize():
    """Run check_status + gen_map + write runs.log line. Single tool-call replacement
    for the previous 3-step agent flow."""
    t0 = time.time()
    out = {'ok': True}

    # check_status manages its own state writes; prints summary on stdout
    cr = subprocess.run(['python3', str(SCRIPT_DIR / 'check_status.py')],
                        capture_output=True, text=True, timeout=300)
    cout = (cr.stdout or '') + '\n' + (cr.stderr or '')
    m = re.search(r'(\d+)\s+killed[^a-z]*?(\d+)\s+alive', cout)
    killed = int(m.group(1)) if m else 0
    alive = int(m.group(2)) if m else 0
    out['check_killed'] = killed
    out['check_alive'] = alive
    out['check_rc'] = cr.returncode
    if cr.returncode != 0:
        out['check_stderr_tail'] = (cr.stderr or '').strip().split('\n')[-3:]

    # gen_map deploys to surge.sh automatically; prints to stdout
    mr = subprocess.run(['python3', str(SCRIPT_DIR / 'gen_map.py')],
                        capture_output=True, text=True, timeout=240)
    mout = (mr.stdout or '') + '\n' + (mr.stderr or '')
    out['map_ok'] = bool(re.search(r'wrote\s+/.*lokali\.html', mout))
    out['map_surge_ok'] = 'Success!' in mout
    feat_m = re.search(r'features:\s*(\d+)', mout)
    out['map_features'] = int(feat_m.group(1)) if feat_m else None
    out['map_rc'] = mr.returncode
    if mr.returncode != 0:
        out['map_stderr_tail'] = (mr.stderr or '').strip().split('\n')[-3:]

    # Sweep zombies: listings >24h old without any terminal status → mark dead.
    # Also flag legacy keys (not matching <src>_<id> with known prefix) — they can't
    # be re-attached by future sweeps.
    KNOWN_PREFIXES = ('olx_', 'storia_', 'imobiliare_')
    zombie_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    zombies_marked, legacy_marked = 0, 0

    # Фидбек из кол. K основного листа (Сергей ставит статусы из попапа карты).
    # Применяется в следующем process-цикле: буст районов с лотами «В работе».
    try:
        feedback = collect_sheet_feedback()
    except Exception as e:
        feedback = None
        out['feedback_error'] = str(e)[:200]

    with StateLock():
        s = load_state()
        listings = s.get('listings', {})
        if feedback:
            s['feedback_aggregates'] = feedback
            out['feedback_districts'] = len(feedback['by_district'])

        # Компакция: терминальным записям старше 45 дней тяжёлые поля не нужны
        # (описания и фото-списки — основной вес state.json).
        compact_cutoff = datetime.now(timezone.utc) - timedelta(days=45)
        compacted = 0
        for k, r in listings.items():
            if not (r.get('rejected') or r.get('removed_from_sheet')):
                continue
            if r.get('compacted'):
                continue
            last = (r.get('last_seen_at') or r.get('last_seen')
                    or r.get('first_seen_at') or r.get('first_seen') or '')
            try:
                lt = datetime.fromisoformat(last.replace('Z', '+00:00'))
            except Exception:
                lt = None
            if lt is None or lt > compact_cutoff:
                continue
            for f in ('description', 'description_ru', 'photo_urls', 'score_data'):
                r.pop(f, None)
            r['compacted'] = True
            compacted += 1
        out['compacted'] = compacted
        for k, r in listings.items():
            if r.get('in_sheet') or r.get('rejected') or r.get('removed_from_sheet'):
                continue
            first_seen = r.get('first_seen_at') or ''
            try:
                dt = datetime.fromisoformat(first_seen.replace('Z', '+00:00')) if first_seen else None
            except Exception:
                dt = None
            is_legacy = not any(k.startswith(p) for p in KNOWN_PREFIXES)
            if is_legacy:
                r['removed_from_sheet'] = True
                r['stale_marked_at'] = now_iso()
                r['dead_reason'] = 'legacy_key_unreachable'
                r['removed_human'] = True
                legacy_marked += 1
            elif dt is None or dt < zombie_cutoff:
                # No timestamps OR older than 24h with no terminal status → dead
                r['removed_from_sheet'] = True
                r['stale_marked_at'] = now_iso()
                r['dead_reason'] = 'stale_no_status'
                r['removed_human'] = True
                zombies_marked += 1
        save_state(s)
        total = len(s.get('listings', {}))
    out['zombies_marked'] = zombies_marked
    out['legacy_marked'] = legacy_marked

    # Append runs.log
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = (f'{ts} · finalize · total={total} · '
            f'check={alive}alive/{killed}killed · '
            f'map={"ok" if out["map_ok"] else "ERR"} '
            f'features={out["map_features"]} · '
            f'surge={"ok" if out["map_surge_ok"] else "ERR"}')
    with open(RUNS_LOG, 'a') as f:
        f.write(line + '\n')

    out['time_sec'] = round(time.time() - t0, 1)
    out['log_line'] = line
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mark-sent', nargs=2, metavar=('KEY', 'MSG_ID'),
                    help='Mark a pass as sent + insert to Sheets')
    ap.add_argument('--desc-ru', default=None,
                    help='Русское «Кратко» для Sheets кол. F (вместо сырого сербского описания)')
    ap.add_argument('--finalize', action='store_true',
                    help='Run check_status + gen_map + runs.log')
    args = ap.parse_args()

    if args.mark_sent:
        return cmd_mark_sent(args.mark_sent[0], args.mark_sent[1], desc_ru=args.desc_ru)
    if args.finalize:
        return cmd_finalize()
    return run_process()


if __name__ == '__main__':
    sys.exit(main() or 0)
