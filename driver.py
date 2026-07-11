#!/usr/bin/env python3
"""driver.py — серверный оркестратор часового цикла (замена CCD-агента).

Делает то, что на Mac делал Claude-агент по SKILL.md:
  1. cycle.py (process phase) → JSON
  2. passes → альбом фото + caption в Telegram → --mark-sent
     Вместо LLM-перевода румынского Summary — глоссарий-замена терминов
     (команда партнёра читает остальное через built-in Translate в Telegram).
  3. price_changes → текстовое сообщение (reply на исходный лот, если есть id)
  4. cycle.py --finalize (check_status + gen_map + surge deploy)

Запускается из GitHub Actions (см. .github/workflows/monitor.yml).
Env: CLUJ_DATA, TG_SESSION, TG_API_ID, TG_API_HASH, GOOGLE_TOKEN_PATH,
     CLUJ_CHAT_ID (default 3828339567).
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent
CHAT_ID = os.environ.get('CLUJ_CHAT_ID', '3828339567')
MANY_PASSES = 15  # SKILL: при ≥15 лотов — одна сводка вместо N сообщений

# Глоссарий ro→en (SKILL.md): термины недвижимости, по которым принимается решение.
# Остальной текст остаётся румынским — Telegram Translate в один тап.
GLOSSARY = [
    (r'\bspa[țt]iu comercial\b', 'commercial space'),
    (r'\bvad comercial\b', 'foot traffic'),
    (r'\bvad pietonal\b', 'foot traffic'),
    (r'\bparter\b', 'ground floor'),
    (r'\bvitrin[ăa]\b', 'display window'),
    (r'\bla strad[ăa]\b', 'street-facing'),
    (r'\bstradal[ăa]?\b', 'street-facing'),
    (r'\bchirie\b', 'rent'),
    (r'\bgaran[țt]ie\b', 'deposit'),
    (r'\b[îi]ntre[țt]inere\b', 'maintenance'),
    (r'\brenovat[ăa]?\b', 'renovated'),
    (r'\bfinisat[ăa]?\b', 'fitted out'),
    (r'\bautoriza[țt]ie de func[țt]ionare\b', 'operating permit'),
    (r'\balimenta[țt]ie public[ăa]\b', 'food service'),
    (r'\bteras[ăa]\b', 'terrace'),
    (r'\bcentral[ăa] termic[ăa]\b', 'own heating unit'),
    (r'\bgeamuri? termopan\b', 'double glazing'),
]


def glossary_en(text):
    for pat, repl in GLOSSARY:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


def run_json(args, timeout):
    """Запуск python-скрипта, JSON — последняя непустая строка stdout."""
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                       timeout=timeout, cwd=HERE)
    sys.stderr.write(r.stderr or '')
    lines = [l for l in (r.stdout or '').strip().splitlines() if l.strip()]
    for line in reversed(lines):
        if line.lstrip().startswith('{'):
            return json.loads(line)
    raise RuntimeError(f'{args[0]}: no JSON in output (rc={r.returncode})')


def send_album(caption, photos):
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                     encoding='utf-8') as f:
        f.write(caption)
        cap_path = f.name
    try:
        return run_json(['send_album.py', CHAT_ID, cap_path] + photos[:10], 120)
    finally:
        os.unlink(cap_path)


def send_text(text, reply_to=None):
    args = ['send_text.py', CHAT_ID, '-']
    if reply_to:
        args += ['--reply-to', str(reply_to)]
    r = subprocess.run([sys.executable] + args, input=text, capture_output=True,
                       text=True, timeout=60, cwd=HERE)
    sys.stderr.write(r.stderr or '')
    return r.returncode == 0


def main():
    out = run_json(['cycle.py'], 900)
    if out.get('concurrent_cycle_running'):
        print('concurrent cycle running — exit')
        return 0

    passes = out.get('passes') or []
    sent = failed = 0

    if len(passes) >= MANY_PASSES:
        lines = [f'🍕 {len(passes)} new locations (bulk):']
        for p in passes:
            lines.append(f"· {p.get('district','?')} · {p.get('area')} m² · "
                         f"{p.get('price')} €/mo · {p.get('url','')}")
        if send_text('\n'.join(lines)[:4000]):
            # bulk: mark-sent без message_id привязки к лоту — ставим 0
            for p in passes:
                subprocess.run([sys.executable, 'cycle.py', '--mark-sent',
                                p['listing_key'], '0'],
                               capture_output=True, cwd=HERE, timeout=120)
            sent = len(passes)
    else:
        for p in passes:
            caption = glossary_en(p.get('caption') or '')[:1024]
            photos = [ph for ph in (p.get('photo_paths') or []) if os.path.exists(ph)]
            try:
                if photos:
                    res = send_album(caption, photos)
                    mid = res.get('first_message_id')
                else:
                    mid = 0 if send_text(caption) else None
                if mid is None:
                    raise RuntimeError('send failed')
                m = re.search(r'📝 Summary:\s*(.+)', caption, re.DOTALL)
                summary = (m.group(1).strip() if m else caption)[:900]
                subprocess.run([sys.executable, 'cycle.py', '--mark-sent',
                                p['listing_key'], str(mid), '--desc-ru', summary],
                               capture_output=True, cwd=HERE, timeout=120)
                sent += 1
            except Exception as e:
                # не mark-sent → лот останется unalerted, доедет следующим циклом
                print(f"  SEND FAIL {p.get('listing_key')}: {e}", file=sys.stderr)
                failed += 1

    for c in out.get('price_changes') or []:
        msg = (f"💶 Price changed · {c.get('district','?')} "
               f"{c.get('address','')} — was {c['old']} € → now {c['new']} €/mo\n"
               f"🔗 {c.get('url','')}")
        send_text(msg, reply_to=c.get('reply_to_message_id'))

    fin = run_json(['cycle.py', '--finalize'], 600)

    s = out.get('summary') or {}
    print(json.dumps({
        'sweep': s.get('sweep_raw'), 'new': s.get('new'),
        'passes': len(passes), 'sent': sent, 'send_failed': failed,
        'rejects': s.get('rejects'), 'duplicates': s.get('duplicates'),
        'price_changes': len(out.get('price_changes') or []),
        'sources_down': s.get('sources_down'),
        'map_ok': fin.get('map_ok'), 'surge_ok': fin.get('map_surge_ok'),
    }, ensure_ascii=False))
    # Упавшая отправка не должна валить workflow: state цел, лот доедет позже
    return 0


if __name__ == '__main__':
    sys.exit(main())
