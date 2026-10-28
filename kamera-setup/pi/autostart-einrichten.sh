#!/bin/bash
# Startet den Setup-Server automatisch beim Hochfahren des Pi (systemd-Dienst).
#
# Aufruf:  sudo bash autostart-einrichten.sh            (einrichten und starten)
#          sudo bash autostart-einrichten.sh --status   (Zustand und letzte Meldungen)
#          sudo bash autostart-einrichten.sh --remove   (Autostart entfernen)
#
# Danach:  sudo systemctl restart kamera-setup   (z. B. nach git pull)
#          journalctl -u kamera-setup -f         (Meldungen live ansehen)

SERVICE=kamera-setup
UNIT=/etc/systemd/system/$SERVICE.service
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$DIR")"

ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*"; exit 1; }

if [ "$EUID" -ne 0 ]; then
    echo "Bitte mit sudo starten: sudo bash $0"
    exit 1
fi

case "$1" in
    --status)
        systemctl --no-pager status "$SERVICE" | head -n 15
        exit 0
        ;;
    --remove)
        systemctl disable --now "$SERVICE" >/dev/null 2>&1
        rm -f "$UNIT"
        systemctl daemon-reload
        ok "Autostart entfernt"
        exit 0
        ;;
esac

[ -f "$DIR/server.py" ] || fail "server.py nicht gefunden in $DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Kamera-Setup-Station (ARVOO Ruby)
After=network-online.target NetworkManager.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Environment=PYTHONUNBUFFERED=1
Environment=SUDO_UID=$(id -u "$OWNER")
Environment=SUDO_GID=$(id -g "$OWNER")
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
ok "$UNIT angelegt (Server aus $DIR, Dateien gehören $OWNER)"

systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null 2>&1 || fail "Dienst konnte nicht gestartet werden"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    ok "Dienst läuft und startet ab jetzt bei jedem Hochfahren"
else
    fail "Dienst läuft nicht – Meldungen: journalctl -u $SERVICE -n 30"
fi
echo
echo "Nach einem git pull neu starten:  sudo systemctl restart $SERVICE"
echo "Meldungen ansehen:                journalctl -u $SERVICE -f"
