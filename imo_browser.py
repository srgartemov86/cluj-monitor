#!/usr/bin/env python3
"""imo_browser.py — imobiliare.ro через настоящий браузер (Camoufox).

С августа 2026 imobiliare закрыт Cloudflare + JS-челленджем DataDome на всех
страницах поиска: HTTP-клиенты (curl_cffi) и резидентные прокси получают 403.
Пробы 11.09.2026 на раннере GitHub: Camoufox (антидетект-Firefox) НАПРЯМУЮ,
без прокси, открывает листинг 3 из 3 раз (68 карточек на 6 страницах);
detail-страница открылась 2 из 3 с первого захода, поэтому есть перезагрузка.
Chromium/Playwright и любой вариант через прокси блокируются.

Один браузер на процесс, ленивый старт, закрывается при выходе процесса.
Включается env IMO_BROWSER=1 (ставит workflow); без него get_html -> None,
и curl_sweep.imo_get работает по-старому.
"""
import atexit, os, re, sys

_state = {'cm': None, 'page': None, 'failed': False}
_BLOCK = ('captcha-delivery', 'var dd=', 'Attention Required', 'Doar un moment', 'Just a moment')
_CARD = re.compile(r'/oferta/spatiu-comercial-de-inchiriat-[a-z0-9\-]+-\d+')


def _log(msg):
    print(f'  imo_browser: {msg}', file=sys.stderr, flush=True)


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
    # прогрев: главная не за челленджем и выдаёт куки, с ней листинг проходит стабильнее
    page.goto('https://www.imobiliare.ro/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(3000)
    _state['page'] = page
    return page


def _blocked(html):
    return (not html) or any(m in html for m in _BLOCK)


def get_html(url, detail=False, tries=3):
    """HTML страницы или None. Листинг ждём до появления карточек, detail до
    ухода челленджа; при блоке перезагружаем (всего до `tries` заходов)."""
    if not enabled():
        return None
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
            for _ in range(8):  # до ~20 с на решение челленджа и рендер
                page.wait_for_timeout(2500)
                html = page.content()
                if detail:
                    if not _blocked(html) and len(html) > 50000:
                        return html
                elif _CARD.search(html):
                    return html
        except Exception as e:
            _log(f'{type(e).__name__} on {url[:80]}')
    _log(f'blocked after {tries} tries: {url[:80]}')
    return None
