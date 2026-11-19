#!/usr/bin/env python3
"""Полная разовая проверка живости ВСЕХ активных лотов (штатный check_status
проверяет по 12–20 на источник за цикл). Только читает: печатает вердикты и
пишет JSON со списком мёртвых ключей в файл из argv[1]. Ничего не меняет.

    CLUJ_DATA=data python3 full_status_check.py /path/to/dead.json
"""
import collections, json, os, sys, time
import check_status as cs

STATE = os.path.join(os.environ.get('CLUJ_DATA', 'data'), 'state.json')


def main():
    L = json.load(open(STATE, encoding='utf-8'))['listings']
    act = [(k, v) for k, v in L.items()
           if v.get('in_sheet') and not v.get('removed_from_sheet') and not v.get('rejected')]
    res = collections.defaultdict(list)
    for k, v in act:
        src = v.get('source') or ''
        _, _, url, code, size, title, verdict = cs.check_one(k, src, v['url'])
        res[(src, verdict)].append((k, code, (title or '')[:60], v['url']))
        time.sleep(0.4)
    for (src, verdict), items in sorted(res.items()):
        print(f'{src:14} {verdict:13} {len(items)}')
    print('--- dead:')
    dead = []
    for (src, verdict), items in res.items():
        if verdict == 'dead':
            for k, code, title, url in items:
                dead.append(k)
                print(f'  {k} http={code} title={title!r}')
    print('--- unknown sample:')
    for (src, verdict), items in res.items():
        if verdict in ('unknown', 'rate_limited'):
            for k, code, title, url in items[:3]:
                print(f'  {k} http={code} title={title!r}')
    if len(sys.argv) > 1:
        json.dump(dead, open(sys.argv[1], 'w'))


if __name__ == '__main__':
    main()
