#!/usr/bin/env python3
import os, json, sqlite3

DB_PATH   = '/mnt/usb/clima.db'
DATA_JSON = '/mnt/usb/data.json'

# Legge i nomi zona da data.json (scritto da clima-fetch con ZONE_NAMES).
# Fallback a "Zona N" se il file non è disponibile o una zona è sconosciuta.
def _load_zone_names():
    try:
        with open(DATA_JSON) as f:
            data = json.load(f)
        return {str(z['id']): z['name'] for z in data.get('zones', [])}
    except Exception:
        return {}

ZONE_NAMES = _load_zone_names()

qs     = os.environ.get('QUERY_STRING', '')
params = dict(p.split('=', 1) for p in qs.split('&') if '=' in p)
view   = params.get('view', 'week')

print("Content-Type: application/json")
print()

try:
    conn = sqlite3.connect(DB_PATH)

    if view == 'day':
        window   = '-1 day'
        grp_expr = "strftime('%H:00', ts, 'unixepoch', 'localtime')"
        day_start = "strftime('%s','now','localtime','start of day','utc')"
        sql = f"""
            SELECT {grp_expr}, zone_id,
                   ROUND(AVG(temp),1), ROUND(MIN(temp),1), ROUND(MAX(temp),1),
                   ROUND(AVG(hum),1),  ROUND(MIN(hum),1),  ROUND(MAX(hum),1)
            FROM readings
            WHERE ts >= {day_start}
            GROUP BY 1, 2 ORDER BY 1, 2"""
        out_sql = f"""
            SELECT {grp_expr}, ROUND(AVG(hum),1), ROUND(AVG(temp),1)
            FROM outdoor_readings
            WHERE ts >= {day_start}
            GROUP BY 1 ORDER BY 1"""
    elif view == 'month':
        window   = '-30 days'
        grp_expr = "date(ts,'unixepoch','localtime')"
        sql = f"""
            SELECT {grp_expr}, zone_id,
                   ROUND(AVG(temp),1), ROUND(MIN(temp),1), ROUND(MAX(temp),1),
                   ROUND(AVG(hum),1),  ROUND(MIN(hum),1),  ROUND(MAX(hum),1)
            FROM readings
            WHERE ts >= strftime('%s','now','-30 days')
            GROUP BY 1, 2 ORDER BY 1, 2"""
        out_sql = f"""
            SELECT {grp_expr}, ROUND(AVG(hum),1), ROUND(AVG(temp),1)
            FROM outdoor_readings
            WHERE ts >= strftime('%s','now','-30 days')
            GROUP BY 1 ORDER BY 1"""
    else:  # week
        window   = '-7 days'
        grp_expr = "date(ts,'unixepoch','localtime')"
        sql = f"""
            SELECT {grp_expr}, zone_id,
                   ROUND(AVG(temp),1), ROUND(MIN(temp),1), ROUND(MAX(temp),1),
                   ROUND(AVG(hum),1),  ROUND(MIN(hum),1),  ROUND(MAX(hum),1)
            FROM readings
            WHERE ts >= strftime('%s','now','-7 days')
            GROUP BY 1, 2 ORDER BY 1, 2"""
        out_sql = f"""
            SELECT {grp_expr}, ROUND(AVG(hum),1), ROUND(AVG(temp),1)
            FROM outdoor_readings
            WHERE ts >= strftime('%s','now','-7 days')
            GROUP BY 1 ORDER BY 1"""

    pts = {}
    for label, zid, at, mint, maxt, ah, minh, maxh in conn.execute(sql).fetchall():
        k = str(zid)
        pts.setdefault(label, {})[k] = {
            'id': zid, 'name': ZONE_NAMES.get(k, f'Zona {zid}'),
            'avg_temp': at, 'min_temp': mint, 'max_temp': maxt,
            'avg_hum':  ah, 'min_hum':  minh, 'max_hum':  maxh,
        }
    points = [{'label': l, 'zones': list(z.values())} for l, z in sorted(pts.items())]

    outdoor = {}
    try:
        for label, ah, at in conn.execute(out_sql).fetchall():
            outdoor[label] = {'avg_hum': ah, 'avg_temp': at}
    except Exception:
        pass

    records = {}
    for zid, maxh, minh, maxt, mint in conn.execute(
        'SELECT zone_id, MAX(hum), MIN(hum), MAX(temp), MIN(temp) FROM readings GROUP BY zone_id'
    ).fetchall():
        k = str(zid)
        records[k] = {
            'name': ZONE_NAMES.get(k, f'Zona {zid}'),
            'max_hum': maxh, 'min_hum': minh,
            'max_temp': maxt, 'min_temp': mint,
        }

    # DEH: tutti gli eventi nella finestra + l'ultimo evento precedente
    # (per sapere se il DEH era già ON all'inizio della finestra)
    if view == 'day':
        win_start_sql = "strftime('%s','now','localtime','start of day','utc')"
    else:
        win_start_sql = f"strftime('%s','now','{window}')"
    win_start = conn.execute(
        f"SELECT CAST({win_start_sql} AS INTEGER)"
    ).fetchone()[0]

    prev = conn.execute(
        'SELECT ts, state FROM deh_events WHERE ts < ? ORDER BY ts DESC LIMIT 1',
        (win_start,)
    ).fetchone()
    deh_rows = conn.execute(
        'SELECT ts, state FROM deh_events WHERE ts >= ? ORDER BY ts',
        (win_start,)
    ).fetchall()

    # Costruisci lista sessioni {on, off} (off può essere null se ancora acceso)
    events = []
    if prev and prev[1] == 1:          # era già ON prima della finestra
        events.append({'ts': win_start, 'state': 1, 'continued': True})
    events += [{'ts': r[0], 'state': r[1]} for r in deh_rows]

    sessions = []
    cur_on = None
    for ev in events:
        if ev['state'] == 1 and cur_on is None:
            cur_on = ev['ts']
        elif ev['state'] == 0 and cur_on is not None:
            sessions.append({'on': cur_on, 'off': ev['ts']})
            cur_on = None
    if cur_on is not None:
        sessions.append({'on': cur_on, 'off': None})  # ancora acceso

    conn.close()
    print(json.dumps({
        'view':         view,
        'points':       points,
        'outdoor':      outdoor,
        'records':      records,
        'deh_sessions': sessions,
    }))

except Exception as e:
    print(json.dumps({'error': str(e)}))
