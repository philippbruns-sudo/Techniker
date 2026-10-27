#!/usr/bin/env python3
"""
Kamera-Update: aktualisiert ARVOO-Kameras im Netz per API und protokolliert jedes Update.

Start:   python3 server.py            -> http://localhost:8080
Demo:    python3 server.py --demo     (simulierte Kameras, eigener Datenordner)
Tablet:  python3 server.py --host 0.0.0.0   (Oberfläche auch im Netzwerk erreichbar)

Nur Python 3 nötig, keine Zusatzpakete. Daten liegen in ./data (bzw. ./data-demo):
  cameras.json     Kameraliste mit letztem bekannten Stand
  update_log.csv   Protokoll aller Updates (Semikolon, für Excel)
"""

import argparse
import base64
import csv
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

DEFAULT_CONFIG = {
    'user': 'user',
    'password': '',
    'port': 577,
    'api_prefix': '/api/v2',
    'auth': 'basic',                 # basic | digest
    'perform_update_body': 'text',   # text | json | none  (Format noch nicht verifiziert)
    'parallel': 3,                   # wie viele Kameras gleichzeitig aktualisiert werden
    'timeout_s': 10,
    'update_timeout_min': 30,
}

LOG_FIELDS = ['zeit', 'ip', 'name', 'seriennummer', 'version_vorher', 'version_nachher', 'ergebnis', 'meldung']

DEMO = False
DATA_DIR = os.path.join(BASE_DIR, 'data')
lock = threading.RLock()
cameras = {}        # ip -> dict (gespeichert + Laufzeitstatus)
config = {}
update_slots = None


# ==========================================
# 1. KONFIGURATION & DATEIEN
# ==========================================

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg.update(json.load(f))
    return cfg


def save_config():
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def cameras_file():
    return os.path.join(DATA_DIR, 'cameras.json')


def log_file():
    return os.path.join(DATA_DIR, 'update_log.csv')


STORED_KEYS = ['ip', 'name', 'password', 'version', 'serial', 'model', 'update_available', 'last_check', 'last_update']


def load_cameras():
    if not os.path.exists(cameras_file()):
        return
    with open(cameras_file(), encoding='utf-8') as f:
        for c in json.load(f):
            c['ip'] = normalize_address(c['ip'])
            cameras[c['ip']] = new_camera(c)


def save_cameras():
    with lock:
        data = [{k: c.get(k) for k in STORED_KEYS} for c in cameras.values()]
    tmp = cameras_file() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, cameras_file())


def new_camera(stored):
    c = {k: stored.get(k) for k in STORED_KEYS}
    c.update({'status': 'idle', 'detail': '', 'progress': None, 'busy': False, 'online': None})
    return c


def write_log(entry):
    path = log_file()
    with lock:
        is_new = not os.path.exists(path)
        with open(path, 'a', newline='', encoding='utf-8-sig' if is_new else 'utf-8') as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS, delimiter=';')
            if is_new:
                w.writeheader()
            w.writerow(entry)


def read_log():
    if not os.path.exists(log_file()):
        return []
    with open(log_file(), newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f, delimiter=';'))


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ==========================================
# 2. KAMERA-API
# ==========================================

class CameraError(Exception):
    pass


class CameraClient:
    """Spricht mit der ARVOO-API einer Kamera."""

    def __init__(self, ip, password=None):
        host = ip if ':' in ip else '%s:%s' % (ip, config['port'])
        self.base = 'http://%s%s' % (host, config['api_prefix'])
        self.user = config['user']
        self.password = password or config['password']
        self.timeout = config['timeout_s']
        if config['auth'] == 'digest':
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.base, self.user, self.password)
            self.opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        else:
            self.opener = urllib.request.build_opener()

    def call(self, method, path, body=None):
        headers = {}
        data = None
        if config['auth'] != 'digest':
            token = base64.b64encode(('%s:%s' % (self.user, self.password)).encode()).decode()
            headers['Authorization'] = 'Basic ' + token
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        elif body is not None:
            data = str(body).encode()
            headers['Content-Type'] = 'text/plain'
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                raw = res.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise CameraError('Anmeldung abgelehnt (HTTP 401) – Passwort prüfen')
            raise CameraError('%s %s: HTTP %s' % (method, path, e.code))
        except (urllib.error.URLError, OSError) as e:
            raise CameraError('nicht erreichbar (%s)' % getattr(e, 'reason', e))
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode('utf-8', 'replace')


class FakeCamera:
    """Simulierte Kamera für den Demo-Modus."""

    LATEST = '4.12.0'
    state = {}

    def __init__(self, ip, password=None):
        self.ip = ip
        if ip not in FakeCamera.state:
            last = int(re.sub(r'\D', '', ip.split(':')[0].split('.')[-1]) or 0)
            FakeCamera.state[ip] = {
                'version': random.choice(['4.9.7', '4.10.2', '4.11.0', FakeCamera.LATEST]),
                'serial': 'RB-%d-%05d' % (random.randint(23, 25), random.randint(0, 99999)),
                'offline': last % 10 == 9,      # IPs auf ...9 sind "offline"
                'breaks': last % 10 == 7,       # IPs auf ...7 schlagen beim Update fehl
                'update_started': None,
                'checked': False,
            }
        self.s = FakeCamera.state[ip]

    def call(self, method, path, body=None):
        s = self.s
        time.sleep(random.uniform(0.2, 0.6))
        if s['offline']:
            raise CameraError('nicht erreichbar (timed out)')
        phase, elapsed = self._phase()
        if phase == 'rebooting':
            raise CameraError('nicht erreichbar (Connection refused)')
        if path == '/system/info':
            return {'model': 'Ruby', 'serial': s['serial'], 'version': s['version'],
                    'update_available': self.LATEST if s['checked'] and s['version'] != self.LATEST else '',
                    'update_progress': {'phase': phase or 'finished', 'error': False}}
        if path == '/system/actions/check_update':
            s['checked'] = True
            return {'update_available': self.LATEST if s['version'] != self.LATEST else '', 'license': 'ok'}
        if path == '/system/actions/perform_update':
            s['update_started'] = time.time()
            return {'success': True}
        if path == '/system/info/update_progress':
            if phase == 'failed':
                return {'phase': 'installing', 'error': True}
            return {'phase': phase or 'finished', 'error': False,
                    'percent': min(99, int(elapsed / 25 * 100)) if phase else 100}
        if path == '/system/info/up':
            return {'success': True}
        raise CameraError('%s %s: HTTP 404' % (method, path))

    def _phase(self):
        s = self.s
        if not s['update_started']:
            return None, 0
        t = time.time() - s['update_started']
        if s['breaks'] and t > 8:
            return 'failed', t
        if t < 3:
            return 'starting', t
        if t < 12:
            return 'downloading', t
        if t < 20:
            return 'installing', t
        if t < 28:
            return 'rebooting', t
        s['version'] = self.LATEST
        s['update_started'] = None
        return None, t


def client_for(cam):
    return (FakeCamera if DEMO else CameraClient)(cam['ip'], cam.get('password'))


def available_version(value, current):
    """Liest aus update_available eine Versionsnummer; None, wenn kein Update."""
    if isinstance(value, dict):
        value = value.get('update_available') or value.get('version')
    if not value or str(value).strip().lower() in ('false', 'none', 'no', '0', 'null'):
        return None
    m = re.search(r'\d+(?:\.\d+)+', str(value))
    version = m.group(0) if m else str(value).strip()
    return None if version == current else version


# ==========================================
# 3. ABLÄUFE: PRÜFEN & AKTUALISIEREN
# ==========================================

def set_cam(ip, **fields):
    with lock:
        if ip in cameras:
            cameras[ip].update(fields)


def check(ip):
    """Liest Version und fragt beim ARVOO-Server nach einem Update. Ändert nichts an der Kamera."""
    cam = cameras[ip]
    api = client_for(cam)
    set_cam(ip, status='checking', detail='Wird geprüft ...', progress=None)
    info = api.call('GET', '/system/info')
    if not isinstance(info, dict):
        raise CameraError('Unerwartete Antwort von /system/info')
    set_cam(ip, version=info.get('version'), serial=info.get('serial'), model=info.get('model'), online=True)
    answer = api.call('POST', '/system/actions/check_update')
    info = api.call('GET', '/system/info')
    current = info.get('version')
    avail = available_version(answer, current) or available_version(info.get('update_available'), current)
    set_cam(ip, version=current, update_available=avail, last_check=now(),
            status='available' if avail else 'current',
            detail='Update auf %s verfügbar' % avail if avail else 'Aktuell')
    return api, info, avail


def error_fields(e):
    offline = 'nicht erreichbar' in str(e)
    return {'status': 'offline' if offline else 'error', 'online': not offline, 'detail': str(e)}


check_slots = threading.BoundedSemaphore(10)


def run_check(ip):
    set_cam(ip, status='queued', detail='Wartet ...', progress=None)
    try:
        with check_slots:
            check(ip)
    except CameraError as e:
        set_cam(ip, last_check=now(), **error_fields(e))
    finally:
        set_cam(ip, busy=False)
        save_cameras()


def run_update(ip):
    cam = cameras[ip]
    entry = {'zeit': '', 'ip': ip, 'name': cam.get('name') or '', 'seriennummer': '',
             'version_vorher': '', 'version_nachher': '', 'ergebnis': '', 'meldung': ''}
    set_cam(ip, status='queued', detail='Wartet auf freien Platz ...', progress=None)
    with update_slots:
        try:
            api, info, avail = check(ip)
            before = info.get('version')
            entry.update(seriennummer=info.get('serial') or '', version_vorher=before or '')
            if not avail:
                entry.update(version_nachher=before or '', ergebnis='bereits aktuell')
                return

            set_cam(ip, status='updating', detail='Update auf %s wird gestartet ...' % avail, progress=0)
            fmt = config['perform_update_body']
            body = avail if fmt == 'text' else {'version': avail} if fmt == 'json' else None
            api.call('POST', '/system/actions/perform_update', body)

            version = wait_for_update(ip, api, before, avail)
            entry.update(version_nachher=version, ergebnis='erfolgreich')
            set_cam(ip, version=version, update_available=None, last_update=now(),
                    status='current', detail='Aktualisiert auf %s' % version, progress=None)
        except CameraError as e:
            entry.update(ergebnis='fehlgeschlagen', meldung=str(e))
            set_cam(ip, progress=None, **error_fields(e))
        finally:
            entry['zeit'] = now()
            write_log(entry)
            set_cam(ip, busy=False)
            save_cameras()


PHASES = {'starting': 'Startet', 'downloading': 'Lädt herunter', 'installing': 'Installiert', 'finished': 'Abgeschlossen'}


def wait_for_update(ip, api, before, target):
    """Wartet, bis die Kamera nach dem Update mit neuer Version zurück ist."""
    deadline = time.time() + config['update_timeout_min'] * 60
    while time.time() < deadline:
        time.sleep(1 if DEMO else 4)
        try:
            prog = api.call('GET', '/system/info/update_progress')
        except CameraError:
            set_cam(ip, detail='Kamera startet neu ...', progress=None)
            continue
        if isinstance(prog, dict):
            if prog.get('error'):
                raise CameraError('Update fehlgeschlagen (update_progress meldet Fehler)')
            phase = prog.get('phase')
            pct = prog.get('percent')
            set_cam(ip, detail='%s ...' % PHASES.get(phase, phase or 'Läuft'),
                    progress=pct if isinstance(pct, (int, float)) else None)
        # Erfolg erst, wenn die Kamera wirklich die neue Version meldet
        try:
            info = api.call('GET', '/system/info')
        except CameraError:
            continue
        version = info.get('version') if isinstance(info, dict) else None
        if version and version != before:
            return version
    raise CameraError('Zeitüberschreitung: Version nach %d min unverändert' % config['update_timeout_min'])


def start_jobs(ips, job):
    started = []
    with lock:
        for ip in ips:
            cam = cameras.get(ip)
            if cam and not cam['busy']:
                cam['busy'] = True
                started.append(ip)
    for ip in started:
        threading.Thread(target=job, args=(ip,), daemon=True).start()
    return started


# ==========================================
# 4. WEBSERVER
# ==========================================

IP_RE = re.compile(r'^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}(:\d{1,5})?$')


def normalize_address(text):
    """'http://10.32.75.8:566/' -> '10.32.75.8:566', '10.32.75.8' -> '10.32.75.8:577' (Standardport).
    Gleiche IP mit verschiedenen Ports sind verschiedene Kameras."""
    addr = re.sub(r'^[a-zA-Z]+://', '', text.strip()).split('/')[0]
    if addr and ':' not in addr:
        addr = '%s:%s' % (addr, config.get('port', 577))
    return addr


def parse_camera_lines(text):
    """Eine Kamera pro Zeile: 'IP' oder 'IP Name' oder 'IP; Name; Passwort'.
    Statt der IP geht auch der Link, z. B. 'http://10.32.75.8:566/'.
    Im Semikolon-Format wird das Passwort genau so übernommen (leer = Standardpasswort)."""
    result, errors = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ';' in line:
            parts = [p.strip() for p in line.split(';')]
        else:
            ip, _, name = line.partition(' ')
            parts = [ip, name.strip()]
        ip = normalize_address(parts[0])
        if not IP_RE.match(ip):
            errors.append(line)
            continue
        if any(r['ip'] == ip for r in result):
            continue
        result.append({'ip': ip, 'name': parts[1] if len(parts) > 1 else '',
                       'password': parts[2] if len(parts) > 2 and parts[2] else None,
                       'password_given': ';' in line})
    return result, errors


def public_state():
    log = read_log()
    with lock:
        cams = []
        for c in cameras.values():
            pub = {k: v for k, v in c.items() if k != 'password'}
            pub['own_password'] = bool(c.get('password'))
            cams.append(pub)
    settings = {k: v for k, v in config.items() if k != 'password'}
    settings['password_set'] = bool(config.get('password'))
    return {'demo': DEMO, 'cameras': cams, 'log': log[::-1], 'settings': settings}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(length) or b'{}')

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            with open(os.path.join(BASE_DIR, 'index.html'), 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/state':
            self.send_json(public_state())
        elif path == '/api/log.csv':
            body = open(log_file(), 'rb').read() if os.path.exists(log_file()) else ';'.join(LOG_FIELDS).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="kamera_update_protokoll.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({'error': 'nicht gefunden'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
        except ValueError:
            return self.send_json({'error': 'ungültiges JSON'}, 400)

        if path == '/api/cameras':
            entries, errors = parse_camera_lines(data.get('text', ''))
            added = []
            with lock:
                for e in entries:
                    if e['ip'] in cameras:
                        cameras[e['ip']]['name'] = e['name'] or cameras[e['ip']].get('name')
                        if e['password_given']:
                            cameras[e['ip']]['password'] = e['password']
                    else:
                        cameras[e['ip']] = new_camera(e)
                        added.append(e['ip'])
            save_cameras()
            return self.send_json({'added': len(added), 'added_ips': added,
                                   'updated': len(entries) - len(added), 'invalid': errors})

        if path == '/api/cameras/delete':
            with lock:
                for ip in data.get('ips', []):
                    if ip in cameras and not cameras[ip]['busy']:
                        del cameras[ip]
            save_cameras()
            return self.send_json({'ok': True})

        if path == '/api/check':
            return self.send_json({'started': start_jobs(data.get('ips', []), run_check)})

        if path == '/api/update':
            return self.send_json({'started': start_jobs(data.get('ips', []), run_update)})

        if path == '/api/settings':
            for key in ('user', 'api_prefix', 'auth', 'perform_update_body'):
                if key in data:
                    config[key] = str(data[key]).strip()
            for key in ('port', 'parallel', 'update_timeout_min'):
                if key in data:
                    config[key] = max(1, int(data[key]))
            if data.get('password'):
                config['password'] = data['password']
            save_config()
            return self.send_json({'ok': True, 'restart_needed': 'parallel' in data})

        self.send_json({'error': 'nicht gefunden'}, 404)


def main():
    global DEMO, DATA_DIR, config, update_slots
    parser = argparse.ArgumentParser(description='Kamera-Update für ARVOO-Kameras')
    parser.add_argument('--demo', action='store_true', help='simulierte Kameras zum Ausprobieren')
    parser.add_argument('--host', default='127.0.0.1', help='0.0.0.0 = im Netzwerk erreichbar')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    DEMO = args.demo
    DATA_DIR = os.path.join(BASE_DIR, 'data-demo' if DEMO else 'data')
    os.makedirs(DATA_DIR, exist_ok=True)
    config = load_config()
    update_slots = threading.BoundedSemaphore(int(config['parallel']))
    load_cameras()

    if DEMO and not cameras:
        demo_text = '\n'.join([
            '10.32.47.131 Parkhaus Nord', '10.32.47.132 Parkhaus Nord', '10.32.47.135 Einfahrt Süd',
            '10.32.47.137 Ausfahrt Süd', '10.32.47.140 Mitarbeiterparkplatz', '10.32.47.149 Lieferzone',
        ])
        for e in parse_camera_lines(demo_text)[0]:
            cameras[e['ip']] = new_camera(e)
        save_cameras()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host = 'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host
    print('Kamera-Update läuft%s: http://%s:%d  (Beenden mit Strg+C)' % (' im DEMO-Modus' if DEMO else '', host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nBeendet.')


if __name__ == '__main__':
    main()
