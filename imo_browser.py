#!/usr/bin/env python3
"""imo_browser.py — imobiliare.ro через настоящий браузер (Camoufox).

С августа 2026 imobiliare закрыт Cloudflare + JS-челленджем DataDome на всех
страницах поиска: HTTP-клиенты (curl_cffi) и резидентные прокси получают 403.
Пробы 11.09.2026 на раннере GitHub: Camoufox (антидетект-Firefox) НАПРЯМУЮ,
без прокси, открывает листинг 3 из 3 раз (68 карточек на 6 страницах).
Страницы объявлений в боевом прогоне 11.09 блокировались 6 из 6 даже после
перезагрузок, поэтому у detail один заход и предохранитель: после двух блоков
подряд detail в этом процессе больше не открываем (иначе цикл шёл 18 минут).

Один браузер на процесс, ленивый старт, закрывается при выходе процесса.
Включается env IMO_BROWSER=1 (ставит workflow); без него get_html -> None,
и curl_sweep.imo_get работает по-старому.
"""
import atexit, os, re, sys

_state = {'cm': None, 'page': None, 'failed': False, 'detail_fails': 0, 'detail_off': False,
          'coords': {}}
_BLOCK = ('captcha-delivery', 'var dd=', 'Attention Required', 'Doar un moment', 'Just a moment')
_CARD = re.compile(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-\d+')
DETAIL_BREAKER = 2


def _log(msg):
    print(f'  imo_browser: {msg}', file=sys.stderr, flush=True)


def _on_response(r):
    """Листинг сам запрашивает /map/top_listing*: results = [{"0": id, "1": [lon, lat]}].
    Копим координаты по id: detail-страницы с ними закрыты DataDome."""
    try:
        u = r.url
        if '/map/top_listing' in u or '/map/listings' in u:
            for x in (r.json() or {}).get('results') or []:
                i, ll = x.get('0'), x.get('1')
                if i and isinstance(ll, list) and len(ll) == 2:
                    _state['coords'][str(i)] = (float(ll[1]), float(ll[0]))
    except Exception:
        pass


def coords():
    return _state['coords']


def enabled():
    return bool(os.environ.get('IMO_BROWSER')) and not _state['failed']


def close():
    cm = _state.get('cm')
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
    _state.update(cm=None, page=None)


def _page():
    if _state['page'] is not None:
        return _state['page']
    from camoufox.sync_api import Camoufox
    cm = Camoufox(headless=True, locale='ro-RO')
    browser = cm.__enter__()
    _state['cm'] = cm
    atexit.register(close)
    page = browser.new_page()
    page.on('response', _on_response)
    # прогрев: главная не за челленджем и выдаёт куки, с ней листинг проходит стабильнее
    page.goto('https://www.imobiliare.ro/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(3000)
    _state['page'] = page
    return page


def _blocked(html):
    return (not html) or any(m in html for m in _BLOCK)


def get_html(url, detail=False, tries=None):
    """HTML страницы или None. Листинг ждём до появления карточек (до 2 заходов),
    detail до ухода челленджа (1 заход, затем предохранитель)."""
    if not enabled():
        return None
    if detail and _state['detail_off']:
        return None
    tries = tries or (1 if detail else 2)
    try:
        page = _page()
    except Exception as e:
        _state['failed'] = True  # браузер не поднялся: в этом процессе больше не пробуем
        _log(f'start failed {type(e).__name__}: {str(e)[:160]}')
        return None
    for attempt in range(tries):
        try:
            if attempt == 0:
                page.goto(url, timeout=60000, wait_until='domcontentloaded')
            else:
                page.wait_for_timeout(4000)
                page.reload(timeout=60000, wait_until='domcontentloaded')
            for _ in range(6):  # до ~15 с на решение челленджа и рендер
                page.wait_for_timeout(2500)
                html = page.content()
                if detail:
                    if not _blocked(html) and len(html) > 50000:
                        _state['detail_fails'] = 0
                        return html
                elif _CARD.search(html):
                    return html
        except Exception as e:
            _log(f'{type(e).__name__} on {url[:80]}')
    _log(f'blocked after {tries} tries: {url[:80]}')
    if detail:
        _state['detail_fails'] += 1
        if _state['detail_fails'] >= DETAIL_BREAKER:
            _state['detail_off'] = True
            _log(f'detail breaker open after {DETAIL_BREAKER} blocks: skip detail pages this run')
    return None
