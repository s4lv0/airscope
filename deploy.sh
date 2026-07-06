#!/usr/bin/env bash
# deploy-openwrt.sh — installa o aggiorna AirScope su un router OpenWrt
#
# REQUISITI: OpenWrt con uhttpd, python3, curl, USB ext4 su /dev/sda1
# NON compatibile con Linux generico (usa apk, uci, uhttpd)
#
# Uso: ./deploy.sh [IP_ROUTER]   (default: 192.168.178.72)

set -euo pipefail

ROUTER="${1:-192.168.178.72}"
SSH_OPTS="-o HostKeyAlgorithms=+ssh-rsa -o StrictHostKeyChecking=accept-new"
SCP_OPTS="-O $SSH_OPTS"
DIR="$(cd "$(dirname "$0")" && pwd)"
FETCH_TMP=""

ok()   { echo "  [OK]  $*"; }
info() { echo "  ...   $*"; }
warn() { echo "  [!]   $*"; }
err()  { echo "  [ERR] $*" >&2; exit 1; }

cleanup() { [ -n "$FETCH_TMP" ] && rm -f "$FETCH_TMP"; }
trap cleanup EXIT

echo
echo "=== AirScope deploy → root@${ROUTER} ==="
echo

# ── Controlla file locali ────────────────────────────────────────────────────
for f in clima-fetch.py clima.html storia.html storia.py refresh.py icon.png; do
  [[ -f "$DIR/$f" ]] || err "File mancante: $DIR/$f"
done
ok "File locali trovati"

# ── Configurazione interattiva ────────────────────────────────────────────────
# Estrae il valore di una variabile stringa da clima-fetch.py
pyget() {
  python3 -c "
import re
with open('$DIR/clima-fetch.py') as f: c = f.read()
m = re.search(r\"^$1\s*=\s*'([^']*)'.*\$\", c, re.MULTILINE)
print(m.group(1) if m else '')
"
}
# Estrae il valore di una variabile numerica da clima-fetch.py
pyget_num() {
  python3 -c "
import re
with open('$DIR/clima-fetch.py') as f: c = f.read()
m = re.search(r'^$1\s*=\s*([\d.]+)', c, re.MULTILINE)
print(m.group(1) if m else '')
"
}

echo "── Configurazione ──────────────────────────────────────────────────────────"

CFG_NTFY=$(pyget NTFY_TOPIC)
CFG_MYHOME=$(pyget MYHOME)
CFG_PASSWORD=$(pyget PASSWORD)
CFG_DEVICE=$(pyget DEVICE_ID)
CFG_BOOST=$(pyget BOOST_DEVICE)
CFG_LAT=$(pyget_num OUTDOOR_LAT)
CFG_LON=$(pyget_num OUTDOOR_LON)
CFG_THR_STOP=$(pyget_num THR_STOP)
CFG_THR_START=$(pyget_num THR_START)
CFG_THR_ALARM=$(pyget_num THR_ALARM)

ask() {
  # ask <label> <current_value> <var_name>
  local label="$1" cur="$2" var="$3"
  if [ -n "$cur" ]; then
    read -r -p "  $label [$cur]: " INPUT
    echo "${INPUT:-$cur}"
  else
    warn "$label non impostato."
    read -r -p "  $label (invio per lasciare vuoto): " INPUT
    echo "${INPUT:-}"
  fi
}

CFG_MYHOME=$(ask   "BTicino IP (MYHOME)"      "$CFG_MYHOME"   "MYHOME")
CFG_PASSWORD=$(ask "BTicino password"          "$CFG_PASSWORD" "PASSWORD")
CFG_DEVICE=$(ask   "Device ID aria (T/UR/DEH)" "$CFG_DEVICE"   "DEVICE_ID")
CFG_BOOST=$(ask    "Device ID fan coil (boost)" "$CFG_BOOST"   "BOOST_DEVICE")

echo ""
if [ -z "$CFG_NTFY" ]; then
  warn "NTFY_TOPIC non impostato — le notifiche push saranno disabilitate."
fi
CFG_NTFY=$(ask "Topic ntfy.sh" "$CFG_NTFY" "NTFY_TOPIC")

echo ""
CFG_LAT=$(ask "Latitudine meteo esterno" "$CFG_LAT" "OUTDOOR_LAT")
CFG_LON=$(ask "Longitudine meteo esterno" "$CFG_LON" "OUTDOOR_LON")

echo ""
CFG_THR_STOP=$(ask  "Soglia stop DEH (%)"  "$CFG_THR_STOP"  "THR_STOP")
CFG_THR_START=$(ask "Soglia start DEH (%)" "$CFG_THR_START" "THR_START")
CFG_THR_ALARM=$(ask "Soglia allarme (%)"   "$CFG_THR_ALARM" "THR_ALARM")

echo "────────────────────────────────────────────────────────────────────────────"
echo ""

# Crea copia di clima-fetch.py con i valori configurati
FETCH_TMP=$(mktemp --suffix=.py)
python3 - <<PYEOF > "$FETCH_TMP"
import re

with open('$DIR/clima-fetch.py') as f:
    c = f.read()

def patch_str(content, key, val):
    return re.sub(
        rf"^{key}\s*=\s*'[^']*'[^\n]*",
        f"{key} = '{val}'",
        content, flags=re.MULTILINE
    )

def patch_num(content, key, val):
    return re.sub(
        rf"^{key}\s*=\s*[\d.]+[^\n]*",
        f"{key} = {val}",
        content, flags=re.MULTILINE
    )

c = patch_str(c, 'MYHOME',       '$CFG_MYHOME')
c = patch_str(c, 'PASSWORD',     '$CFG_PASSWORD')
c = patch_str(c, 'DEVICE_ID',    '$CFG_DEVICE')
c = patch_str(c, 'BOOST_DEVICE', '$CFG_BOOST')
c = patch_str(c, 'NTFY_TOPIC',   '$CFG_NTFY')
c = patch_num(c, 'OUTDOOR_LAT',  '$CFG_LAT')
c = patch_num(c, 'OUTDOOR_LON',  '$CFG_LON')
c = patch_num(c, 'THR_STOP',     '$CFG_THR_STOP')
c = patch_num(c, 'THR_START',    '$CFG_THR_START')
c = patch_num(c, 'THR_ALARM',    '$CFG_THR_ALARM')

print(c, end='')
PYEOF

ok "Configurazione applicata"

# ── Test connessione ─────────────────────────────────────────────────────────
info "Connessione SSH..."
ssh $SSH_OPTS "root@${ROUTER}" "true" 2>/dev/null || err "Impossibile connettersi a root@${ROUTER}"
ok "SSH OK"

# ── Pacchetti ───────────────────────────────────────────────────────────────
info "Verifica pacchetti (python3, curl)..."
ssh $SSH_OPTS "root@${ROUTER}" '
  missing=""
  python3 -c "import sqlite3" 2>/dev/null || missing="$missing python3-sqlite3"
  command -v curl >/dev/null 2>&1       || missing="$missing curl"
  if [ -n "$missing" ]; then
    echo "  ...   Installo:$missing"
    apk update -q && apk add -q $missing
  fi
'
ok "Pacchetti OK"

# ── Directory ────────────────────────────────────────────────────────────────
info "Creazione directory..."
ssh $SSH_OPTS "root@${ROUTER}" '
  mkdir -p /usr/local/bin /www/clima /www/cgi-bin /mnt/usb
'
ok "Directory OK"

# ── Copia file ───────────────────────────────────────────────────────────────
info "Copia clima-fetch → /usr/local/bin/clima-fetch"
scp $SCP_OPTS "$FETCH_TMP" "root@${ROUTER}:/usr/local/bin/clima-fetch"

# HTML e icona vanno sulla USB: la flash del router ha poco spazio (~10MB overlay).
# I symlink /www/clima/*.html → /mnt/usb/ vengono creati nella sezione symlink.
info "Copia clima.html, storia.html, icon.png → /mnt/usb/ (flash risparmiata)"
scp $SCP_OPTS "$DIR/clima.html" "$DIR/storia.html" "$DIR/icon.png" "root@${ROUTER}:/mnt/usb/"

info "Copia storia.py → /www/cgi-bin/storia"
scp $SCP_OPTS "$DIR/storia.py" "root@${ROUTER}:/www/cgi-bin/storia"

info "Copia refresh.py → /www/cgi-bin/refresh"
scp $SCP_OPTS "$DIR/refresh.py" "root@${ROUTER}:/www/cgi-bin/refresh"

ok "File copiati"

# ── Permessi ─────────────────────────────────────────────────────────────────
info "Permessi esecuzione..."
ssh $SSH_OPTS "root@${ROUTER}" '
  chmod 755 /usr/local/bin/clima-fetch
  chmod 755 /www/cgi-bin/storia
  chmod 755 /www/cgi-bin/refresh
'
ok "Permessi OK"

# ── Symlink data.json ─────────────────────────────────────────────────────────
info "Symlink /mnt/usb → /www/clima/ ..."
ssh $SSH_OPTS "root@${ROUTER}" '
  ln -sf /mnt/usb/data.json   /www/clima/data.json
  ln -sf /mnt/usb/clima.html  /www/clima/clima.html
  ln -sf /mnt/usb/storia.html /www/clima/storia.html
  ln -sf /mnt/usb/icon.png    /www/clima/icon.png
  ln -sf /mnt/usb/icon.png    /www/icon.png
'
ok "Symlink OK"

# ── USB automount in rc.local ─────────────────────────────────────────────────
info "Verifica USB automount in /etc/rc.local..."
ssh $SSH_OPTS "root@${ROUTER}" '
  RC=/etc/rc.local
  MOUNT_LINE="mount -t ext4 /dev/sda1 /mnt/usb 2>/dev/null || true"
  if ! grep -qF "/mnt/usb" "$RC" 2>/dev/null; then
    if grep -q "^exit 0" "$RC" 2>/dev/null; then
      sed -i "/^exit 0/i $MOUNT_LINE" "$RC"
    else
      echo "$MOUNT_LINE" >> "$RC"
    fi
    echo "  ...   Aggiunto mount USB a rc.local"
  fi
'
ok "rc.local OK"

# ── uhttpd: CGI + disabilita redirect HTTPS ──────────────────────────────────
info "Verifica configurazione uhttpd..."
ssh $SSH_OPTS "root@${ROUTER}" '
  changed=0
  CFG=/etc/config/uhttpd
  if ! grep -q "cgi_prefix" "$CFG" 2>/dev/null; then
    uci set uhttpd.main.cgi_prefix="/cgi-bin"
    changed=1
    echo "  ...   CGI abilitato"
  fi
  if [ "$(uci get uhttpd.main.redirect_https 2>/dev/null)" != "0" ]; then
    uci set uhttpd.main.redirect_https=0
    changed=1
    echo "  ...   Redirect HTTPS disabilitato"
  fi
  if [ "$changed" = "1" ]; then
    uci commit uhttpd
    /etc/init.d/uhttpd reload
  fi
'
ok "uhttpd OK"

# ── Cron ─────────────────────────────────────────────────────────────────────
info "Verifica cron..."
ssh $SSH_OPTS "root@${ROUTER}" '
  CRON_LINE="*/2 * * * * /usr/local/bin/clima-fetch --once 2>>/tmp/clima.log"
  if ! crontab -l 2>/dev/null | grep -qF "clima-fetch"; then
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "  ...   Cron aggiunto"
  fi
'
ok "Cron OK"

# ── Primo fetch ───────────────────────────────────────────────────────────────
echo
info "Eseguo primo fetch (può impiegare fino a 30s)..."
ssh $SSH_OPTS "root@${ROUTER}" '/usr/local/bin/clima-fetch --once'
ok "Primo fetch completato"

# ── Verifica data.json ────────────────────────────────────────────────────────
echo
info "Contenuto data.json:"
ssh $SSH_OPTS "root@${ROUTER}" 'cat /mnt/usb/data.json' | python3 -m json.tool 2>/dev/null || \
  ssh $SSH_OPTS "root@${ROUTER}" 'cat /mnt/usb/data.json'

echo
echo "=== Deploy completato ==="
echo "  Dashboard: http://${ROUTER}/clima/clima.html"
echo "  Storico:   http://${ROUTER}/clima/storia.html"
echo "  Log:       ssh root@${ROUTER} tail -f /tmp/clima.log"
echo
