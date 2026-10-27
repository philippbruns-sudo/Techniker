# Kamera-Update (ARVOO Ruby)

Aktualisiert Kameras im Netz per API auf die neueste Firmware und protokolliert,
wann welche Kamera welches Update erhalten hat.

```
[Browser] ── http://localhost:8080 ──> [server.py] ── HTTP :577 ──> [Kameras]
                                          │
                                          └── data/update_log.csv (Protokoll)
```

Warum ein kleiner Server und nicht nur eine HTML-Seite? Browser blockieren Anfragen
einer Webseite direkt an die Kameras (CORS), besonders mit Passwort. `server.py`
übernimmt die Kommunikation und schreibt das Protokoll.

## Starten

Nur Python 3 nötig (Windows, Linux, Raspberry Pi), keine Zusatzpakete.

```bash
python3 server.py                  # http://localhost:8080
python3 server.py --demo           # zum Ausprobieren mit simulierten Kameras
python3 server.py --host 0.0.0.0   # auch von anderen Geräten im Netz erreichbar
```

Unter Windows ggf. `py server.py` statt `python3 server.py`.

Beim ersten Start unter **⚙ Einstellungen** das Kamera-Passwort eintragen.

## Bedienung

1. **＋ IPs eintragen** – eine Kamera pro Zeile, als Link oder IP, optional mit Name.
   Ohne Port gilt 577; gleiche IP mit anderem Port ist eine eigene Kamera.
   Neue Kameras werden danach automatisch geprüft.
   ```
   http://10.32.75.8:577/
   http://10.32.75.8:566/
   10.32.47.135
   10.32.47.135 Parkhaus Nord
   10.32.47.135; Parkhaus Nord; Passwort   (eigenes Passwort)
   10.32.47.135; Parkhaus Nord;            (zurück zum Standardpasswort)
   ```
2. **🔍 Alle prüfen** – liest Version und Seriennummer und fragt beim ARVOO-Server,
   ob ein Update verfügbar ist. Ändert nichts an den Kameras.
3. Aktualisieren – einzeln über **⬆** in der Zeile, mehrere per Häkchen und
   **⬆ Ausgewählte aktualisieren**, **⬆ Alle mit Update aktualisieren** oder
   **⬆ Alle aktualisieren**. Kameras, die schon aktuell sind, werden nur als
   „bereits aktuell“ protokolliert. Es laufen höchstens 3 Updates gleichzeitig (einstellbar).
   Ein Klick auf die IP öffnet die Weboberfläche der Kamera.
4. Das **Update-Protokoll** unten zeigt jedes Update mit Zeitpunkt, Version vorher/nachher
   und Ergebnis. **⬇ CSV herunterladen** öffnet es in Excel.

## Ablauf eines Updates

| Schritt | Kamera-API |
|---|---|
| Version und Seriennummer lesen | `GET /system/info` |
| Beim ARVOO-Server nach Update fragen | `POST /system/actions/check_update` |
| Update starten (Version aus `update_available`) | `POST /system/actions/perform_update` |
| Fortschritt verfolgen | `GET /system/info/update_progress` |
| Neustart abwarten, neue Version bestätigen | `GET /system/info` |

Als **erfolgreich** gilt ein Update erst, wenn die Kamera nach dem Neustart eine neue
Version meldet. Nach 30 Minuten ohne neue Version wird abgebrochen (einstellbar).

Die Kameras brauchen dafür **Internetzugang** zum ARVOO-Server.

## Protokoll

`data/update_log.csv` (Semikolon-getrennt, öffnet direkt in Excel):

| zeit | ip | name | seriennummer | version_vorher | version_nachher | ergebnis | meldung |
|---|---|---|---|---|---|---|---|
| 2026-10-27 08:28:58 | 10.32.47.135 | Parkhaus Nord | RB-24-00817 | 4.10.2 | 4.12.0 | erfolgreich | |

`ergebnis` ist `erfolgreich`, `bereits aktuell` oder `fehlgeschlagen` (mit Grund in `meldung`).

## Noch zu prüfen an einer echten Kamera

- **Format von `perform_update`:** Die API-Doku zeigt nicht, wie die Version übergeben wird.
  Standard ist **Automatisch**: Beim ersten Update probiert das Tool bei HTTP 400 nacheinander
  JSON `{"version": …}`, JSON-Text, Formularfeld, `?version=` in der Adresse, Text u. a. durch
  und speichert das erste Format, das die Kamera annimmt (steht danach unter ⚙ Einstellungen
  und im Protokoll als „Format: …“). Bitte das erste Update an **einer** Kamera starten.
  Fehlermeldungen zeigen, was gesendet wurde und was die Kamera geantwortet hat.
- **Inhalt von `update_available`:** Das Tool liest die Versionsnummer aus dem Text
  (z. B. „Version 4.12.0 available“ → `4.12.0`).
- **Anmeldung:** Standard ist Basic, alternativ Digest in den Einstellungen.

## Dateien

| Datei | Inhalt | Im Repo |
|---|---|---|
| `server.py` | Server und Kamera-Logik | ✅ |
| `index.html` | Oberfläche | ✅ |
| `config.example.json` | Beispiel-Einstellungen | ✅ |
| `config.json` | Einstellungen inkl. Passwort | ❌ (`.gitignore`) |
| `data/` | Kameraliste und Protokoll | ❌ (`.gitignore`) |
| `data-demo/` | Daten des Demo-Modus | ❌ (`.gitignore`) |
