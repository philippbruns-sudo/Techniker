#!/bin/bash
# Gibt Kameras am LAN-Port (eth0) Internetzugang über den Pi – für das
# Firmware-Update (check_update / perform_update) während der Einrichtung.
#
# Kameras im Werkszustand: 192.168.0.240, Gateway 192.168.0.1, DNS 192.168.0.1.
# Der Pi übernimmt 192.168.0.1:
#   - leitet Verkehr aus 192.168.0.0/24 per NAT ins WLAN weiter
#   - blockiert dabei alle privaten Netze (kein Zugriff aufs Firmennetz)
#   - lässt aus dem WLAN nur Antworten zur Kamera durch
#   - beantwortet DNS-Anfragen nur auf eth0 (dnsmasq, ohne DHCP)
#
# Voraussetzung: netz-einrichten.sh wurde ausgeführt (Verbindung kamera-lan).
#
# Aufruf:  sudo bash kamera-internet.sh --on
#          sudo bash kamera-internet.sh --off
#          sudo bash kamera-internet.sh --status

IFACE=eth0
UPLINK=wlan0
CON=kamera-lan
GW_ADDR=192.168.0.1/24
CAM_NET=192.168.0.0/24
NFT_FILE=/etc/kamera-setup/internet.nft
HOOK=/etc/NetworkManager/dispatcher.d/91-kamera-internet
SYSCTL=/etc/sysctl.d/91-kamera-internet.conf
DNSMASQ_CONF=/etc/dnsmasq.d/kamera-setup.conf

ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*"; }
fail() { echo "  ✗ $*"; exit 1; }

apply_nft() {
    nft delete table inet kamera_setup 2>/dev/null
    nft -f "$NFT_FILE"
}

status() {
    echo "Status:"
    if ip -br addr show "$IFACE" | grep -q "192.168.0.1/"; then ok "$IFACE hat 192.168.0.1 (Gateway für die Kamera)"; else warn "$IFACE hat 192.168.0.1 nicht (Kabel gesteckt?)"; fi
    if [ "$(sysctl -n net.ipv4.ip_forward)" = "1" ]; then ok "Weiterleitung aktiv"; else warn "Weiterleitung aus"; fi
    if nft list table inet kamera_setup >/dev/null 2>&1; then ok "Firewall/NAT-Regeln geladen"; else warn "Firewall/NAT-Regeln nicht geladen"; fi
    if systemctl is-active --quiet dnsmasq; then ok "DNS (dnsmasq) läuft"; else warn "DNS (dnsmasq) läuft nicht"; fi
    wlan_ip=$(ip -4 -br addr show "$UPLINK" | awk '{print $3}' | cut -d/ -f1)
    if [ -n "$wlan_ip" ] && ss -lun 2>/dev/null | grep -q "$wlan_ip:53"; then
        warn "dnsmasq lauscht auch im WLAN ($wlan_ip) – bitte melden!"
    else
        ok "DNS nur am Kabel, nicht im WLAN"
    fi
    if curl -s -o /dev/null --max-time 5 https://www.google.com; then ok "Pi selbst hat Internet"; else warn "Pi selbst erreicht das Internet nicht"; fi
}

if [ "$EUID" -ne 0 ]; then
    echo "Bitte mit sudo starten: sudo bash $0 --on|--off|--status"
    exit 1
fi

case "$1" in
    --status)
        status
        ;;

    --off)
        nft delete table inet kamera_setup 2>/dev/null && ok "Firewall/NAT-Regeln entfernt" || ok "keine Regeln geladen"
        rm -f "$HOOK" "$NFT_FILE" "$SYSCTL" "$DNSMASQ_CONF"
        sysctl -w net.ipv4.ip_forward=0 >/dev/null && ok "Weiterleitung aus"
        systemctl disable --now dnsmasq >/dev/null 2>&1 && ok "DNS (dnsmasq) gestoppt"
        nmcli con modify "$CON" -ipv4.addresses "$GW_ADDR" >/dev/null 2>&1 && ok "192.168.0.1 von $IFACE entfernt"
        nmcli con up "$CON" >/dev/null 2>&1
        ;;

    --on)
        nmcli -t -f NAME con show | grep -qx "$CON" || fail "Verbindung $CON fehlt – zuerst netz-einrichten.sh ausführen"

        echo "1) DNS-Konfiguration (nur auf $IFACE, ohne DHCP)"
        mkdir -p /etc/dnsmasq.d
        printf 'interface=%s\nbind-dynamic\nno-dhcp-interface=%s\n' "$IFACE" "$IFACE" > "$DNSMASQ_CONF"
        ok "$DNSMASQ_CONF"

        echo "2) Pakete (nftables, dnsmasq)"
        if ! command -v nft >/dev/null || ! dpkg -s dnsmasq >/dev/null 2>&1; then
            apt-get update >/dev/null
            apt-get install -y nftables dnsmasq >/dev/null || fail "Installation fehlgeschlagen (Internet am Pi?)"
        fi
        ok "installiert"

        echo "3) Firewall- und NAT-Regeln"
        mkdir -p "$(dirname "$NFT_FILE")"
        cat > "$NFT_FILE" <<EOF
table inet kamera_setup {
    chain forward {
        type filter hook forward priority 0; policy drop;
        iifname "$UPLINK" oifname "$IFACE" ct state established,related accept
        iifname "$IFACE" oifname "$UPLINK" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10 } drop
        iifname "$IFACE" oifname "$UPLINK" ip saddr $CAM_NET accept
    }
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
        oifname "$UPLINK" ip saddr $CAM_NET masquerade
    }
}
EOF
        apply_nft || fail "Regeln konnten nicht geladen werden"
        ok "$NFT_FILE geladen"

        echo "4) Weiterleitung einschalten"
        echo "net.ipv4.ip_forward = 1" > "$SYSCTL"
        sysctl -p "$SYSCTL" >/dev/null
        ok "an"

        echo "5) Hook, damit Regeln nach Neustart/Kabelwechsel wieder geladen werden"
        {
            echo '#!/bin/sh'
            echo "[ \"\$1\" = \"$IFACE\" ] || exit 0"
            echo "[ \"\$2\" = \"up\" ] || exit 0"
            echo "nft delete table inet kamera_setup 2>/dev/null"
            echo "nft -f $NFT_FILE"
        } > "$HOOK"
        chmod 755 "$HOOK"
        ok "$HOOK"

        echo "6) 192.168.0.1 auf $IFACE"
        nmcli con modify "$CON" +ipv4.addresses "$GW_ADDR" >/dev/null || fail "nmcli konnte die Adresse nicht hinzufügen"
        nmcli con up "$CON" >/dev/null 2>&1 || warn "Kein Kabel an $IFACE – wird aktiv, sobald eines steckt"
        ok "hinzugefügt"

        echo "7) DNS starten"
        systemctl enable dnsmasq >/dev/null 2>&1
        systemctl restart dnsmasq || warn "dnsmasq startet nicht – siehe: journalctl -u dnsmasq -n 20"
        ok "gestartet"

        echo
        status
        ;;

    *)
        echo "Aufruf: sudo bash $0 --on | --off | --status"
        exit 1
        ;;
esac
