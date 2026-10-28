#!/bin/bash
# Richtet den LAN-Port (eth0) des Pi für die Kamera-Einrichtung ein.
#
# Problem: Das Firmen-WLAN nutzt 192.168.8.0/24 – denselben Bereich wie die
# Kameras nach dem Backup (192.168.8.21 / .22).
# Lösung:  eth0 bekommt
#   - 192.168.0.250/24   für Kameras im Werkszustand (192.168.0.240)
#   - 192.168.8.250/32   nur als Absender, ohne Netz-Route
#   - Host-Routen für .21 und .22 über eth0 (haben Vorrang vor dem WLAN-/24),
#     gesetzt von einem NetworkManager-Hook, sobald ein Kabel steckt
# Alles andere in 192.168.8.x läuft weiter über das WLAN.
#
# Aufruf:  sudo bash netz-einrichten.sh            (einrichten)
#          sudo bash netz-einrichten.sh --check    (nur prüfen)
#          sudo bash netz-einrichten.sh --remove   (wieder entfernen)

IFACE=eth0
CON=kamera-lan
FACTORY_ADDR=192.168.0.250/24
SRC_ADDR=192.168.8.250
CAMERA_IPS="192.168.8.21 192.168.8.22"
HOOK=/etc/NetworkManager/dispatcher.d/90-kamera-routes
SYSCTL=/etc/sysctl.d/90-kamera-setup.conf

ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*"; }
fail() { echo "  ✗ $*"; exit 1; }

route_dev() {
    ip route get "$1" 2>/dev/null | grep -o 'dev [^ ]*' | cut -d' ' -f2
}

set_routes() {
    for ip in $CAMERA_IPS; do
        ip route replace "$ip/32" dev "$IFACE" src "$SRC_ADDR"
    done
}

check() {
    echo "Prüfung:"
    if ip -br addr show "$IFACE" | grep -q "192.168.0.250"; then ok "$IFACE hat 192.168.0.250"; else warn "$IFACE hat 192.168.0.250 nicht (Kabel gesteckt?)"; fi
    if ip -br addr show "$IFACE" | grep -q "$SRC_ADDR"; then ok "$IFACE hat $SRC_ADDR"; else warn "$IFACE hat $SRC_ADDR nicht (Kabel gesteckt?)"; fi
    for ip in 192.168.0.240 $CAMERA_IPS; do
        dev=$(route_dev "$ip")
        if [ "$dev" = "$IFACE" ]; then ok "$ip läuft über $IFACE"; else warn "$ip läuft über '${dev:-?}' statt $IFACE"; fi
    done
    dev=$(route_dev 192.168.8.1)
    if [ "$dev" = "wlan0" ]; then ok "Firmen-Gateway 192.168.8.1 weiter über wlan0"; else warn "192.168.8.1 läuft über '${dev:-?}'"; fi
    if [ "$(sysctl -n net.ipv4.conf.all.arp_ignore)" = "1" ]; then ok "ARP-Schutz aktiv"; else warn "ARP-Schutz nicht aktiv"; fi
}

if [ "$EUID" -ne 0 ]; then
    echo "Bitte mit sudo starten: sudo bash $0"
    exit 1
fi

case "$1" in
    --check)
        check
        exit 0
        ;;
    --remove)
        nmcli con delete "$CON" >/dev/null 2>&1 && ok "Verbindung $CON entfernt" || warn "Verbindung $CON gab es nicht"
        rm -f "$HOOK" && ok "Routen-Hook entfernt"
        rm -f "$SYSCTL"
        sysctl -w net.ipv4.conf.all.arp_ignore=0 net.ipv4.conf.all.arp_announce=0 >/dev/null
        ok "ARP-Einstellungen zurückgesetzt"
        exit 0
        ;;
esac

echo "1) Ist $SRC_ADDR im Firmen-WLAN frei?"
if ping -c 2 -W 1 -I wlan0 "$SRC_ADDR" >/dev/null 2>&1; then
    fail "$SRC_ADDR antwortet im WLAN – Adresse ist belegt. Im Script SRC_ADDR ändern."
fi
ok "$SRC_ADDR antwortet nicht – wird verwendet"

echo "2) ARP-Schutz (Pi antwortet für $SRC_ADDR nicht im WLAN)"
printf 'net.ipv4.conf.all.arp_ignore = 1\nnet.ipv4.conf.all.arp_announce = 2\n' > "$SYSCTL"
sysctl -p "$SYSCTL" >/dev/null
ok "gesetzt"

echo "3) Verbindung $CON für $IFACE anlegen"
nmcli con delete "$CON" >/dev/null 2>&1
nmcli con add type ethernet ifname "$IFACE" con-name "$CON" \
    ipv4.method manual \
    ipv4.addresses "$FACTORY_ADDR, $SRC_ADDR/32" \
    ipv4.never-default yes \
    ipv6.method disabled \
    connection.autoconnect yes >/dev/null || fail "nmcli konnte die Verbindung nicht anlegen"
ok "angelegt"

echo "4) Hook für die Kamera-Routen"
{
    echo '#!/bin/sh'
    echo "[ \"\$1\" = \"$IFACE\" ] || exit 0"
    echo "[ \"\$2\" = \"up\" ] || exit 0"
    for ip in $CAMERA_IPS; do
        echo "ip route replace $ip/32 dev $IFACE src $SRC_ADDR"
    done
} > "$HOOK"
chmod 755 "$HOOK"
ok "$HOOK angelegt"

echo "5) Verbindung starten"
if nmcli con up "$CON" >/dev/null 2>&1; then
    set_routes
    ok "aktiv"
else
    warn "Kein Kabel an $IFACE – wird aktiv, sobald eines steckt"
fi

echo
check
echo
echo "Fertig. Ohne Kabel an $IFACE sind Adressen und Routen noch nicht aktiv –"
echo "nach dem Einstecken einer Kamera:  sudo bash $0 --check"
