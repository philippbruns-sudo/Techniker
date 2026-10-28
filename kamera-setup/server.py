import argparse
import base64
import copy
import csv
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = '0.1.4'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
DATA_DIR = os.path.join(BASE_DIR, 'data')
LOG_FILE = os.path.join(DATA_DIR, 'setup_log.csv')

DEFAULT_CONFIG = {
    'factory_ip': '192.168.0.240',
    'port': 80,
    'api_prefix': '/api/v2',
    'user': 'user',
    'new_password': '',
    'interface': 'eth0',
    'backup_dir': 'backups',
    'backup_mode': 'all_settings',
    'internet': True,
    'perform_update_body': 'json',
    'allow_downgrade': False,
    'timeout_s': 10,
    'update_timeout_min': 30,
    'ip_change_timeout_s': 240,
    'profile': 'cam1',
    'profiles': [
        {'id': 'cam1', 'name': 'CAM 1', 'ip': '192.168.8.21', 'backup': 'backup.json'},
        {'id': 'cam2', 'name': 'CAM 2', 'ip': '192.168.8.22', 'backup': 'backup.json'},
    ],
}

STEPS = [
    ('detect', 'Kamera suchen ({factory_ip})'),
    ('login', 'MAC auslesen & anmelden'),
    ('firmware', 'Firmware-Update'),
    ('password', 'Passwort ändern'),
    ('name', 'Name „{name}“ setzen'),
    ('backup', 'Backup einspielen'),
    ('newip', 'Neue IP prüfen ({ip})'),
    ('verify', 'Abschlussprüfung'),
]

MAX_UPDATE_ROUNDS = 5

LOG_FIELDS = ['time', 'profile', 'name', 'mac', 'serial', 'ip', 'version_before', 'version_after',
              'result', 'message']

lock = threading.RLock()
config = {}


class StepError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Cancelled(Exception):
    pass


def give_back(path):
    uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
    if os.geteuid() != 0 or not uid or not os.path.exists(path):
        return
    for p in [path] + ([os.path.join(path, n) for n in os.listdir(path)] if os.path.isdir(path) else []):
        try:
            os.chown(p, int(uid), int(gid or uid))
        except OSError:
            pass


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg.update(json.load(f))
    return cfg


def save_config():
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    give_back(CONFIG_FILE)


def profile():
    for p in config['profiles']:
        if p['id'] == config.get('profile'):
            return p
    return config['profiles'][0]


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def host_of(ip):
    return ip if ':' in ip else '%s:%s' % (ip, config['port'])


def bare_ip(ip):
    return ip.split(':')[0]


class Camera:
    def __init__(self, ip, password, http_log):
        self.ip = ip
        self.password = password
        self.http_log = http_log

    def call(self, method, path, body=None, content_type=None, timeout=None, quiet=False):
        url = 'http://%s%s%s' % (host_of(self.ip), config['api_prefix'], path)
        headers = {'Authorization': 'Basic ' + base64.b64encode(('%s:%s' % (config['user'], self.password)).encode()).decode()}
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        elif body is not None:
            data = str(body).encode()
            headers['Content-Type'] = content_type or 'text/plain'
        entry = {'time': now(), 'method': method, 'url': url,
                 'sent': (data.decode('utf-8', 'replace')[:300] if data else ''), 'status': None, 'response': ''}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or config['timeout_s']) as res:
                raw = res.read()
                entry['status'] = res.status
        except urllib.error.HTTPError as e:
            raw = e.read(600)
            entry['status'] = e.code
            entry['response'] = clean(raw.decode('utf-8', 'replace'))[:600]
            self._log(entry, quiet)
            if e.code == 401:
                raise StepError('Anmeldung abgelehnt (HTTP 401) – Passwort falsch', 401)
            raise StepError('%s %s: HTTP %s – Kamera: %s' % (method, path, e.code, entry['response'][:200] or '(leer)'), e.code)
        except (urllib.error.URLError, OSError) as e:
            entry['status'] = 'Fehler'
            entry['response'] = str(getattr(e, 'reason', e))
            self._log(entry, quiet)
            raise StepError('%s nicht erreichbar (%s)' % (self.ip, entry['response']))
        text = raw.decode('utf-8', 'replace')
        entry['response'] = text[:600]
        self._log(entry, quiet)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def _log(self, entry, quiet):
        with lock:
            if quiet:
                same = next((e for e in reversed(self.http_log) if e['url'] == entry['url'] and e['method'] == entry['method']), None)
                if same and same['status'] == entry['status'] and same['response'] == entry['response']:
                    same['repeat'] = same.get('repeat', 1) + 1
                    same['last'] = entry['time']
                    return
            self.http_log.append(entry)
            del self.http_log[:-200]


def clean(text):
    if text.lstrip().startswith('<'):
        text = re.sub(r'<title>.*?</title>', '', text, flags=re.S)
        text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def port_open(ip, timeout=1.5):
    host, port = host_of(ip).split(':')
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def route_device(ip):
    try:
        out = subprocess.run(['ip', 'route', 'get', bare_ip(ip)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'\bdev (\S+)', out)
    return m.group(1) if m else None


def check_route(ip):
    if bare_ip(ip).startswith('127.'):
        return
    dev = route_device(ip)
    if dev != config['interface']:
        raise StepError('%s würde über „%s“ statt %s laufen – Netzwerk-Script ausgeführt und Kabel gesteckt?'
                        % (bare_ip(ip), dev or '?', config['interface']))


def flush_arp(ip):
    try:
        subprocess.run(['ip', 'neigh', 'flush', 'to', bare_ip(ip), 'dev', config['interface']],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def read_mac(ip):
    fake = os.environ.get('KAMERA_FAKE_MAC')
    if fake:
        return fake.lower()
    try:
        with open('/proc/net/arp') as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == bare_ip(ip) and parts[3] != '00:00:00:00:00:00':
                    return parts[3].lower()
    except OSError:
        pass
    return None


def version_key(value):
    nums = re.findall(r'\d+', str(value or ''))
    return tuple(int(n) for n in nums) if nums else None


def is_newer(target, current):
    t, c = version_key(target), version_key(current)
    return bool(t and c and t > c)


def version_of(value):
    if not value or str(value).strip().lower() in ('false', 'none', 'null', '0'):
        return None
    m = re.search(r'\d+(?:\.\d+)+', str(value))
    return m.group(0) if m else str(value).strip()


def load_backup():
    p = profile()
    path = os.path.join(BASE_DIR, config['backup_dir'], p['backup'])
    if not os.path.exists(path):
        raise StepError('Backup-Datei fehlt: %s/%s' % (config['backup_dir'], p['backup']))
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    lan = data.get('api/v2/network/settings', {}).get('lan')
    if lan is None:
        raise StepError('Backup enthält keine Netzwerk-Einstellungen (api/v2/network/settings.lan)')
    lan['address'] = bare_ip(p['ip'])
    return data


def compare(expected, actual, path=''):
    diffs = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path or '/']
        for k, v in expected.items():
            diffs += compare(v, actual.get(k), path + '/' + str(k))
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)) and not isinstance(expected, bool):
        if abs(expected - actual) > 1e-6:
            diffs.append(path)
    elif expected != actual:
        diffs.append(path)
    return diffs


class Job:
    def __init__(self):
        self.http_log = []
        self.reset()

    def reset(self):
        p = profile()
        self.state = 'waiting'
        self.ip = config['factory_ip']
        self.password = None
        self.camera = None
        self.error = None
        self.cancel_flag = False
        self.target_version = None
        self.steps = []
        for key, label in STEPS:
            self.steps.append({'key': key, 'label': label.format(factory_ip=config['factory_ip'], name=p['name'], ip=bare_ip(p['ip'])),
                               'status': 'pending', 'detail': '', 'progress': None})

    def step(self, key):
        return next(s for s in self.steps if s['key'] == key)

    def set(self, key, **fields):
        with lock:
            self.step(key).update(fields)

    def api(self):
        return Camera(self.ip, self.password, self.http_log)

    def check_cancel(self):
        if self.cancel_flag:
            raise Cancelled()

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.check_cancel()
            time.sleep(min(0.5, end - time.time()))

    def cam(self, **fields):
        with lock:
            self.camera = dict(self.camera or {}, **fields)

    def do_detect(self):
        ip = config['factory_ip']
        check_route(ip)
        self.set('detect', detail='Warte auf Antwort von %s ...' % ip)
        if not port_open(ip):
            raise StepError('Keine Kamera auf %s (Port %s) – Kabel/PoE prüfen, Kamera bootet ca. 1–2 min' % (ip, config['port']))
        self.ip = ip
        return 'Kamera antwortet'

    def do_login(self):
        flush_arp(self.ip)
        port_open(self.ip)
        mac = read_mac(self.ip)
        if not mac:
            raise StepError('MAC von %s nicht in der ARP-Tabelle gefunden' % self.ip)
        self.password = mac.replace(':', '')[-4:].upper()
        self.cam(mac=mac.upper())
        info = self.api().call('GET', '/system/info')
        if not isinstance(info, dict):
            raise StepError('Unerwartete Antwort von /system/info')
        serial = info.get('serial') or ''
        self.cam(serial=serial, model=info.get('model'), version_before=info.get('version'), ip=bare_ip(self.ip))
        note = '' if serial.upper() == mac.replace(':', '').upper() else ' (Seriennummer ≠ MAC)'
        return 'Angemeldet mit …%s · %s %s%s' % (self.password, info.get('model') or '', info.get('version') or '', note)

    def do_firmware(self):
        if not config['internet']:
            self.set('firmware', status='skipped')
            return 'Übersprungen (Internet in den Einstellungen aus)'
        api = self.api()
        first = None
        history = []
        for round_no in range(1, MAX_UPDATE_ROUNDS + 1):
            self.set('firmware', detail='Suche nach Updates%s ...' % (' (Runde %d)' % round_no if round_no > 1 else ''), progress=None)
            current, target = self.check_for_update(api)
            first = first or current
            downgrade_ok = config.get('allow_downgrade') and target and target != current and not history
            if not target or (not is_newer(target, current) and not downgrade_ok):
                self.cam(version_after=current)
                note = ' (angeboten: %s – nicht neuer, ignoriert)' % target if target and target != current else ''
                if not history:
                    self.set('firmware', status='skipped')
                    return 'Version %s ist aktuell%s' % (current, note)
                return 'Version %s → %s (%d Update%s), danach kein neueres Update%s' % (
                    first, current, len(history), '' if len(history) == 1 else 's', note)
            self.target_version = target
            prefix = 'Runde %d: ' % round_no if round_no > 1 else ''
            self.set('firmware', detail='%sUpdate %s → %s wird gestartet ...' % (prefix, current, target), progress=0)
            body = {'version': target} if config['perform_update_body'] == 'json' else target
            api.call('POST', '/system/actions/perform_update', body)
            new_version = self.wait_for_update(api, current, target, prefix)
            history.append(new_version)
            self.cam(version_after=new_version)
        raise StepError('Nach %d Updates gibt es immer noch ein neueres – bitte prüfen' % MAX_UPDATE_ROUNDS)

    def check_for_update(self, api):
        for attempt in range(6):
            try:
                answer = api.call('POST', '/system/actions/check_update', timeout=60)
                info = api.call('GET', '/system/info')
                current = info.get('version')
                target = version_of(answer.get('update_available') if isinstance(answer, dict) else answer) \
                    or version_of(info.get('update_available'))
                return current, target
            except StepError as e:
                if e.code and 400 <= e.code < 500:
                    raise
                self.set('firmware', detail='Kamera noch nicht bereit, neuer Versuch ...')
                self.sleep(10)
        raise StepError('Update-Prüfung nicht möglich – Kamera antwortet nicht')

    def wait_for_update(self, api, before, target, prefix):
        started = time.time()
        deadline = started + config['update_timeout_min'] * 60
        phases = {'starting': 'Startet', 'downloading': 'Lädt herunter', 'installing': 'Installiert', 'finished': 'Abgeschlossen'}
        candidate, seen = None, 0
        while time.time() < deadline:
            self.sleep(4)
            elapsed = '%d:%02d min' % divmod(int(time.time() - started), 60)
            try:
                prog = api.call('GET', '/system/info/update_progress', quiet=True)
                if isinstance(prog, dict):
                    if prog.get('error'):
                        raise StepError('Update auf %s fehlgeschlagen (update_progress meldet Fehler)' % target)
                    pct = prog.get('percent')
                    self.set('firmware', detail='%s%s %s ... (%s)' % (prefix, phases.get(prog.get('phase'), prog.get('phase') or 'Läuft'), target, elapsed),
                             progress=pct if isinstance(pct, (int, float)) else None)
                info = api.call('GET', '/system/info', quiet=True)
            except StepError as e:
                if e.code and 400 <= e.code < 500:
                    raise
                self.set('firmware', detail='%sKamera startet neu ... (%s)' % (prefix, elapsed), progress=None)
                candidate, seen = None, 0
                continue
            version = info.get('version') if isinstance(info, dict) else None
            if version and version_of(version) == target:
                self.set('firmware', progress=None)
                return version
            if version and version != before and not info.get('software_down'):
                seen = seen + 1 if version == candidate else 1
                candidate = version
                if seen >= 4:
                    self.set('firmware', progress=None)
                    return version
            else:
                candidate, seen = None, 0
        raise StepError('Zeitüberschreitung: nach %d min nicht auf %s (zuletzt: %s)' % (config['update_timeout_min'], target, candidate or before))

    def do_password(self):
        new = config.get('new_password')
        if not new:
            raise StepError('Kein neues Passwort in den Einstellungen hinterlegt')
        if self.password == new:
            return 'Passwort ist bereits gesetzt'
        old = self.password
        self.api().call('POST', '/system/actions/set_password', new, 'text/plain')
        self.password = new
        self.api().call('GET', '/system/info')
        try:
            Camera(self.ip, old, []).call('GET', '/system/info', quiet=True)
            raise StepError('Altes Passwort funktioniert noch – Änderung nicht wirksam')
        except StepError as e:
            if '401' not in str(e):
                raise
        return 'Neues Passwort aktiv, altes abgelehnt'

    def do_name(self):
        name = profile()['name']
        api = self.api()
        api.call('POST', '/system/settings/', {'name': name})
        result = api.call('GET', '/system/settings/')
        actual = result.get('name') if isinstance(result, dict) else None
        if actual != name:
            raise StepError('Name zurückgelesen: „%s“ statt „%s“' % (actual, name))
        self.cam(name=name)
        return 'Name zurückgelesen: %s' % name

    def do_backup(self):
        data = load_backup()
        target = profile()['ip']
        api = self.api()
        if config['backup_mode'] == 'sections':
            order = sorted(data.keys(), key=lambda k: 'network' in k)
            for key in order:
                path = '/' + key.replace(config['api_prefix'].strip('/') + '/', '', 1).lstrip('/')
                self.set('backup', detail='%s ...' % path)
                try:
                    api.call('POST', path, data[key], timeout=30)
                except StepError as e:
                    if 'network' in key and 'nicht erreichbar' in str(e):
                        break
                    raise
        else:
            self.set('backup', detail='all_settings wird übertragen ...')
            try:
                api.call('POST', '/system/actions/all_settings', data, timeout=60)
            except StepError as e:
                if 'nicht erreichbar' not in str(e):
                    raise
        self.ip = target
        return 'Übertragen (%s), Netzwerk wechselt auf %s' % (', '.join(k.split('/')[-2] for k in data), bare_ip(target))

    def do_newip(self):
        target = profile()['ip']
        self.ip = target
        check_route(target)
        deadline = time.time() + config['ip_change_timeout_s']
        while time.time() < deadline:
            self.set('newip', detail='Warte auf %s ...' % bare_ip(target))
            flush_arp(target)
            if port_open(target):
                break
            self.sleep(3)
        else:
            raise StepError('%s antwortet nicht (Timeout nach %d s)' % (bare_ip(target), config['ip_change_timeout_s']))
        api = self.api()
        backup = load_backup()
        diffs = []
        for key, expected in backup.items():
            path = '/' + key.replace(config['api_prefix'].strip('/') + '/', '', 1).lstrip('/')
            actual = api.call('GET', path)
            diffs += [key.split('/')[-2] + p for p in compare(expected, actual)]
        net = api.call('GET', '/network/settings')
        lan = net.get('lan', {}) if isinstance(net, dict) else {}
        if lan.get('address') != bare_ip(target):
            raise StepError('Kamera meldet IP %s statt %s' % (lan.get('address'), bare_ip(target)))
        self.cam(ip=bare_ip(target))
        if diffs:
            self.cam(backup_diffs=diffs[:20])
            return '%s antwortet · %d Abweichung(en): %s' % (bare_ip(target), len(diffs), ', '.join(diffs[:3]))
        return '%s antwortet, Einstellungen stimmen mit dem Backup überein' % bare_ip(target)

    def do_verify(self):
        api = self.api()
        info = api.call('GET', '/system/info')
        name = api.call('GET', '/system/settings/')
        problems = []
        if self.target_version and info.get('version') != self.target_version:
            problems.append('Version %s statt %s' % (info.get('version'), self.target_version))
        if isinstance(name, dict) and name.get('name') != profile()['name']:
            problems.append('Name „%s“' % name.get('name'))
        self.cam(version_after=info.get('version'))
        if problems:
            raise StepError('Abschlussprüfung: ' + ', '.join(problems))
        return 'Version %s · Name %s' % (info.get('version'), profile()['name'])

    def run_step(self, key):
        with lock:
            st = self.step(key)
            st.update(status='running', detail='', progress=None)
        try:
            detail = getattr(self, 'do_' + key)()
            with lock:
                if st['status'] == 'running':
                    st['status'] = 'done'
                st.update(detail=detail or '', progress=None)
            return True
        except Cancelled:
            self.set(key, status='error', detail='Abgebrochen', progress=None)
            self.error = {'step': key, 'message': 'Vom Benutzer abgebrochen'}
            return False
        except StepError as e:
            self.set(key, status='error', detail=str(e), progress=None)
            self.error = {'step': key, 'message': str(e)}
            return False
        except Exception as e:
            self.set(key, status='error', detail='Interner Fehler: %s' % e, progress=None)
            self.error = {'step': key, 'message': 'Interner Fehler: %s' % e}
            return False

    def run_from(self, index):
        self.state = 'running'
        self.error = None
        self.cancel_flag = False
        for st in self.steps[index:]:
            if not self.run_step(st['key']):
                self.state = 'error'
                write_log(self, 'fehlgeschlagen', self.error['message'])
                return
        self.state = 'done'
        write_log(self, 'erfolgreich', '')

    def public(self):
        with lock:
            return {'state': self.state, 'camera': self.camera, 'steps': copy.deepcopy(self.steps),
                    'error': self.error, 'ip': self.ip, 'password_known': bool(self.password)}


def write_log(job, result, message):
    os.makedirs(DATA_DIR, exist_ok=True)
    c = job.camera or {}
    row = {'time': now(), 'profile': profile()['id'], 'name': c.get('name') or profile()['name'],
           'mac': c.get('mac', ''), 'serial': c.get('serial', ''), 'ip': c.get('ip', ''),
           'version_before': c.get('version_before', ''), 'version_after': c.get('version_after', ''),
           'result': result, 'message': message}
    with lock:
        new = not os.path.exists(LOG_FILE)
        fields = LOG_FIELDS
        if not new:
            with open(LOG_FILE, newline='', encoding='utf-8-sig') as f:
                fields = next(csv.reader(f, delimiter=';'), LOG_FIELDS) or LOG_FIELDS
        with open(LOG_FILE, 'a', newline='', encoding='utf-8-sig' if new else 'utf-8') as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore', restval='')
            if new:
                w.writeheader()
            w.writerow(row)
        give_back(DATA_DIR)


def stats_today():
    ok = err = 0
    today = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f, delimiter=';'):
                if row.get('time', '').startswith(today):
                    if row.get('result') == 'erfolgreich':
                        ok += 1
                    else:
                        err += 1
    return {'ok': ok, 'error': err}


job = None
worker = None
auto_detect = True


def start_worker(index=0):
    global worker
    if worker and worker.is_alive():
        return False
    worker = threading.Thread(target=job.run_from, args=(index,), daemon=True)
    worker.start()
    return True


def detector():
    while True:
        time.sleep(2)
        if not auto_detect or job.state != 'waiting' or (worker and worker.is_alive()):
            continue
        try:
            if route_device(config['factory_ip']) == config['interface'] and port_open(config['factory_ip'], 1):
                start_worker(0)
        except Exception:
            pass


def public_status():
    s = job.public()
    s['stats'] = stats_today()
    s['config'] = {
        'default_ip': config['factory_ip'],
        'profile': profile()['id'],
        'profiles': [{'id': p['id'], 'name': p['name'], 'ip': bare_ip(p['ip'])} for p in config['profiles']],
    }
    s['version'] = VERSION
    return s


def test_state():
    s = public_status()
    s['http_log'] = list(reversed(job.http_log[-60:]))
    s['settings'] = {k: v for k, v in config.items() if k != 'new_password'}
    s['settings']['new_password_set'] = bool(config.get('new_password'))
    s['backups'] = sorted(os.listdir(os.path.join(BASE_DIR, config['backup_dir']))) \
        if os.path.isdir(os.path.join(BASE_DIR, config['backup_dir'])) else []
    s['auto_detect'] = auto_detect
    s['running'] = bool(worker and worker.is_alive())
    s['routes'] = {ip: route_device(ip) for ip in [config['factory_ip']] + [bare_ip(p['ip']) for p in config['profiles']]}
    return s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send(json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8', code)

    def send_file(self, name, ctype):
        with open(os.path.join(BASE_DIR, name), 'rb') as f:
            self.send(f.read(), ctype)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            return self.send_file('frontend/index.html', 'text/html; charset=utf-8')
        if path in ('/test', '/test.html'):
            return self.send_file('frontend/test.html', 'text/html; charset=utf-8')
        if path == '/api/status':
            return self.send_json(public_status())
        if path == '/api/test/state':
            return self.send_json(test_state())
        if path == '/api/log.csv':
            body = open(LOG_FILE, 'rb').read() if os.path.exists(LOG_FILE) else ';'.join(LOG_FIELDS).encode()
            return self.send(body, 'text/csv; charset=utf-8')
        self.send_json({'error': 'nicht gefunden'}, 404)

    def do_POST(self):
        global auto_detect, worker
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return self.send_json({'error': 'ungültiges JSON'}, 400)
        busy = bool(worker and worker.is_alive())

        if path == '/api/start':
            if job.state == 'waiting':
                start_worker(0)
            return self.send_json({'ok': True})
        if path == '/api/cancel':
            job.cancel_flag = True
            return self.send_json({'ok': True})
        if path == '/api/retry':
            if not busy and job.state == 'error':
                idx = next((i for i, s in enumerate(job.steps) if s['status'] == 'error'), 0)
                start_worker(idx)
            return self.send_json({'ok': True})
        if path == '/api/next':
            if not busy:
                job.reset()
            return self.send_json({'ok': True})
        if path == '/api/profile':
            if not busy and job.state == 'waiting' and any(p['id'] == data.get('id') for p in config['profiles']):
                config['profile'] = data['id']
                save_config()
                job.reset()
            return self.send_json({'ok': True})

        if path == '/api/test/run':
            auto_detect = False
            if busy:
                return self.send_json({'error': 'Es läuft bereits ein Schritt'}, 409)
            key = data.get('step')
            if key not in dict(STEPS):
                return self.send_json({'error': 'Unbekannter Schritt'}, 400)
            job.cancel_flag = False
            job.state = 'running'

            def run_one():
                job.run_step(key)
                job.state = 'error' if job.step(key)['status'] == 'error' else 'waiting'
            worker = threading.Thread(target=run_one, daemon=True)
            worker.start()
            return self.send_json({'ok': True})
        if path == '/api/test/context':
            if 'ip' in data and data['ip']:
                job.ip = data['ip'].strip()
            if 'password' in data and data['password']:
                job.password = data['password'].strip()
            return self.send_json({'ok': True})
        if path == '/api/test/auto':
            auto_detect = bool(data.get('on'))
            return self.send_json({'ok': True})
        if path == '/api/test/clear':
            with lock:
                job.http_log.clear()
            return self.send_json({'ok': True})
        if path == '/api/test/backup':
            name = os.path.basename(str(data.get('name') or 'backup.json'))
            if not name.lower().endswith('.json'):
                return self.send_json({'error': 'Bitte eine .json-Datei wählen'}, 400)
            try:
                content = json.loads(data.get('content') or '')
            except ValueError as e:
                return self.send_json({'error': 'Datei ist kein gültiges JSON: %s' % e}, 400)
            if not isinstance(content, dict) or 'lan' not in (content.get('api/v2/network/settings') or {}):
                return self.send_json({'error': 'Kein ARVOO-Backup: api/v2/network/settings.lan fehlt'}, 400)
            folder = os.path.join(BASE_DIR, config['backup_dir'])
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                json.dump(content, f, ensure_ascii=False)
            give_back(folder)
            return self.send_json({'ok': True, 'saved': name, 'sections': [k.split('/')[-2] for k in content]})
        if path == '/api/settings':
            for key in ('factory_ip', 'api_prefix', 'user', 'interface', 'backup_mode', 'perform_update_body'):
                if key in data:
                    config[key] = str(data[key]).strip()
            for key in ('port', 'update_timeout_min', 'ip_change_timeout_s'):
                if key in data:
                    config[key] = max(1, int(data[key]))
            if 'internet' in data:
                config['internet'] = bool(data['internet'])
            if 'allow_downgrade' in data:
                config['allow_downgrade'] = bool(data['allow_downgrade'])
            if data.get('new_password'):
                config['new_password'] = data['new_password']
            if data.get('profiles'):
                config['profiles'] = data['profiles']
            save_config()
            if not busy and job.state == 'waiting':
                job.reset()
            return self.send_json({'ok': True})
        self.send_json({'error': 'nicht gefunden'}, 404)


def main():
    global config, job, auto_detect
    parser = argparse.ArgumentParser(description='Kamera-Setup-Station für ARVOO Ruby')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--manual', action='store_true', help='keine automatische Erkennung (Testbetrieb)')
    args = parser.parse_args()
    config = load_config()
    if not os.path.exists(CONFIG_FILE):
        save_config()
    os.makedirs(os.path.join(BASE_DIR, config['backup_dir']), exist_ok=True)
    give_back(os.path.join(BASE_DIR, config['backup_dir']))
    job = Job()
    auto_detect = not args.manual
    threading.Thread(target=detector, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print('Kamera-Setup %s läuft: http://<pi-ip>:%d  ·  Testmodus: http://<pi-ip>:%d/test  (Strg+C beendet)'
          % (VERSION, args.port, args.port))
    if os.geteuid() != 0:
        print('Hinweis: ohne sudo kann der ARP-Cache nicht geleert werden.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nBeendet.')


if __name__ == '__main__':
    main()
