#!/usr/bin/env python3
"""
clima-fetch  —  raccoglie T/UR, stato DEH e boost raffrescatore da MyHOME.

  clima-fetch                avvia il daemon
  clima-fetch --once         fetch singolo e termina
  clima-fetch --now          segnala SIGUSR1 al daemon (fetch immediato)
  clima-fetch --import-history  ricostruisce storico DEH e termina
"""

import sys, os, time, re, json, signal, sqlite3, logging, subprocess

# ── Configurazione ────────────────────────────────────────────────────────────
MYHOME       = 'https://192.168.1.45'
PASSWORD     = ''
DEVICE_ID    = 'A0001025'   # aria: T/UR + DEH
BOOST_DEVICE = 'A0001001'   # fan coil: boost raffrescatore
INTERVAL     = 120           # 2 minuti

DB_PATH   = '/mnt/usb/clima.db'
JSON_PATH = '/mnt/usb/data.json'
PID_FILE  = '/var/run/clima-fetch.pid'
COOKIE    = '/tmp/clima-cookies.txt'

ZONE_NAMES = {'1': 'Soggiorno', '2': 'Camera', '3': 'Cameretta'}

OUTDOOR_LAT = 41.9028   # esempio: Roma — imposta le tue coordinate al deploy
OUTDOOR_LON = 12.4964

NTFY_TOPIC = ''  # topic ntfy.sh — vuoto = notifiche disabilitate
ALERT_PATH = '/mnt/usb/alert_state.json'

THR_STOP  = 58   # DEH si ferma sotto questa soglia
THR_START = 61   # DEH parte sopra questa soglia
THR_ALARM = 70   # notifica urgente

# Frame OpenWebNet DEH (dispositivo A0001025):
#   *1*1*01## / *1*1*02## = DEH ON  (keep-alive ogni ~4 min, in start_actions)
#   *1*0*01## / *1*0*02## = DEH OFF (in stop_actions)
#   *1*1*04##              = "FINE ALLARME UMIDITA" — ignorato, non indica stato DEH
DEH_ON_FRAMES  = frozenset({'*1*1*01##', '*1*1*02##'})
DEH_OFF_FRAMES = frozenset({'*1*0*01##', '*1*0*02##'})
DEH_ALL_FRAMES = DEH_ON_FRAMES | DEH_OFF_FRAMES

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s clima-fetch %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('clima-fetch')

# ── HTTP via curl ─────────────────────────────────────────────────────────────
def _get(path):
    r = subprocess.run(
        ['curl', '-sk', '-b', COOKIE, f'{MYHOME}{path}'],
        capture_output=True, text=True, timeout=30,
    )
    return r.stdout

def do_login():
    subprocess.run(
        ['curl', '-sk', '-c', COOKIE, '-d', f'password={PASSWORD}',
         f'{MYHOME}/login.php'],
        capture_output=True, timeout=30,
    )

# ── Fetch zone clima ──────────────────────────────────────────────────────────
def fetch_zones():
    raw = json.loads(_get(f'/d/{DEVICE_ID}/command.php?action=devices.list'))
    if not raw.get('success'):
        raise RuntimeError(raw.get('message', 'API error'))
    device = next((d for d in raw.get('list', []) if d['devicetype'] == 'btcn'), None)
    if not device:
        raise RuntimeError('device btcn non trovato')
    zones = []
    for seg in device['status'].split('<br>'):
        m = re.search(
            r'Zona\s+(\d+).*?([\d.]+)\s*(?:&deg;|°)C,\s*([\d.]+)\s*(?:&deg;|°)C,\s*([\d.]+)\s*RH',
            seg, re.I,
        )
        if m:
            zones.append({
                'id':       int(m.group(1)),
                'temp':     float(m.group(2)),
                'setpoint': float(m.group(3)),
                'hum':      float(m.group(4)),
            })
    if not zones:
        raise RuntimeError('nessuna zona parsata')
    return zones

# ── Fetch DEH (azioni aria) ───────────────────────────────────────────────────
def fetch_actions():
    return json.loads(_get(f'/d/{DEVICE_ID}/command.php?action=actions.status'))

def _server_ts(s):
    try:
        d, mo, y = s[:10].split('/')
        return f'{y}-{mo}-{d} {s[13:21]}'
    except Exception:
        return ''

def _server_ts_to_epoch(s):
    try:
        d, mo, y = s[:10].split('/')
        h, mi, sec = s[13:21].split(':')
        return int(time.mktime(time.strptime(
            f'{y}-{mo}-{d} {h}:{mi}:{sec}', '%Y-%m-%d %H:%M:%S'
        )))
    except Exception:
        return 0

def get_deh_state(actions):
    # Considera solo i frame DEH espliciti (ON/OFF) da entrambe le liste,
    # ignorando *1*1*04## (FINE ALLARME UMIDITA) che non indica stato DEH.
    relevant = [e for e in actions.get('start_actions', []) if e.get('frame') in DEH_ALL_FRAMES]
    relevant += [e for e in actions.get('stop_actions',  []) if e.get('frame') in DEH_ALL_FRAMES]
    if not relevant:
        return None
    latest = max(relevant, key=lambda e: _server_ts(e['time']))
    return latest['frame'] in DEH_ON_FRAMES

# ── Fetch fan coil A0001001 (zone + boost) ────────────────────────────────────
def fetch_fancoil():
    """
    Legge lo stato del dispositivo fan coil A0001001.
    Ritorna (fc_zones, boost_on):
      fc_zones  = lista di {id, ambient, setpoint, fanspeed, mode}
      boost_on  = True/False/None
    """
    try:
        raw = json.loads(_get(f'/d/{BOOST_DEVICE}/command.php?action=status'))
        if not raw.get('success'):
            return [], None
        fc_zones = [
            {
                'id':       z['address'],
                'ambient':  float(z.get('ambient',  0)),
                'setpoint': float(z.get('setpoint', 0)),
                'fanspeed': z.get('fanspeed', ''),
                'mode':     z.get('mode', ''),
            }
            for z in raw.get('zones', {}).values()
        ]
        boost = None
        for grp in raw.get('groups', {}).values():
            b = grp.get('boost')
            if b is not None:
                boost = bool(b.get('active', 0))
                break
        return fc_zones, boost
    except Exception as e:
        log.warning('fancoil fetch error: %s', e)
        return [], None

# ── Fetch meteo esterno (Open-Meteo) ─────────────────────────────────────────
def fetch_outdoor():
    """
    Legge temperatura e umidità esterna da Open-Meteo (gratuito, no API key).
    Ritorna {'temp': float, 'hum': int} oppure None in caso di errore.
    """
    try:
        url = (
            f'https://api.open-meteo.com/v1/forecast'
            f'?latitude={OUTDOOR_LAT}&longitude={OUTDOOR_LON}'
            f'&current=temperature_2m,relative_humidity_2m'
            f'&timezone=Europe%2FRome'
        )
        r = subprocess.run(
            ['curl', '-sk', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(r.stdout)
        cur = data['current']
        return {
            'temp': round(cur['temperature_2m'], 1),
            'hum':  int(cur['relative_humidity_2m']),
        }
    except Exception as e:
        log.warning('outdoor fetch error: %s', e)
        return None

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS readings (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       INTEGER NOT NULL,
        zone_id  INTEGER NOT NULL,
        temp     REAL,
        setpoint REAL,
        hum      REAL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts)')
    conn.execute('''CREATE TABLE IF NOT EXISTS deh_events (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        ts    INTEGER NOT NULL,
        state INTEGER NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS boost_events (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        ts    INTEGER NOT NULL,
        state INTEGER NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS fancoil_readings (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       INTEGER NOT NULL,
        zone_id  INTEGER NOT NULL,
        ambient  REAL,
        setpoint REAL,
        fanspeed TEXT,
        mode     TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_fc_ts ON fancoil_readings(ts)')
    conn.execute('''CREATE TABLE IF NOT EXISTS outdoor_readings (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        ts   INTEGER NOT NULL,
        temp REAL,
        hum  INTEGER
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_out_ts ON outdoor_readings(ts)')
    conn.execute('DROP TABLE IF EXISTS deh_keepalive')
    conn.commit()

def save_outdoor(conn, ts, outdoor):
    if not outdoor:
        return
    conn.execute(
        'INSERT INTO outdoor_readings (ts, temp, hum) VALUES (?,?,?)',
        (ts, outdoor['temp'], outdoor['hum']),
    )
    conn.commit()

def save_fancoil(conn, ts, fc_zones):
    if not fc_zones:
        return
    conn.executemany(
        'INSERT INTO fancoil_readings (ts, zone_id, ambient, setpoint, fanspeed, mode) VALUES (?,?,?,?,?,?)',
        [(ts, z['id'], z['ambient'], z['setpoint'], z['fanspeed'], z['mode']) for z in fc_zones],
    )
    conn.commit()

def save_zones(conn, ts, zones):
    conn.executemany(
        'INSERT INTO readings (ts, zone_id, temp, setpoint, hum) VALUES (?,?,?,?,?)',
        [(ts, z['id'], z['temp'], z['setpoint'], z['hum']) for z in zones],
    )
    conn.commit()

def _save_event(conn, table, ts, state, label):
    if state is None:
        return
    state_int = 1 if state else 0
    row = conn.execute(f'SELECT state FROM {table} ORDER BY ts DESC LIMIT 1').fetchone()
    if row is None or row[0] != state_int:
        conn.execute(f'INSERT INTO {table} (ts, state) VALUES (?,?)', (ts, state_int))
        conn.commit()
        log.info('%s → %s', label, 'ON' if state else 'OFF')

def save_deh_event(conn, ts, state):
    _save_event(conn, 'deh_events', ts, state, 'DEH')

def save_boost_event(conn, ts, state):
    _save_event(conn, 'boost_events', ts, state, 'Boost')

def _get_summary(conn, table):
    row = conn.execute(
        f'SELECT state, ts FROM {table} ORDER BY ts DESC LIMIT 1'
    ).fetchone()
    if not row:
        return None
    return {'on': bool(row[0]), 'since': row[1]}

def get_deh_summary(conn):
    return _get_summary(conn, 'deh_events')

def get_boost_summary(conn):
    return _get_summary(conn, 'boost_events')

# ── JSON ──────────────────────────────────────────────────────────────────────
def write_json(ts, zones, deh, boost, outdoor):
    data = {
        'ts':      ts,
        'deh':     deh,
        'boost':   boost,
        'outdoor': outdoor,
        'zones': zones,
    }
    tmp = JSON_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, JSON_PATH)

# ── Notifiche ntfy ────────────────────────────────────────────────────────────
def send_ntfy(title, body, tags='', priority='default'):
    if not NTFY_TOPIC:
        return
    cmd = ['curl', '-sk', '-X', 'POST', f'https://ntfy.sh/{NTFY_TOPIC}',
           '-H', f'Title: {title}',
           '-H', f'Priority: {priority}',
           '-d', body]
    if tags:
        cmd += ['-H', f'Tags: {tags}']
    try:
        subprocess.run(cmd, capture_output=True, timeout=15)
        log.info('ntfy: %s', title)
    except Exception as e:
        log.warning('ntfy error: %s', e)

def load_alert_state():
    try:
        with open(ALERT_PATH) as f:
            return json.load(f)
    except Exception:
        return None

def save_alert_state(state):
    with open(ALERT_PATH, 'w') as f:
        json.dump(state, f)

def check_and_notify(zones, deh_state, boost_state):
    prev = load_alert_state()
    eff_deh = deh_state if deh_state is not None else (prev or {}).get('deh')

    new_state = {
        'zones': {
            str(z['id']): {
                'above_alarm': z['hum'] > THR_ALARM,
                'above_stop':  z['hum'] >= THR_STOP,
                'above_start': z['hum'] >  THR_START,
            }
            for z in zones
        },
        'deh': eff_deh,
    }

    if prev is None:
        save_alert_state(new_state)
        log.info('alert_state inizializzato — nessuna notifica al primo avvio')
        return

    # DEH acceso/spento
    # ON: notifica se prev era False o None (non era esplicitamente acceso)
    # OFF: notifica solo se prev era esplicitamente True (evita None→OFF spurio)
    prev_deh = prev.get('deh')
    if deh_state is not None:
        if deh_state and not prev_deh:
            send_ntfy('DEH acceso', '', 'droplet', 'default')
        elif not deh_state and prev_deh:
            send_ntfy('DEH spento', '', 'white_check_mark', 'low')

    # Boost raffrescatore — stessa logica
    prev_boost = prev.get('boost')
    eff_boost = boost_state if boost_state is not None else prev_boost
    new_state['boost'] = eff_boost
    if boost_state is not None:
        if boost_state and not prev_boost:
            send_ntfy('❄️ Raffrescatore acceso', '', 'snowflake', 'default')
        elif not boost_state and prev_boost:
            send_ntfy('❄️ Raffrescatore spento', '', 'snowflake', 'low')

    # Zone: notifica soglia condizionale allo stato DEH corrente + allarme >70%
    for z in zones:
        zid  = str(z['id'])
        name = z['name']
        hum  = int(z['hum'])
        prev_z = prev.get('zones', {}).get(zid)
        if not isinstance(prev_z, dict):
            prev_z = {}  # primo avvio o migrazione dal vecchio formato

        # Allarme umidità >70% (indipendente da stato DEH)
        if z['hum'] > THR_ALARM and not prev_z.get('above_alarm'):
            send_ntfy(f'🚨 {name} {hum}% — allarme umidità', '', 'rotating_light', 'urgent')

        # DEH ON: zona scende sotto soglia stop → DEH ha fatto il suo lavoro
        if eff_deh and prev_z.get('above_stop') and z['hum'] < THR_STOP:
            send_ntfy(f'{name} {hum}% sotto soglia', '', 'white_check_mark', 'low')

        # DEH OFF: zona sale sopra soglia start → umidità in aumento
        if not eff_deh and not prev_z.get('above_start') and z['hum'] > THR_START:
            send_ntfy(f'{name} {hum}% sopra soglia', '', 'sweat_drops', 'default')

    save_alert_state(new_state)

# ── Import storico DEH ────────────────────────────────────────────────────────
def import_deh_history(conn):
    do_login()
    actions = fetch_actions()
    starts  = sorted(actions.get('start_actions', []), key=lambda e: _server_ts(e['time']))
    stops   = sorted(actions.get('stop_actions',  []), key=lambda e: _server_ts(e['time']))

    deh_starts = [e for e in starts if e['frame'] in DEH_ON_FRAMES]
    deh_stops  = [e for e in stops
                  if '*1*0*01##' in e.get('frame', '') or
                     '*1*0*02##' in e.get('frame', '')]

    if not deh_starts:
        log.info('Nessun evento DEH ON nei dati storici')
        return

    real_now    = int(time.time())
    server_last = _server_ts_to_epoch(deh_starts[-1]['time'])
    offset      = real_now - server_last
    log.info('Offset server→reale: %+d s (%+.1f h)', offset, offset / 3600)

    SESSION_GAP = 10 * 60
    sessions    = []
    cur         = [deh_starts[0]]
    for entry in deh_starts[1:]:
        gap = _server_ts_to_epoch(entry['time']) - _server_ts_to_epoch(cur[-1]['time'])
        if gap > SESSION_GAP:
            sessions.append(cur)
            cur = [entry]
        else:
            cur.append(entry)
    sessions.append(cur)

    conn.execute('DELETE FROM deh_events')
    for sess in sessions:
        t_on       = _server_ts_to_epoch(sess[0]['time']) + offset
        last_ka_ts = _server_ts_to_epoch(sess[-1]['time'])
        stop_after = next(
            (_server_ts_to_epoch(e['time']) + offset
             for e in deh_stops if _server_ts_to_epoch(e['time']) > last_ka_ts),
            last_ka_ts + offset + 5 * 60,
        )
        conn.execute('INSERT INTO deh_events (ts, state) VALUES (?,?)', (t_on, 1))
        conn.execute('INSERT INTO deh_events (ts, state) VALUES (?,?)', (stop_after, 0))
        log.info('  ON %s → OFF %s',
                 time.strftime('%H:%M', time.localtime(t_on)),
                 time.strftime('%H:%M', time.localtime(stop_after)))
    conn.commit()
    log.info('Importate %d sessioni (%d eventi)', len(sessions), len(sessions) * 2)

# ── Ciclo principale ──────────────────────────────────────────────────────────
_trigger = False

def _on_usr1(sig, frame):
    global _trigger
    _trigger = True
    log.info('SIGUSR1 — fetch immediato')

def do_fetch(conn):
    log.info('fetch in corso…')
    try:
        do_login()
        raw_zones             = fetch_zones()
        zones                 = [{**z, 'name': ZONE_NAMES.get(str(z['id']), f'Zona {z["id"]}')} for z in raw_zones]
        actions               = fetch_actions()
        fc_zones, boost_state = fetch_fancoil()
        outdoor               = fetch_outdoor()
        ts                    = int(time.time())
        deh_state             = get_deh_state(actions)
        save_zones(conn, ts, zones)
        save_fancoil(conn, ts, fc_zones)
        save_outdoor(conn, ts, outdoor)
        save_deh_event(conn, ts, deh_state)
        save_boost_event(conn, ts, boost_state)
        deh   = get_deh_summary(conn)
        boost = get_boost_summary(conn)
        write_json(ts, zones, deh, boost, outdoor)
        check_and_notify(zones, deh_state, boost_state)
        log.info('OK — %d zone, DEH %s, Boost %s, Esterno %s',
                 len(zones),
                 'ON' if deh_state   else 'OFF' if deh_state   is not None else '?',
                 'ON' if boost_state else 'OFF' if boost_state is not None else '?',
                 f"{outdoor['temp']}°C {outdoor['hum']}%" if outdoor else '?')
    except Exception as e:
        log.error('%s', e)

def run_daemon():
    global _trigger
    signal.signal(signal.SIGUSR1, _on_usr1)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    log.info('daemon avviato (PID %d, intervallo %ds)', os.getpid(), INTERVAL)
    try:
        while True:
            do_fetch(conn)
            deadline = time.time() + INTERVAL
            while time.time() < deadline:
                if _trigger:
                    _trigger = False
                    break
                time.sleep(2)
    finally:
        conn.close()
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if '--now' in sys.argv:
        if not os.path.exists(PID_FILE):
            sys.exit('daemon non in esecuzione')
        with open(PID_FILE) as f:
            os.kill(int(f.read()), signal.SIGUSR1)
        print('segnale inviato')
    elif '--import-history' in sys.argv:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        import_deh_history(conn)
        conn.close()
    elif '--once' in sys.argv:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        do_fetch(conn)
        conn.close()
    else:
        run_daemon()
