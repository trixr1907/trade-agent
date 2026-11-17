#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# start_server.sh — Production-grade Starter für trade-agent
# Pfad: /mnt/c/dev/trading/trade-agent/start_server.sh
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"
LOG="$SCRIPT_DIR/trade-agent.log"
PID_FILE="$SCRIPT_DIR/trade-agent.pid"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

# ── 1. .env laden (falls vorhanden) ──────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +o allexport
    echo "[INFO] .env geladen"
else
    echo "[WARN] Keine .env gefunden — Defaults werden verwendet"
fi

# ── 2. VENV sicherstellen ─────────────────────────────────────────
if [ ! -f "$VENV/bin/python" ]; then
    echo "[INFO] Erstelle Virtual Environment …"
    python3 -m venv "$VENV"
fi

# ── 3. Abhängigkeiten installieren falls nötig ───────────────────
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "[INFO] Prüfe / installiere Abhängigkeiten …"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# ── 4. Import-Smoke-Test ─────────────────────────────────────────
if ! "$VENV/bin/python" -c "from tradeagent.api.app import app" 2>/dev/null; then
    echo "[ERROR] Import fehlgeschlagen — bitte requirements prüfen"
    exit 1
fi

# ── 5. Alt-Prozess sauber beenden ───────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[INFO] Stoppe alten Prozess (PID $OLD_PID) …"
        kill "$OLD_PID"
        sleep 1
        # Notfall-Kill falls nötig
        kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# ── 6. Server starten ────────────────────────────────────────────
echo "[INFO] Starte trade-agent auf ${HOST}:${PORT} …"
nohup "$VENV/bin/uvicorn" tradeagent.api.app:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level info \
    >> "$LOG" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo "[INFO] PID=$SERVER_PID | Log: $LOG"

# ── 7. Health-Check (max. 10s warten) ────────────────────────────
echo "[INFO] Warte auf Server …"
for i in $(seq 1 10); do
    sleep 1
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[ERROR] Prozess ist unerwartet beendet. Log-Tail:"
        tail -20 "$LOG"
        rm -f "$PID_FILE"
        exit 1
    fi
    HTTP_CODE=$("$VENV/bin/python" -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://localhost:${PORT}/health', timeout=2)
    print(r.getcode())
except Exception:
    print(0)
" 2>/dev/null)
    if [ "$HTTP_CODE" = "200" ]; then
        echo ""
        echo "╔══════════════════════════════════════════╗"
        echo "║  ✅  trade-agent läuft                   ║"
        echo "║  http://localhost:${PORT}                     ║"
        echo "║  PID: ${SERVER_PID}                           ║"
        echo "║  Log: trade-agent.log                    ║"
        echo "╚══════════════════════════════════════════╝"
        exit 0
    fi
    printf "."
done

echo ""
echo "[ERROR] Health-Check nach 10s nicht erfolgreich. Log-Tail:"
tail -20 "$LOG"
exit 1