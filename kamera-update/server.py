import argparse
import base64
import csv
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

DEFAULT_CONFIG = {
    'user': 'user',
    'password': '',
    'port': 577,
    'api_prefix': '/api/v2',
    'auth': 'basic',
    'perform_update_body': 'json',
    'parallel': 3,
    'timeout_s': 10,
    'update_timeout_min': 30,
}

LOG_FIELDS = ['time', 'ip', 'name', 'serial', 'version_before', 'version_after', 'result', 'message']
RESULT_SUCCESS = 'success'
RESULT_CURRENT = 'up to date'
RESULT_FAILED = 'failed'

LEGACY_LOG_FIELDS = {
    'zeit': 'time', 'seriennummer': 'serial', 'version_vorher': 'version_before',
    'version_nachher': 'version_after', 'ergebnis': 'result', 'meldung': 'message',
}
LEGACY_RESULTS = {'erfolgreich': RESULT_SUCCESS, 'bereits aktuell': RESULT_CURRENT, 'fehlgeschlagen': RESULT_FAILED}

MESSAGES = {
    'checking': 'Checking ...',
    'waiting': 'Waiting ...',
    'waiting_slot': 'Waiting for a free slot ...',
    'available': 'Update to {0} available',
    'current': 'Up to date',
    'starting_update': 'Starting update to {0} ...',
    'detecting_format': 'Detecting update format: {0} ...',
    'phase': '{0} ...',
    'rebooting': 'Camera is restarting ...',
    'updated': 'Updated to {0}',
    'auth_failed': 'Login rejected (HTTP 401) - check password',
    'unreachable': 'not reachable ({0})',
    'http_error': '{0} {1}: HTTP {2}{3}{4}',
    'bad_info': 'Unexpected response from /system/info',
    'success_false': 'perform_update: camera reports success = false ({0})',
    'all_formats_rejected': 'Camera rejects all formats for perform_update ({0})',
    'update_error': 'Update failed (update_progress reports an error)',
    'timeout': 'Timeout: version unchanged after {0} min',
    'rechecking': 'Checking for further updates ...',
    'too_many_rounds': 'Still a newer version after {0} updates - please check',
    'not_applied': 'Camera reports "update finished" but is still on {0} - update to {1} was not applied',
    'not_starting': 'Update to {0} does not start - camera stays on {1}',
}

UPDATE_FORMATS = ['json', 'json_string', 'form', 'query', 'json_raw', 'text', 'raw', 'none']
UPDATE_ROUNDS = 2
PHASES = ('starting', 'downloading', 'installing', 'finished')

DEMO = False
DATA_DIR = os.path.join(BASE_DIR, 'data')
lock = threading.RLock()
cameras = {}
config = {}
update_slots = None
check_slots = threading.BoundedSemaphore(10)


def render(key, args=()):
    return MESSAGES.get(key, key).format(*args)


def detail(key, *args):
    return {'key': key, 'args': [str(a) for a in args]}


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
    c.update({'status': 'idle', 'detail': None, 'progress': None, 'busy': False, 'online': None, 'update_raw': None})
    return c


def migrate_log():
    path = log_file()
    if not os.path.exists(path):
        return
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=';')
        rows = list(reader)
        fields = reader.fieldnames or []
    if 'zeit' not in fields:
        return
    shutil.copyfile(path, path.replace('.csv', '_backup_de.csv'))
    converted = []
    for row in rows:
        new = {LEGACY_LOG_FIELDS.get(k, k): v for k, v in row.items()}
        new['result'] = LEGACY_RESULTS.get(new.get('result'), new.get('result'))
        converted.append(new)
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(converted)


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


class CameraError(Exception):
    def __init__(self, key, *args, code=None):
        self.key = key
        self.args_ = [str(a) for a in args]
        self.code = code
        super().__init__(render(key, self.args_))


class CameraClient:
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

    def call(self, method, path, body=None, content_type=None):
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
            headers['Content-Type'] = content_type or 'text/plain'
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                raw = res.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise CameraError('auth_failed', code=401)
            try:
                reason = e.read(400).decode('utf-8', 'replace').strip()
            except Exception:
                reason = ''
            if reason.lstrip().startswith('<'):
                reason = re.sub(r'<title>.*?</title>', '', reason, flags=re.S)
                reason = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', reason)).strip()
            sent = ' | sent: %s' % (data.decode('utf-8', 'replace')[:80] if data else '(nothing)') if method == 'POST' else ''
            raise CameraError('http_error', method, path, e.code, ' - camera: ' + reason if reason else '', sent, code=e.code)
        except (urllib.error.URLError, OSError) as e:
            raise CameraError('unreachable', getattr(e, 'reason', e))
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode('utf-8', 'replace')


class FakeCamera:
    LATEST = '4.12.0'
    state = {}

    def __init__(self, ip, password=None):
        self.ip = ip
        if ip not in FakeCamera.state:
            last = int(re.sub(r'\D', '', ip.split(':')[0].split('.')[-1]) or 0)
            FakeCamera.state[ip] = {
                'version': random.choice(['4.9.7', '4.10.2', '4.11.0', FakeCamera.LATEST]),
                'serial': 'RB-%d-%05d' % (random.randint(23, 25), random.randint(0, 99999)),
                'offline': last % 10 == 9,
                'breaks': last % 10 == 7,
                'update_started': None,
                'checked': False,
            }
        self.s = FakeCamera.state[ip]

    def call(self, method, path, body=None, content_type=None):
        path = path.split('?')[0]
        s = self.s
        time.sleep(random.uniform(0.2, 0.6))
        if s['offline']:
            raise CameraError('unreachable', 'timed out')
        phase, elapsed = self._phase()
        if phase == 'rebooting':
            raise CameraError('unreachable', 'Connection refused')
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
        raise CameraError('http_error', method, path, 404, '', '', code=404)

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


def offered_update(value):
    if isinstance(value, dict):
        value = value.get('update_available')
    if not value or str(value).strip().lower() in ('false', 'none', 'no', '0', 'null'):
        return None
    return str(value).strip()


def set_cam(ip, **fields):
    with lock:
        if ip in cameras:
            cameras[ip].update(fields)


def check(ip):
    cam = cameras[ip]
    api = client_for(cam)
    set_cam(ip, status='checking', detail=detail('checking'), progress=None)
    info = api.call('GET', '/system/info')
    if not isinstance(info, dict):
        raise CameraError('bad_info')
    set_cam(ip, version=info.get('version'), serial=info.get('serial'), model=info.get('model'), online=True)
    answer = api.call('POST', '/system/actions/check_update')
    info = api.call('GET', '/system/info')
    current = info.get('version')
    avail = offered_update(answer)
    set_cam(ip, update_raw=avail)
    set_cam(ip, version=current, update_available=avail, last_check=now(),
            status='available' if avail else 'current',
            detail=detail('available', avail) if avail else detail('current'))
    return api, info, avail


def error_fields(e):
    offline = getattr(e, 'key', None) == 'unreachable'
    return {'status': 'offline' if offline else 'error', 'online': not offline,
            'detail': detail(e.key, *e.args_)}


def run_check(ip):
    set_cam(ip, status='queued', detail=detail('waiting'), progress=None)
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
    entry = {'time': '', 'ip': ip, 'name': cam.get('name') or '', 'serial': '',
             'version_before': '', 'version_after': '', 'result': '', 'message': ''}
    set_cam(ip, status='queued', detail=detail('waiting_slot'), progress=None)
    with update_slots:
        try:
            api, info, avail = check(ip)
            before = info.get('version')
            entry.update(serial=info.get('serial') or '', version_before=before or '')
            if not avail:
                entry.update(version_after=before or '', result=RESULT_CURRENT)
                return

            rounds = []
            fmt = None
            for round_no in range(1, UPDATE_ROUNDS + 1):
                if not avail:
                    break
                set_cam(ip, status='updating', detail=detail('starting_update', avail), progress=0)
                fmt = perform_update(ip, api, avail, avail)
                if not wait_update_done(ip, api):
                    rounds.append('%s rejected by camera' % avail)
                    api, info, avail = recheck(ip)
                    break
                rounds.append(avail)
                set_cam(ip, last_update=now())
                set_cam(ip, status='updating', detail=detail('rechecking'), progress=None)
                api, info, avail = recheck(ip)
            final = info.get('version')
            entry.update(version_after=final or '', result=RESULT_SUCCESS,
                         message='Format: %s · %s' % (fmt, ' · '.join('round %d: %s' % (i + 1, v) for i, v in enumerate(rounds))))
            set_cam(ip, version=final, update_available=avail, status='available' if avail else 'current',
                    detail=detail('updated', final), progress=None)
        except CameraError as e:
            entry.update(result=RESULT_FAILED, message=str(e))
            set_cam(ip, progress=None, **error_fields(e))
        finally:
            entry['time'] = now()
            write_log(entry)
            set_cam(ip, busy=False)
            save_cameras()


def recheck(ip):
    for _ in range(6):
        try:
            return check(ip)
        except CameraError as e:
            if e.code and 400 <= e.code < 500:
                raise
            time.sleep(1 if DEMO else 10)
    return check(ip)


def send_perform_update(api, fmt, version, raw):
    answer = _send_perform_update(api, fmt, version, raw)
    if isinstance(answer, dict) and answer.get('success') is False:
        raise CameraError('success_false', json.dumps(answer, ensure_ascii=False)[:150], code=400)
    return answer


def _send_perform_update(api, fmt, version, raw):
    path = '/system/actions/perform_update'
    if fmt == 'json':
        return api.call('POST', path, {'version': version})
    if fmt == 'json_raw':
        return api.call('POST', path, {'version': raw})
    if fmt == 'json_string':
        return api.call('POST', path, json.dumps(version), 'application/json')
    if fmt == 'form':
        return api.call('POST', path, urllib.parse.urlencode({'version': version}), 'application/x-www-form-urlencoded')
    if fmt == 'query':
        return api.call('POST', path + '?' + urllib.parse.urlencode({'version': version}))
    if fmt == 'text':
        return api.call('POST', path, version)
    if fmt == 'raw':
        return api.call('POST', path, raw)
    return api.call('POST', path)


def perform_update(ip, api, version, raw):
    fmt = config['perform_update_body']
    if fmt != 'auto':
        send_perform_update(api, fmt, version, raw)
        return fmt
    tried = []
    for key in UPDATE_FORMATS:
        set_cam(ip, detail=detail('detecting_format', key))
        try:
            send_perform_update(api, key, version, raw)
        except CameraError as e:
            if e.code in (400, 415, 422, 500):
                tried.append(key)
                continue
            raise
        with lock:
            if config['perform_update_body'] == 'auto':
                config['perform_update_body'] = key
                save_config()
        return key
    raise CameraError('all_formats_rejected', ', '.join(tried), code=400)


def wait_update_done(ip, api):
    started = time.time()
    deadline = started + config['update_timeout_min'] * 60
    active, ready, rejected = False, 0, 0
    while time.time() < deadline:
        time.sleep(1 if DEMO else 4)
        try:
            prog = api.call('GET', '/system/info/update_progress')
            info = api.call('GET', '/system/info')
        except CameraError as e:
            if e.code and 400 <= e.code < 500:
                raise
            set_cam(ip, detail=detail('rebooting'), progress=None)
            active, ready = True, 0
            continue
        phase = prog.get('phase') if isinstance(prog, dict) else None
        if phase in ('starting', 'downloading', 'installing'):
            active, ready, rejected = True, 0, 0
        elif isinstance(prog, dict) and prog.get('error'):
            rejected += 1
            if rejected >= 3:
                return False
            continue
        pct = prog.get('percent') if isinstance(prog, dict) else None
        set_cam(ip, detail=detail('phase', phase or 'running'), progress=pct if isinstance(pct, (int, float)) else None)
        settled = phase == 'finished' and not (isinstance(info, dict) and info.get('software_down'))
        ready = ready + 1 if settled and (active or time.time() - started > (8 if DEMO else 60)) else 0
        if ready >= 3:
            return True
    raise CameraError('timeout', config['update_timeout_min'])


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


IP_RE = re.compile(r'^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}(:\d{1,5})?$')


def normalize_address(text):
    addr = re.sub(r'^[a-zA-Z]+://', '', text.strip()).split('/')[0]
    if addr and ':' not in addr:
        addr = '%s:%s' % (addr, config.get('port', 577))
    return addr


def parse_camera_lines(text):
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
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/state':
            self.send_json(public_state())
        elif path == '/api/log.csv':
            body = open(log_file(), 'rb').read() if os.path.exists(log_file()) else ';'.join(LOG_FIELDS).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="camera_update_log.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({'error': 'not found'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
        except ValueError:
            return self.send_json({'error': 'invalid JSON'}, 400)

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

        self.send_json({'error': 'not found'}, 404)


def main():
    global DEMO, DATA_DIR, config, update_slots
    parser = argparse.ArgumentParser(description='Camera update tool for ARVOO cameras')
    parser.add_argument('--demo', action='store_true', help='simulated cameras for trying it out')
    parser.add_argument('--host', default='127.0.0.1', help='0.0.0.0 = reachable from the network')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    DEMO = args.demo
    DATA_DIR = os.path.join(BASE_DIR, 'data-demo' if DEMO else 'data')
    os.makedirs(DATA_DIR, exist_ok=True)
    config = load_config()
    update_slots = threading.BoundedSemaphore(int(config['parallel']))
    migrate_log()
    load_cameras()

    if DEMO and not cameras:
        demo_text = '\n'.join([
            '10.32.47.131 Car park North', '10.32.47.132 Car park North', '10.32.47.135 Entrance South',
            '10.32.47.137 Exit South', '10.32.47.140 Staff car park', '10.32.47.149 Delivery zone',
        ])
        for e in parse_camera_lines(demo_text)[0]:
            cameras[e['ip']] = new_camera(e)
        save_cameras()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host = 'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host
    print('Camera update running%s: http://%s:%d  (stop with Ctrl+C)' % (' in DEMO mode' if DEMO else '', host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
