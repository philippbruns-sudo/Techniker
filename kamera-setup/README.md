# Kamera-Setup-Station (ARVOO Ruby)

Raspberry Pi + Tablet zum automatischen Einrichten von ARVOO-Ruby-Kameras.

```
 [Tablet] ──Firmen-WLAN── wlan0 [Raspberry Pi] eth0 ── [PoE] ── [Kamera]
                                  Oberfläche          nur Konfig, kein Gateway
```

## Ablauf pro Kamera

| # | Schritt | Kamera-API |
|---|---|---|
| 1 | Kamera auf Werks-IP finden (192.168.0.21, noch zu bestätigen) | `GET /system/info/up` |
| 2 | MAC per ARP auslesen, Passwort = letzte 4 Zeichen, anmelden | `GET /system/info` |
| 3 | Passwort ändern (Body: `text/plain`, nur das neue Passwort) | `POST /system/actions/set_password` |
| 4 | Name „CAM 1“ setzen und zurücklesen | `POST /api/v2/system/settings/` |
| 5 | Backup einspielen (ANPR → Base → Netzwerk zuletzt) | `POST /system/actions/all_settings` |
| 6 | Auf 192.168.8.21 warten, Einstellungen zurücklesen und vergleichen | `GET /network/settings` |
| 7 | Firmware hochladen, Fortschritt verfolgen | `POST /system/actions/upload_update`, `GET /system/info/update_progress` |
| 8 | Abschlussprüfung (Version, Name, IP) | `GET /system/info` |

Name und Passwort sind nicht im Backup enthalten und werden davon nicht überschrieben.

## Frontend

`frontend/index.html` – eine Datei, kein Build nötig.

- **Demo-Modus:** Datei direkt im Browser öffnen oder `?demo` anhängen.
  `?demo=error` simuliert einen Fehler beim Backup.
- Ansonsten fragt die Seite jede Sekunde das Backend ab.

### Schnittstelle Frontend ↔ Backend

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/status` | Aktueller Zustand (siehe unten) |
| `POST` | `/api/start` | Einrichtung manuell starten |
| `POST` | `/api/cancel` | Laufende Einrichtung abbrechen |
| `POST` | `/api/retry` | Ab dem fehlgeschlagenen Schritt wiederholen |
| `POST` | `/api/next` | Zurück zu „Kamera anschließen“ (nächste Kamera) |

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
  "config": { "default_ip": "192.168.0.21" }
}
```

`progress` ist `null`, wenn der Schritt keinen Fortschrittsbalken hat.
Während `firmware` läuft, ist „Abbrechen“ gesperrt.

## Backup-Dateien

Backups enthalten API-Schlüssel und kommen **nicht** ins Repo (siehe `.gitignore`).
Sie werden nur auf dem Pi abgelegt.
