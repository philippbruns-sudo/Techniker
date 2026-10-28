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
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = '0.4.8'
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
    'factory_gateway': '192.168.0.1',
    'autostart_delay_s': 5,
    'interface': 'eth0',
    'backup_dir': 'backups',
    'backup_mode': 'all_settings',
    'internet': True,
    'perform_update_body': 'json',
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
    ('detect', 'Kamera suchen'),
    ('login', 'MAC auslesen & anmelden'),
    ('firmware', 'Firmware-Update'),
    ('password', 'Passwort ändern'),
    ('name', 'Name „{name}“ setzen'),
    ('backup', 'Backup einspielen'),
    ('newip', 'Neue IP prüfen ({ip})'),
    ('verify', 'Abschlussprüfung'),
]

UPDATE_ROUNDS = 1

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
    def __init__(self, ip, password, http_log, job=None):
        self.ip = ip
        self.password = password
        self.http_log = http_log
        self.job = job

    def call(self, *args, **kwargs):
        try:
            return self.request(*args, **kwargs)
        except StepError as e:
            if e.code != 401 or not self.job:
                raise
            for pw in self.job.password_candidates():
                if pw == self.password:
                    continue
                self.password = pw
                try:
                    result = self.request(*args, **kwargs)
                except StepError as retry:
                    if retry.code == 401:
                        continue
                    self.job.password = pw
                    raise
                self.job.password = pw
                return result
            raise

    def request(self, method, path, body=None, content_type=None, timeout=None, quiet=False):
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



def run_ip(*args):
    try:
        return subprocess.run(['ip'] + list(args), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def protected_ips():
    ips = set()
    for args in (('-4', 'route', 'show', 'default'), ('-4', '-o', 'addr', 'show')):
        res = run_ip(*args)
        if res:
            ips.update(re.findall(r'(?:via|inet) (\d+\.\d+\.\d+\.\d+)', res.stdout))
    return ips


def reach(ip):
    target = bare_ip(ip)
    if target.startswith('127.') or route_device(target) == config['interface']:
        return True
    if target in protected_ips():
        return False
    parts = target.split('.')
    src = '.'.join(parts[:3] + ['251' if parts[3] == '250' else '250'])
    res = run_ip('addr', 'add', src + '/32', 'dev', config['interface'])
    added_addr = bool(res and res.returncode == 0)
    run_ip('route', 'replace', target + '/32', 'dev', config['interface'], 'src', src)
    with lock:
        temp_routes.append((target, src if added_addr else None))
    return route_device(target) == config['interface']


def drop_temp_routes():
    with lock:
        routes = list(temp_routes)
        del temp_routes[:]
    for target, src in routes:
        run_ip('route', 'del', target + '/32', 'dev', config['interface'])
        if src:
            run_ip('addr', 'del', src + '/32', 'dev', config['interface'])


def internet_problem():
    if bare_ip(config['factory_ip']).startswith('127.') or os.geteuid() != 0:
        return None
    res = run_ip('-4', 'addr', 'show', 'dev', config['interface'])
    if not res or (' %s/' % config['factory_gateway']) not in res.stdout:
        return '%s hat %s nicht' % (config['interface'], config['factory_gateway'])
    try:
        with open('/proc/sys/net/ipv4/ip_forward') as f:
            if f.read().strip() != '1':
                return 'Weiterleitung aus'
    except OSError:
        pass
    try:
        if subprocess.run(['nft', 'list', 'table', 'inet', 'kamera_setup'], capture_output=True, timeout=5).returncode != 0:
            return 'NAT-Regeln nicht geladen'
    except (OSError, subprocess.SubprocessError):
        return 'nftables fehlt'
    try:
        socket.create_connection(('1.1.1.1', 443), timeout=4).close()
    except OSError:
        return 'Pi selbst hat kein Internet'
    try:
        socket.create_connection((config['factory_gateway'], 53), timeout=3).close()
    except OSError:
        return 'DNS (dnsmasq) läuft nicht'
    return None


class Sniffer:
    def __init__(self):
        self.seen = {}

    def start(self, iface):
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            sock.bind((iface, 0))
        except (OSError, AttributeError) as e:
            print('Hinweis: Kameras mit unbekannter IP werden nicht erkannt (%s) – mit sudo starten.' % e)
            return
        threading.Thread(target=self.loop, args=(sock,), daemon=True).start()

    def loop(self, sock):
        while True:
            try:
                frame, addr = sock.recvfrom(2048)
            except OSError:
                time.sleep(1)
                continue
            if addr[2] == socket.PACKET_OUTGOING or len(frame) < 34:
                continue
            etype = frame[12:14]
            if etype == b'\x08\x06' and len(frame) >= 42:
                raw = frame[28:32] if frame[28:32] != b'\0\0\0\0' else frame[38:42]
            elif etype == b'\x08\x00':
                raw = frame[26:30]
            else:
                continue
            if raw[0] in (0, 127) or raw[0] >= 224:
                continue
            ip = '.'.join(str(b) for b in raw)
            with lock:
                self.seen[ip] = time.time()

    def recent(self, seconds=120):
        with lock:
            return [ip for ip, t in sorted(self.seen.items(), key=lambda x: -x[1]) if time.time() - t < seconds]


def link_up():
    try:
        with open('/sys/class/net/%s/carrier' % config['interface']) as f:
            return f.read().strip() == '1'
    except OSError:
        return True


def find_camera():
    if not link_up():
        return None
    fixed = [config['factory_ip']] + [p['ip'] for p in config['profiles']]
    for ip in fixed:
        if (bare_ip(ip).startswith('127.') or route_device(ip) == config['interface']) and port_open(ip, 1):
            return ip, (read_mac(ip) or '').lower()
    for ip in sniffer.recent():
        if ip in [bare_ip(i) for i in fixed] or not reach(ip) or not port_open(ip, 1):
            continue
        return ip, (read_mac(ip) or '').lower()
    return None


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
            if k in actual:
                diffs += compare(v, actual[k], path + '/' + str(k))
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
        self.found = None
        self.password = None
        self.camera = None
        self.error = None
        self.cancel_flag = False
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
        return Camera(self.ip, self.password, self.http_log, self)

    def password_candidates(self):
        mac = (self.camera or {}).get('mac') or ''
        return [pw for pw in (mac.replace(':', '')[-4:].upper(), config.get('new_password')) if pw]

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
        auto = bool(self.found)
        self.set('detect', detail='Suche Kamera ...')
        found = self.found or find_camera()
        self.found = None
        if not found:
            raise StepError('Keine Kamera gefunden (%s und andere IPs) – Kabel/PoE prüfen, Kamera bootet ca. 1–2 min'
                            % bare_ip(config['factory_ip']))
        self.ip = found[0]
        self.set('detect', label='Kamera gefunden (%s)' % bare_ip(self.ip))
        if found[1]:
            self.cam(mac=found[1].upper())
        for left in range(int(config['autostart_delay_s']) if auto else 0, 0, -1):
            self.set('detect', detail='Einrichtung startet in %d s ...' % left)
            self.sleep(1)
        if bare_ip(self.ip) == bare_ip(config['factory_ip']):
            return 'Kamera im Werkszustand'
        return 'Kamera auf %s – wird ins Werksnetz geholt' % bare_ip(self.ip)

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
        how = 'neuem Passwort (schon gesetzt)' if self.password == config.get('new_password') else '…%s' % self.password
        moved = self.move_to_factory_net()
        return 'Angemeldet mit %s · %s %s%s%s' % (how, info.get('model') or '', info.get('version') or '', note, moved)

    def move_to_factory_net(self):
        factory = config['factory_ip']
        old = bare_ip(self.ip)
        if old == bare_ip(factory):
            return ''
        api = self.api()
        net = api.call('GET', '/network/settings')
        lan = net.get('lan') if isinstance(net, dict) else None
        if not isinstance(lan, dict):
            raise StepError('Netzwerk-Einstellungen der Kamera nicht lesbar')
        lan.update(address=bare_ip(factory), netmask='255.255.255.0', gateway=config['factory_gateway'],
                   dns_nameservers=[config['factory_gateway']], dhcp_enabled=False, static_ip_enabled=True)
        self.set('login', detail='Stelle %s auf Werks-IP %s um ...' % (old, bare_ip(factory)))
        try:
            api.call('POST', '/network/settings', net, timeout=30)
        except StepError as e:
            if 'nicht erreichbar' not in str(e):
                raise
        self.ip = factory
        deadline = time.time() + config['ip_change_timeout_s']
        while time.time() < deadline:
            self.set('login', detail='Warte auf Kamera unter %s ...' % bare_ip(factory))
            flush_arp(factory)
            if port_open(factory):
                return ' · von %s ins Werksnetz geholt' % old
            self.sleep(3)
        raise StepError('Kamera nach Umstellung nicht unter %s erreichbar (vorher %s)' % (bare_ip(factory), old))

    def do_firmware(self):
        if not config['internet']:
            self.set('firmware', status='skipped')
            return 'Übersprungen (Internet in den Einstellungen aus)'
        problem = internet_problem()
        if problem:
            raise StepError('Kein Internet für die Kamera: %s – sudo bash ~/Techniker/kamera-setup/pi/kamera-internet.sh --status' % problem)
        api = self.api()
        results = []
        for round_no in range(1, UPDATE_ROUNDS + 1):
            prefix = 'Runde %d: ' % round_no if UPDATE_ROUNDS > 1 else ''
            self.set('firmware', detail=prefix + 'Suche nach Update ...', progress=None)
            offered = self.check_for_update(api)
            if not offered:
                results.append(prefix + 'kein Update' + self.update_note)
                break
            self.set('firmware', detail='%sUpdate %s wird installiert ...' % (prefix, offered), progress=0)
            body = {'version': offered} if config['perform_update_body'] == 'json' else offered
            before = (self.wait_reachable(api) or {}).get('version')
            try:
                api.call('POST', '/system/actions/perform_update', body, timeout=60)
            except StepError as e:
                if e.code:
                    raise
            if not self.wait_update_done(api, prefix + offered, before):
                results.append('%s%s von Kamera abgelehnt (Fehler: %s)' % (prefix, offered, getattr(self, 'update_error', '?')))
                break
            results.append('%s%s installiert' % (prefix, offered))
        info = self.wait_reachable(api)
        self.cam(version_after=info.get('version'))
        if all('kein Update' in r for r in results):
            self.set('firmware', status='skipped')
        return ' · '.join(results) + ' · jetzt %s' % info.get('version')

    def wait_reachable(self, api, step='firmware', path='/system/info', minutes=5):
        deadline = time.time() + minutes * 60
        while True:
            try:
                return api.call('GET', path, quiet=True)
            except StepError as e:
                if (e.code and e.code < 500) or time.time() > deadline:
                    raise
                self.set(step, detail='Warte auf Kamera (%s) ...' % bare_ip(self.ip), progress=None)
                self.sleep(5)

    def wait_stable(self, step, ip, timeout_s):
        deadline = time.time() + timeout_s
        ok = 0
        while time.time() < deadline:
            if not ok:
                flush_arp(ip)
            ok = ok + 1 if port_open(ip) else 0
            self.set(step, detail='Warte auf %s ...%s' % (bare_ip(ip), ' (antwortet, prüfe ob stabil)' if ok else ''))
            if ok >= 4:
                return
            self.sleep(3)
        raise StepError('%s antwortet nicht stabil (Timeout nach %d s)' % (bare_ip(ip), timeout_s))

    def check_for_update(self, api):
        self.update_note = ''
        no_update = 0
        for attempt in range(8):
            try:
                answer = api.call('POST', '/system/actions/check_update', timeout=60)
            except StepError as e:
                if e.code == 401:
                    raise
                if e.code and 400 <= e.code < 500:
                    self.update_note = ' (Kamera meldet HTTP %s)' % e.code
                    return None
                self.set('firmware', detail='Kamera noch nicht bereit, neuer Versuch ...')
                self.sleep(10)
                continue
            offered = answer.get('update_available') if isinstance(answer, dict) else answer
            offered = str(offered or '').strip()
            if re.match(r'^v?\d+(\.\d+)+\S*$', offered):
                return offered
            no_update += 1
            self.update_note = ' (Antwort: %s)' % clean(json.dumps(answer, ensure_ascii=False))[:80]
            if no_update >= 3:
                return None
            self.set('firmware', detail='Kamera meldet kein Update – frage in 20 s erneut (%d/3) ...' % no_update)
            self.sleep(20)
        self.update_note = ' (Update-Prüfung nicht möglich)'
        return None

    def wait_update_done(self, api, label, before=None):
        started = time.time()
        deadline = started + config['update_timeout_min'] * 60
        phases = {'starting': 'Startet', 'downloading': 'Lädt herunter', 'installing': 'Installiert', 'finished': 'Abgeschlossen'}
        active, ready, rejected = False, 0, 0
        while time.time() < deadline:
            self.sleep(4)
            elapsed = '%d:%02d min' % divmod(int(time.time() - started), 60)
            try:
                prog = api.call('GET', '/system/info/update_progress', quiet=True)
                info = api.call('GET', '/system/info', quiet=True)
            except StepError as e:
                if e.code and 400 <= e.code < 500:
                    raise
                active, ready = True, 0
                self.set('firmware', detail='%s – Kamera startet neu ... (%s)' % (label, elapsed), progress=None)
                continue
            phase = prog.get('phase') if isinstance(prog, dict) else None
            version = info.get('version') if isinstance(info, dict) else None
            if before and version and version != before and not info.get('software_down'):
                ready += 1
                self.set('firmware', detail='%s – jetzt %s, prüfe ob stabil ... (%s)' % (label, version, elapsed), progress=None)
                if ready >= 3:
                    return True
                continue
            if phase in ('starting', 'downloading', 'installing'):
                active, ready, rejected = True, 0, 0
            elif isinstance(prog, dict) and prog.get('error'):
                rejected += 1
                self.update_error = prog.get('error')
                self.set('firmware', detail='%s – Kamera meldet Fehler, prüfe ... (%s)' % (label, elapsed), progress=None)
                if rejected >= 3:
                    self.set('firmware', progress=None)
                    return False
                continue
            pct = prog.get('percent') if isinstance(prog, dict) else None
            self.set('firmware', detail='%s – %s ... (%s)' % (label, phases.get(phase, phase or 'Läuft'), elapsed),
                     progress=pct if isinstance(pct, (int, float)) else None)
            settled = phase == 'finished' and not (isinstance(info, dict) and info.get('software_down'))
            ready = ready + 1 if settled and (active or time.time() - started > 60) else 0
            if ready >= 3:
                self.set('firmware', progress=None)
                return True
        raise StepError('Zeitüberschreitung: Update %s nach %d min nicht abgeschlossen' % (label, config['update_timeout_min']))

    def do_password(self):
        new = config.get('new_password')
        if not new:
            raise StepError('Kein neues Passwort hinterlegt – Testseite › Einstellungen › „Neues Kamera-Passwort“ eintragen und Speichern')
        if self.password == new:
            return 'Passwort ist bereits gesetzt'
        old = self.password
        self.api().call('POST', '/system/actions/set_password', new, 'text/plain')
        self.password = new
        self.sleep(3)
        self.wait_ready('password')
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
        mode = 'Abschnitte einzeln'
        if config['backup_mode'] != 'sections':
            self.set('backup', detail='all_settings wird übertragen ...')
            try:
                api.call('POST', '/system/actions/all_settings', data, timeout=60)
                mode = 'all_settings'
            except StepError as e:
                if 'nicht erreichbar' in str(e):
                    mode = 'all_settings'
                elif not e.code or e.code == 401:
                    raise
                else:
                    self.set('backup', detail='all_settings abgelehnt (HTTP %s) – Abschnitte einzeln ...' % e.code)
                    mode = 'Abschnitte einzeln, all_settings: HTTP %s' % e.code
        if mode.startswith('Abschnitte'):
            self.post_sections(api, data)
        self.ip = target
        return 'Übertragen (%s; %s), Netzwerk wechselt auf %s' % (', '.join(k.split('/')[-2] for k in data), mode, bare_ip(target))

    def post_sections(self, api, data):
        for key in sorted(data.keys(), key=lambda k: 'network' in k):
            path = '/' + key.replace(config['api_prefix'].strip('/') + '/', '', 1).lstrip('/')
            self.set('backup', detail='%s ...' % path)
            try:
                api.call('POST', path, data[key], timeout=30)
            except StepError as e:
                if 'network' in key and 'nicht erreichbar' in str(e):
                    break
                raise

    def do_newip(self):
        target = profile()['ip']
        self.ip = target
        check_route(target)
        self.wait_stable('newip', target, config['ip_change_timeout_s'])
        api = self.api()
        backup = load_backup()
        tz_note = self.fix_timezone(api, backup)
        diffs = []
        for key, expected in backup.items():
            path = '/' + key.replace(config['api_prefix'].strip('/') + '/', '', 1).lstrip('/')
            actual = self.wait_reachable(api, 'newip', path)
            diffs += [key.split('/')[-2] + p for p in compare(expected, actual)]
        net = self.wait_reachable(api, 'newip', '/network/settings')
        lan = net.get('lan', {}) if isinstance(net, dict) else {}
        if lan.get('address') != bare_ip(target):
            raise StepError('Kamera meldet IP %s statt %s' % (lan.get('address'), bare_ip(target)))
        self.cam(ip=bare_ip(target))
        if diffs:
            self.cam(backup_diffs=diffs[:20])
            return '%s antwortet · %d Abweichung(en): %s%s' % (bare_ip(target), len(diffs), ', '.join(diffs[:6]), tz_note)
        return '%s antwortet, Einstellungen stimmen mit dem Backup überein%s' % (bare_ip(target), tz_note)

    def fix_timezone(self, api, backup):
        wanted = (backup.get('api/v2/base/settings') or {}).get('timezone')
        if not wanted:
            return ''
        current = (self.wait_reachable(api, 'newip', '/base/settings') or {}).get('timezone')
        name = wanted.split(' (')[0]
        if current and current.split(' (')[0] == name:
            if current != wanted:
                backup['api/v2/base/settings']['timezone'] = current
            return ''
        variants = [name]
        try:
            offset = datetime.now(ZoneInfo(name)).utcoffset()
            minutes = int(offset.total_seconds() // 60)
            variants.insert(0, '%s (UTC%s%d:%02d)' % (name, '-' if minutes < 0 else '+', abs(minutes) // 60, abs(minutes) % 60))
        except Exception:
            pass
        for value in variants:
            try:
                api.call('POST', '/base/settings', {'timezone': value})
                current = api.call('GET', '/base/settings').get('timezone')
            except StepError:
                continue
            if current and current.split(' (')[0] == name:
                backup['api/v2/base/settings']['timezone'] = current
                return ' · Zeitzone %s gesetzt' % current
        return ' · Zeitzone %s nicht übernommen' % name

    def do_verify(self):
        api = self.api()
        self.wait_stable('verify', self.ip, config['ip_change_timeout_s'])
        info = self.wait_reachable(api, 'verify', '/system/info')
        name = self.wait_reachable(api, 'verify', '/system/settings/')
        problems = []
        if isinstance(name, dict) and name.get('name') != profile()['name']:
            problems.append('Name „%s“' % name.get('name'))
        self.cam(version_after=info.get('version'))
        if problems:
            raise StepError('Abschlussprüfung: ' + ', '.join(problems))
        return 'Version %s · Name %s' % (info.get('version'), profile()['name'])

    def wait_ready(self, key, timeout_s=None):
        deadline = time.time() + (timeout_s or config['ip_change_timeout_s'])
        ok = 0
        while time.time() < deadline:
            alive = port_open(self.ip, 2)
            if alive and self.password:
                try:
                    self.api().call('GET', '/system/info', quiet=True, timeout=5)
                except StepError as e:
                    alive = e.code is not None and e.code < 500
            ok = ok + 1 if alive else 0
            if ok >= 3:
                return
            if not alive:
                flush_arp(self.ip)
            self.set(key, detail='Warte bis Kamera (%s) bereit ist ...' % bare_ip(self.ip))
            self.sleep(2)
        raise StepError('Kamera %s antwortet nicht (Timeout nach %d s)' % (bare_ip(self.ip), timeout_s or config['ip_change_timeout_s']))

    def run_step(self, key):
        with lock:
            st = self.step(key)
            st.update(status='running', detail='', progress=None)
        try:
            for attempt in range(3):
                try:
                    if key not in ('detect', 'newip'):
                        self.wait_ready(key)
                    detail = getattr(self, 'do_' + key)()
                    break
                except StepError as e:
                    if attempt == 2 or (e.code is not None and e.code < 500):
                        raise
                    self.set(key, status='running', detail='Verbindung unterbrochen – neuer Versuch (%d/2) ...' % (attempt + 1))
                    self.sleep(3)
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
gui_port = 0
gui_last = None
worker = None
auto_detect = True
ignore_mac = None
temp_routes = []
sniffer = Sniffer()


def start_worker(index=0):
    global worker
    if worker and worker.is_alive():
        return False
    worker = threading.Thread(target=job.run_from, args=(index,), daemon=True)
    worker.start()
    return True


def detector():
    global ignore_mac
    while True:
        time.sleep(2)
        if not auto_detect or job.state != 'waiting' or (worker and worker.is_alive()):
            continue
        try:
            found = find_camera()
            if not found:
                ignore_mac = None
                drop_temp_routes()
                continue
            if ignore_mac and found[1] == ignore_mac:
                continue
            if job.state == 'waiting' and not (worker and worker.is_alive()):
                job.found = found
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
    s['gui_port'] = gui_port
    s['camera_ip'] = bare_ip(gui_last or job.ip or config['factory_ip'])
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
        global auto_detect, worker, ignore_mac
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
                ignore_mac = ((job.camera or {}).get('mac') or '').lower() or None
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
            if data.get('new_password'):
                config['new_password'] = str(data['new_password']).strip()
                print('Neues Kamera-Passwort gespeichert (%d Zeichen)' % len(config['new_password']), flush=True)
            if data.get('profiles'):
                config['profiles'] = data['profiles']
            save_config()
            if not busy and job.state == 'waiting':
                job.reset()
            return self.send_json({'ok': True})
        self.send_json({'error': 'nicht gefunden'}, 404)


def pipe(src, dst):
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def gui_targets():
    ips = [gui_last, job.ip if job else None, config['factory_ip']] + [p['ip'] for p in config['profiles']]
    seen = []
    for ip in ips:
        if ip and host_of(ip) not in seen:
            seen.append(host_of(ip))
    return seen


def gui_forward(client):
    global gui_last
    camera = None
    for target in gui_targets():
        host, port = target.rsplit(':', 1)
        try:
            camera = socket.create_connection((host, int(port)), timeout=2)
            gui_last = target
            break
        except OSError:
            continue
    if not camera:
        client.close()
        return
    camera.settimeout(None)
    threading.Thread(target=pipe, args=(client, camera), daemon=True).start()
    pipe(camera, client)
    client.close()
    camera.close()


def gui_proxy(host, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(32)
    while True:
        client, _ = listener.accept()
        threading.Thread(target=gui_forward, args=(client,), daemon=True).start()


def main():
    global config, job, auto_detect, gui_port
    parser = argparse.ArgumentParser(description='Kamera-Setup-Station für ARVOO Ruby')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--gui-port', type=int, default=8081, help='Weiterleitung zur Kamera-Oberfläche (0 = aus)')
    parser.add_argument('--manual', action='store_true', help='keine automatische Erkennung (Testbetrieb)')
    args = parser.parse_args()
    config = load_config()
    if not os.path.exists(CONFIG_FILE):
        save_config()
    os.makedirs(os.path.join(BASE_DIR, config['backup_dir']), exist_ok=True)
    give_back(os.path.join(BASE_DIR, config['backup_dir']))
    job = Job()
    auto_detect = not args.manual
    sniffer.start(config['interface'])
    threading.Thread(target=detector, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print('Kamera-Setup %s läuft: http://<pi-ip>:%d  ·  Testmodus: http://<pi-ip>:%d/test  (Strg+C beendet)'
          % (VERSION, args.port, args.port))
    gui_port = args.gui_port
    if args.gui_port:
        threading.Thread(target=gui_proxy, args=(args.host, args.gui_port), daemon=True).start()
        print('Kamera-Oberfläche: http://<pi-ip>:%d (aktuelle Kamera)' % args.gui_port)
    if os.geteuid() != 0:
        print('Hinweis: ohne sudo kann der ARP-Cache nicht geleert werden.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nBeendet.')


if __name__ == '__main__':
    main()
