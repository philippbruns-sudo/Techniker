# Kamera-Setup-Station (ARVOO Ruby)

Raspberry Pi + Tablet zum automatischen Einrichten von ARVOO-Ruby-Kameras.

```
 [Tablet] ──Firmen-WLAN── wlan0 [Raspberry Pi] eth0 ── [PoE] ── [Kamera]
                                  Oberfläche          nur Konfig, kein Gateway
```

## Netzwerk am Pi

Das Firmen-WLAN nutzt **192.168.8.0/24** – denselben Bereich wie die Kameras nach dem Backup.
`pi/netz-einrichten.sh` löst das so:

| eth0 bekommt | Zweck |
|---|---|
| `192.168.0.250/24` | Kameras im Werkszustand (`192.168.0.240`) |
| `192.168.8.250/32` | nur Absender-Adresse, ohne Netz-Route (vorher geprüft, dass sie im WLAN frei ist) |
| Host-Routen `192.168.8.21/32`, `192.168.8.22/32` | haben Vorrang vor dem WLAN-Netz; gesetzt per NetworkManager-Hook, sobald ein Kabel steckt |

Alles andere in 192.168.8.x läuft weiter über das WLAN. Zusätzlich antwortet der Pi
per ARP-Einstellung im WLAN nicht für `192.168.8.250`.

```bash
sudo bash ~/Techniker/kamera-setup/pi/netz-einrichten.sh            # einrichten
sudo bash ~/Techniker/kamera-setup/pi/netz-einrichten.sh --check    # prüfen (Kamera eingesteckt)
sudo bash ~/Techniker/kamera-setup/pi/netz-einrichten.sh --remove   # rückgängig
```

### Internet für die Kamera (Firmware-Update)

Im Werkszustand nutzt die Kamera Gateway und DNS `192.168.0.1`. `pi/kamera-internet.sh`
lässt den Pi diese Rolle übernehmen – **nur nach Freigabe durch die IT einschalten**:

- NAT von `192.168.0.0/24` ins WLAN, **private Netze gesperrt** (kein Zugriff aufs Firmennetz)
- aus dem WLAN nur Antworten zur Kamera, keine neuen Verbindungen
- DNS (dnsmasq) nur auf eth0, ohne DHCP – der Pi wird kein DNS-Server im Firmen-WLAN

```bash
sudo bash ~/Techniker/kamera-setup/pi/kamera-internet.sh --on
sudo bash ~/Techniker/kamera-setup/pi/kamera-internet.sh --status
sudo bash ~/Techniker/kamera-setup/pi/kamera-internet.sh --off
```

## Starten (auf dem Pi)

```bash
cd ~/Techniker && git pull
sudo python3 ~/Techniker/kamera-setup/server.py --manual
```

| Adresse | Zweck |
|---|---|
| `http://<pi-ip>:8080/test` | **Testmodus** – jeden Schritt einzeln ausführen, alle Anfragen an die Kamera mit Antwort sehen |
| `http://<pi-ip>:8080/` | Normale Oberfläche (Tablet) |

`--manual` schaltet die automatische Erkennung aus (zum Testen). Ohne `--manual` startet die
Einrichtung von selbst, sobald eine Kamera auf der Werks-IP antwortet. `sudo` wird gebraucht,
um vor dem Anmelden den ARP-Cache zu leeren (MAC der richtigen Kamera).

**Backup:** im Testmodus unter Einstellungen **„Backup hochladen“** – wird als
`backups/backup.json` gespeichert und vorher geprüft (gültiges JSON mit Netzwerk-Einstellungen).

Beim ersten Start entsteht `config.json` (nicht im Repo). Dateien, die der Server mit `sudo`
anlegt, gehören danach trotzdem dem normalen Benutzer. Im Testmodus unter **Einstellungen**
das **neue Kamera-Passwort** eintragen. Ein Backup reicht für beide Profile – die IP
(`.21` bzw. `.22`) wird beim Einspielen automatisch gesetzt.

Protokoll: `data/setup_log.csv` (MAC, Seriennummer, IP, Firmware vorher/nachher, Ergebnis).

## Ablauf pro Kamera

Bestätigt an einer Ruby LR (Firmware 4.96.1): Werks-IP `192.168.0.240`, API direkt auf **Port 80**
(`http://<ip>/api/v2/...`; Port 577 gibt es nur über die Portweiterleitung im Feld),
Benutzer `user`, Passwort = letzte 4 Zeichen der MAC in Großbuchstaben. Die Seriennummer
entspricht der MAC ohne Doppelpunkte.

| # | Schritt | Kamera-API | Stand |
|---|---|---|---|
| 1 | Kamera auf `192.168.0.240` finden | TCP Port 80 | ✅ bestätigt |
| 2 | MAC aus ARP, Passwort = letzte 4 Zeichen groß, anmelden | `GET /system/info` | ✅ bestätigt |
| 3 | Firmware-Update in **zwei Runden**: nach Update fragen, wenn eins angeboten wird, installieren – Versionsnummern werden ignoriert | `check_update`, `perform_update` `{"version": …}`, `update_progress` | ✅ Format bestätigt (Update-Tool) |
| 4 | Neues Passwort setzen, alt/neu prüfen | `POST /system/actions/set_password` (text/plain) | ⏳ an Kamera testen |
| 5 | Name setzen und zurücklesen | `POST /system/settings/` `{"name": …}` | ⏳ an Kamera testen |
| 6 | Backup einspielen (IP aus Profil) | `POST /system/actions/all_settings` oder Abschnitte einzeln | ⏳ an Kamera testen |
| 7 | Auf neuer IP warten, alle Abschnitte mit Backup vergleichen | `GET` je Abschnitt | ⏳ an Kamera testen |
| 8 | Abschlussprüfung: Version, Name | `GET /system/info`, `/system/settings/` | ⏳ an Kamera testen |

Bietet die Kamera in Runde 1 nichts an, ist der Schritt fertig (übersprungen). Nach Runde 2 wird
nicht weiter geprüft. Ein Update gilt als fertig, sobald die Kamera „finished“ meldet und wieder
erreichbar ist (spätestens nach 30 Minuten Abbruch).

Das Update läuft **vor** dem Backup, solange die Kamera noch im Werksnetz hängt
(dort kann der Pi Gateway spielen, siehe `kamera-internet.sh`).

Name und Passwort sind nicht im Backup enthalten und werden davon nicht überschrieben.

## Profile (welches Backup)

Vor dem Start wird auf dem Startbildschirm gewählt, welche Kamera eingerichtet wird.
Ein Profil verknüpft Backup-Datei, Ziel-IP und Kameraname:

| Profil | Backup-Datei | Ziel-IP | Name |
|---|---|---|---|
| `cam1` | `backup_cam1.json` | 192.168.8.21 | CAM 1 |
| `cam2` | `backup_cam2.json` | 192.168.8.22 | CAM 2 |

Die Auswahl bleibt für die nächste Kamera erhalten und ist während der Einrichtung gesperrt.
Schritt 4 (Name) und Schritt 6 (IP-Prüfung) richten sich nach dem gewählten Profil.

## Frontend

`frontend/index.html` – eine Datei, kein Build nötig.

- Ausgelegt auf einen Bildschirm im Querformat (Monitor oder Tablet quer): links Status,
  Kameradaten und Knöpfe, rechts alle Schritte – alles auf einer Seite, ohne Scrollen.
  Unter 900 px Breite (Handy, Hochformat) wird alles untereinander angezeigt.

- Im Normalbetrieb fragt die Seite jede Sekunde das Backend ab.

### Töne

Damit man nicht ständig auf den Bildschirm schauen muss:

| Ereignis | Ton |
|---|---|
| Kamera erkannt, Einrichtung startet | kurzer Doppel-Piep |
| Einrichtung erfolgreich | aufsteigender Dreiklang |
| Fehler | tiefer Warnton, **wiederholt alle 30 s**, bis jemand den Bildschirm berührt |

Die Töne werden im Browser erzeugt (Web Audio), es gibt keine Sounddateien.
Der Schalter oben rechts schaltet den Ton an/aus (wird gespeichert).
Browser geben Ton erst nach der ersten Berührung frei – bis dahin blinkt der Schalter
orange („Antippen für Ton“). Im Fully Kiosk Browser lässt sich das über die Einstellung
für automatische Medienwiedergabe umgehen.

### Demo-Modus

Simuliert Backend und Kamera komplett im Browser, zum Ausprobieren und Vorführen.

Starten: Datei direkt im Browser öffnen, `?demo` an die Adresse hängen oder auf dem
Ladebildschirm „🧪 Demo starten“ tippen (erscheint, wenn die Setup-Station nicht erreichbar ist).

Über den Knopf **🧪 Demo** unten rechts:

| Einstellung | Wirkung |
|---|---|
| Kamera anstecken / abziehen | Kamera „einstecken“; Abziehen während der Einrichtung erzeugt einen Verbindungsfehler |
| Automatisch erkennen | Kamera wird nach ein paar Sekunden von selbst „gefunden“ |
| Szenario | Alles klappt · Fehler bei bestimmtem Schritt · Zufällige Fehler (5–50 % pro Schritt) |
| Fehler bleibt beim Wiederholen | Wiederholen schlägt erneut fehl |
| Firmware schon aktuell | Update-Schritt wird übersprungen |
| Geschwindigkeit | Schnell · Normal · Realistisch |
| Töne testen | Erkannt · Erfolg · Fehler einzeln abspielen |
| Zähler zurücksetzen / Demo beenden | |

Jede Kamera bekommt eine neue Zufalls-MAC und -Seriennummer. Die Einstellungen
bleiben im Browser gespeichert. `?demo=error` startet direkt mit einem Fehler beim Backup.

### Schnittstelle Frontend ↔ Backend

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/status` | Aktueller Zustand (siehe unten) |
| `POST` | `/api/start` | Einrichtung manuell starten |
| `POST` | `/api/cancel` | Laufende Einrichtung abbrechen |
| `POST` | `/api/retry` | Ab dem fehlgeschlagenen Schritt wiederholen |
| `POST` | `/api/next` | Zurück zu „Kamera anschließen“ (nächste Kamera) |
| `POST` | `/api/profile` | Profil wählen, Body `{"id": "cam2"}` – nur im Zustand `waiting` |

`GET /api/status`:

```json
{
  "state": "waiting | running | done | error",
  "camera": {
    "mac": "00:1A:2B:3C:A1:B2",
    "serial": "RB-24-00817",
    "ip": "192.168.8.21",
    "name": "CAM 1",
    "version_before": "4.10.2",
    "version_after": "4.12.0"
  },
  "steps": [
    {
      "key": "firmware",
      "label": "Firmware-Update",
      "status": "pending | running | done | error | skipped",
      "detail": "Wird installiert ... 60 %",
      "progress": 60
    }
  ],
  "error": { "step": "backup", "message": "HTTP 400 ..." },
  "stats": { "ok": 12, "error": 1 },
  "config": {
    "default_ip": "192.168.0.21",
    "profile": "cam1",
    "profiles": [
      { "id": "cam1", "name": "CAM 1", "ip": "192.168.8.21" },
      { "id": "cam2", "name": "CAM 2", "ip": "192.168.8.22" }
    ]
  }
}
```

`steps` wird in jedem Zustand mitgeschickt, auch bei `waiting` (dann alle `pending`),
damit die rechte Seite den Ablauf schon vorab zeigt.
`progress` ist `null`, wenn der Schritt keinen Fortschrittsbalken hat.
Während `firmware` läuft, ist „Abbrechen“ gesperrt.

## Backup-Dateien

Backups enthalten API-Schlüssel und kommen **nicht** ins Repo (siehe `.gitignore`).
Sie werden nur auf dem Pi abgelegt.
