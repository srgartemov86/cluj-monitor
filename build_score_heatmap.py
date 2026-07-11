#!/usr/bin/env python3
"""Scoring heatmap для Клуж-Напоки: квадратная сетка 450 м по bbox города.

В отличие от белградского (гексы Kontur + per-hex OSM-слой), здесь всё из OSM
напрямую: жители — из жилых зданий (площадь × этажность ÷ м²/чел, та же
классификация, что в scoring.py), POI — тайловая докачка Overpass.
Скор каждой ячейки — ТА ЖЕ формула scoring.compose_score (без Dodo-штрафа:
точек Dodo в Клуже нет). Выход: public/score_heatmap_cluj.geojson.

Запуск разовый/по просьбе (5–10 мин, десятки Overpass-тайлов) — НЕ в часовом цикле.
"""
import json, math, sys, time
from collections import defaultdict

import requests

sys.path.insert(0, '/Users/dodo/.claude/scheduled-tasks/cluj-location-monitor')
import scoring
from scoring import (RES_TYPES, NONRES_BUILDINGS, LEVELS_DEFAULT, M2_BY_TYPE,
                     M2_PER_PERSON, FLOOR_EFFICIENCY, FOOD_AMENITIES, EDU_AMENITIES)

OUT = '/Users/dodo/cluj-location-monitor/public/score_heatmap_cluj.geojson'
RAW_CACHE = '/Users/dodo/cluj-location-monitor/osm_raw_cache.json'
RAW_CACHE_TTL_H = 72    # сырые здания/POI меняются медленно
UA = 'dodo-cluj-location-scout/1.0 (s.artemov@dodobrands.io)'
MIRRORS = ['https://overpass-api.de/api/interpreter',
           'https://overpass.private.coffee/api/interpreter',
           'https://maps.mail.ru/osm/tools/overpass/api/interpreter']

# Bbox города (радиус интереса 6 км от Unirii + запас 1 км на кольца res1000)
S_LAT, N_LAT = 46.715, 46.825
W_LON, E_LON = 23.500, 23.670
CELL_M = 250.0          # шаг сетки (был 450; уменьшен по просьбе 2026-07-10)
TILE = 0.022            # ~2.4 км тайлы Overpass

EARTH = 6371000.0


def hav(la1, lo1, la2, lo2):
    p = math.pi / 180
    a = (math.sin((la2 - la1) * p / 2) ** 2 +
         math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2)
    return 2 * EARTH * math.asin(math.sqrt(a))


def poly_area_m2(geom, lat0, lon0):
    if not geom or len(geom) < 3:
        return 0.0
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    pts = [((p['lon'] - lon0) * mlon, (p['lat'] - lat0) * mlat) for p in geom]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def centroid(el):
    if 'lat' in el and 'lon' in el:
        return el['lat'], el['lon']
    if 'center' in el:
        return el['center']['lat'], el['center']['lon']
    g = el.get('geometry')
    if g:
        return (sum(p['lat'] for p in g) / len(g),
                sum(p['lon'] for p in g) / len(g))
    return None


def fetch_tile(S, W, N, E):
    q = (f'[out:json][timeout:120];('
         f'way["building"]({S},{W},{N},{E});relation["building"]({S},{W},{N},{E});'
         f'nwr["amenity"]({S},{W},{N},{E});nwr["shop"]({S},{W},{N},{E});'
         f'nwr["office"]({S},{W},{N},{E});'
         f'nwr["public_transport"="platform"]({S},{W},{N},{E});'
         f'node["highway"="bus_stop"]({S},{W},{N},{E});'
         f'nwr["railway"~"^(station|tram_stop|subway_entrance|halt)$"]({S},{W},{N},{E});'
         f');out tags geom;')
    for _ in range(2):
        for m in MIRRORS:
            try:
                r = requests.post(m, data={'data': q},
                                  headers={'User-Agent': UA}, timeout=150)
                if r.status_code == 200:
                    return r.json().get('elements', [])
            except Exception:
                continue
        time.sleep(4)
    return None


def load_raw_cache():
    """Кэш сырых данных (жители-по-зданиям + POI): смена шага сетки не требует
    повторной перекачки ~36 Overpass-тайлов."""
    import os
    if not os.path.exists(RAW_CACHE):
        return None
    try:
        c = json.load(open(RAW_CACHE))
        age_h = (time.time() - c.get('fetched_at', 0)) / 3600.0
        if age_h > RAW_CACHE_TTL_H:
            return None
        print(f'raw cache hit (age {age_h:.1f}h): people={len(c["people"])}, '
              f'poi={ {k: len(v) for k, v in c["pois"].items()} }', flush=True)
        return c
    except Exception:
        return None


def main():
    cached = load_raw_cache()
    if cached:
        people = [tuple(p) for p in cached['people']]
        pois = {k: [tuple(c) for c in v] for k, v in cached['pois'].items()}
        return score_and_write(people, pois)

    # --- 1. Тайловая скачка всего bbox ---
    lat_tiles = [S_LAT + i * TILE for i in range(int((N_LAT - S_LAT) / TILE) + 1)]
    lon_tiles = [W_LON + j * TILE for j in range(int((E_LON - W_LON) / TILE) + 1)]
    tiles = [(la, lo) for la in lat_tiles for lo in lon_tiles]
    print(f'tiles: {len(tiles)}', flush=True)

    people = []   # (lat, lon, ppl) на здание
    pois = {'transit': [], 'shop': [], 'office': [], 'food': [], 'edu': []}
    seen = set()
    fails = 0
    for ti, (la, lo) in enumerate(tiles):
        els = fetch_tile(la, lo, min(la + TILE, N_LAT), min(lo + TILE, E_LON))
        if els is None:
            fails += 1
            print(f'  tile {ti+1}/{len(tiles)} FAIL', flush=True)
            continue
        n = 0
        for e in els:
            key = (e.get('type'), e.get('id'))
            if key in seen:
                continue
            seen.add(key)
            t = e.get('tags', {}) or {}
            c = centroid(e)
            if not c:
                continue
            b = t.get('building')
            if b and not (t.get('amenity') or t.get('shop') or t.get('office')):
                # жилое здание → люди (та же логика, что scoring.score_location)
                if b in NONRES_BUILDINGS:
                    continue
                if b in RES_TYPES or b == 'yes':
                    area = poly_area_m2(e.get('geometry'), c[0], c[1])
                    if area >= 25:
                        try:
                            lev = float(t.get('building:levels'))
                        except (TypeError, ValueError):
                            lev = LEVELS_DEFAULT.get(b, 3)
                        lev = max(1.0, min(lev, 35.0))
                        fac = 1.0 if b in RES_TYPES else 0.55
                        m2 = M2_BY_TYPE.get(b, M2_PER_PERSON)
                        ppl = area * lev * FLOOR_EFFICIENCY / m2 * fac
                        people.append((c[0], c[1], ppl))
                continue
            am = t.get('amenity')
            if am in FOOD_AMENITIES:
                pois['food'].append(c)
            if am in EDU_AMENITIES:
                pois['edu'].append(c)
            if t.get('shop'):
                pois['shop'].append(c)
            if t.get('office'):
                pois['office'].append(c)
            if (t.get('public_transport') == 'platform' or t.get('highway') == 'bus_stop'
                    or t.get('railway') in ('station', 'tram_stop', 'subway_entrance', 'halt')):
                pois['transit'].append(c)
            n += 1
        print(f'  tile {ti+1}/{len(tiles)} ok ({n} poi, buildings→people so far {len(people)})',
              flush=True)
        time.sleep(0.8)
    print(f'people-buildings: {len(people)}, POI:',
          {k: len(v) for k, v in pois.items()}, 'fails', fails, flush=True)
    try:
        json.dump({'fetched_at': time.time(), 'people': people, 'pois': pois},
                  open(RAW_CACHE, 'w'))
        print(f'raw cache written: {RAW_CACHE}', flush=True)
    except Exception as e:
        print(f'raw cache write failed: {e}', flush=True)
    return score_and_write(people, pois)


def score_and_write(people, pois):
    # --- 2. Spatial buckets ---
    B = 0.006

    def bucket(points):
        g = defaultdict(list)
        for p in points:
            g[(round(p[0] / B), round(p[1] / B))].append(p)
        return g

    poi_buckets = {k: bucket(v) for k, v in pois.items()}
    ppl_bucket = bucket(people)

    def count_within(cat, lat, lon, R):
        g = poi_buckets[cat]
        rb = int(R / 111320.0 / B) + 1
        bi, bj = round(lat / B), round(lon / B)
        cnt = 0
        for di in range(-rb, rb + 1):
            for dj in range(-rb, rb + 1):
                for la, lo in g.get((bi + di, bj + dj), ()):
                    if hav(lat, lon, la, lo) <= R:
                        cnt += 1
        return cnt

    def residents_within(lat, lon, R):
        rb = int(R / 111320.0 / B) + 1
        bi, bj = round(lat / B), round(lon / B)
        tot = 0.0
        for di in range(-rb, rb + 1):
            for dj in range(-rb, rb + 1):
                for la, lo, ppl in ppl_bucket.get((bi + di, bj + dj), ()):
                    if hav(lat, lon, la, lo) <= R:
                        tot += ppl
        return tot

    # --- 3. Сетка и скоринг ---
    dlat = CELL_M / 111320.0
    print('scoring grid...', flush=True)
    feats = []
    scored = 0
    lat = S_LAT
    while lat < N_LAT:
        dlon = CELL_M / (111320.0 * math.cos(math.radians(lat)))
        lon = W_LON
        while lon < E_LON:
            clat, clon = lat + dlat / 2, lon + dlon / 2
            res500 = residents_within(clat, clon, 500)
            transit = count_within('transit', clat, clon, 300)
            shops = count_within('shop', clat, clon, 300)
            if res500 < 200 and transit + shops < 3:
                lon += dlon
                continue  # пустырь/лес/река — не скорим
            res1000 = residents_within(clat, clon, 1000)
            food = count_within('food', clat, clon, 400)
            comp = count_within('food', clat, clon, 200)
            offices = count_within('office', clat, clon, 500)
            edu = count_within('edu', clat, clon, 500)
            sc = scoring.compose_score(round(res500), round(res1000), transit,
                                       shops, food, offices, edu, comp, None)
            ring = [[lon, lat], [lon + dlon, lat], [lon + dlon, lat + dlat],
                    [lon, lat + dlat], [lon, lat]]
            feats.append({'type': 'Feature',
                          'geometry': {'type': 'Polygon', 'coordinates': [ring]},
                          'properties': {'score': sc['score'], 'res500': round(res500),
                                         'transit': transit, 'shops': shops,
                                         'food': food, 'comp': comp,
                                         'offices': offices}})
            scored += 1
            lon += dlon
        lat += dlat

    json.dump({'type': 'FeatureCollection', 'features': feats}, open(OUT, 'w'))
    ss = sorted(f['properties']['score'] for f in feats)
    print(f'DONE scored={scored}', flush=True)
    if ss:
        print(f'score: min {ss[0]} median {ss[len(ss)//2]} '
              f'p90 {ss[int(len(ss)*0.9)]} max {ss[-1]}', flush=True)
    print(f'wrote {OUT}', flush=True)


if __name__ == '__main__':
    main()
