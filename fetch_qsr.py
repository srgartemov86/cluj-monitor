"""
QSR competitors in Cluj-Napoca from OpenStreetMap (Overpass API).
Writes `/Users/dodo/cluj-location-monitor/qsr_cluj.json` for gen_map.

Run weekly — OSM data changes slowly, Overpass rate-limits requests.

Brands (requested by Sergey 2026-07-10):
McDonald's, KFC, Burger King, Trenta Pizza, Domino's, Pizza Hut, Jerry's Pizza.

Logos: public/brands/<slug>.{png|svg}; brands without a logo file use
letter icons (icon_type='letter').
"""
import json, os
from collections import defaultdict
from curl_cffi import requests

OUTPUT = os.path.join(os.environ.get('CLUJ_DATA', '/Users/dodo/cluj-location-monitor'), 'qsr_cluj.json')

# Bounding box: Cluj-Napoca + близлежащий Florești strip (конкуренты у границы важны)
BBOX = "46.70,23.45,46.83,23.72"

# (lowercase keyword → brand_slug), matched against name|brand
RULES = [
    ('mcdonald',     'mcdonalds'),
    ('kfc',          'kfc'),
    ('burger king',  'burgerking'),
    ('burgerking',   'burgerking'),
    ('trenta',       'trenta'),
    ('domino',       'dominos'),
    ('pizza hut',    'pizzahut'),
    ('pizzahut',     'pizzahut'),
    ("jerry's pizza", 'jerrys'),
    ('jerrys pizza', 'jerrys'),
    ('jerry s pizza', 'jerrys'),
]

BRAND_META = {
    'mcdonalds':  {'name': "McDonald's",   'logo': 'brands/mcdonalds.svg'},
    'kfc':        {'name': 'KFC',          'logo': 'brands/kfc.png'},
    # Логотипы BK/Domino's/Pizza Hut — Wikimedia Commons SVG (2026-07-10)
    'burgerking': {'name': 'Burger King',  'logo': 'brands/burgerking.svg'},
    'trenta':     {'name': 'Trenta Pizza', 'logo': 'brands/trenta.png',
                   'icon_type': 'letter', 'letter': 'T'},
    'dominos':    {'name': "Domino's",     'logo': 'brands/dominos.svg'},
    'pizzahut':   {'name': 'Pizza Hut',    'logo': 'brands/pizzahut.svg'},
    'jerrys':     {'name': "Jerry's Pizza", 'logo': 'brands/jerrys.png',
                   'icon_type': 'letter', 'letter': 'J'},
}

# Manual overrides for brands with poor OSM coverage (source: official sites).
# Trenta Pizza: НЕ клужская сеть — все 10 точек в Бухаресте/Илфове
# (trentapizza.ro/magazine, проверено 2026-07-10). В Клуже 0 — это корректно.
# Jerry's Pizza Titulescu 2: PERMANENTLY CLOSED по Google Maps (103 репорта,
# проверено 2026-07-10) — из карты убрана. В Клуже у Jerry's точек нет.
MANUAL_OVERRIDES = {
    'jerrys': [],  # держим override пустым, чтобы OSM-остатки закрытой точки не всплыли
}


def classify(name):
    n = (name or '').lower()
    for kw, slug in RULES:
        if kw in n:
            return slug
    return None


def main():
    name_re = "|".join(sorted(set(kw for kw, _ in RULES)))
    query = f'''[out:json][timeout:60];
(
  nwr["name"~"{name_re}",i]({BBOX});
  nwr["brand"~"{name_re}",i]({BBOX});
);
out center tags;'''
    r = requests.post("https://overpass-api.de/api/interpreter",
                      data={"data": query},
                      headers={"Accept": "application/json", "User-Agent": "DodoMonitor/1.0"},
                      timeout=90, impersonate="chrome120")
    els = r.json().get('elements', [])

    by_brand = defaultdict(list)
    seen = set()
    for e in els:
        tags = e.get('tags', {})
        name = tags.get('name') or tags.get('brand') or ''
        lat = e.get('lat') or (e.get('center') or {}).get('lat')
        lon = e.get('lon') or (e.get('center') or {}).get('lon')
        if not (lat and lon): continue
        slug = classify(name)
        if not slug: continue
        key = (slug, round(lat, 4), round(lon, 4))  # dedup ~11m
        if key in seen: continue
        seen.add(key)
        addr_parts = [tags.get('addr:street', ''), tags.get('addr:housenumber', '')]
        addr = ' '.join(p for p in addr_parts if p).strip()
        by_brand[slug].append({'lat': lat, 'lon': lon, 'name': name, 'address': addr})

    out = {
        'fetched_at': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
        'source': 'OpenStreetMap via Overpass',
        'brands': {},
    }
    for slug, meta in BRAND_META.items():
        if slug in MANUAL_OVERRIDES:
            points, src = MANUAL_OVERRIDES[slug], 'official site (manual)'
        else:
            points, src = by_brand.get(slug, []), 'OSM/Overpass'
        brand_out = {
            'name': meta['name'], 'logo': meta['logo'], 'source': src,
            'count': len(points), 'points': points,
        }
        if 'icon_type' in meta: brand_out['icon_type'] = meta['icon_type']
        if 'letter' in meta:    brand_out['letter'] = meta['letter']
        out['brands'][slug] = brand_out

    json.dump(out, open(OUTPUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    total = sum(len(b['points']) for b in out['brands'].values())
    print(f"wrote {OUTPUT}")
    print(f"total points: {total}")
    for slug, b in out['brands'].items():
        print(f"  {b['name']:<16} {b['count']:>3}")


if __name__ == '__main__':
    main()
