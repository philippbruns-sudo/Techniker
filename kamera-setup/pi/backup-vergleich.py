#!/usr/bin/env python3
# Vergleicht die Einstellungen einer Kamera mit dem Backup und zeigt jede Abweichung.
# Aufruf:  python3 backup-vergleich.py 192.168.8.21 [passwort] [backup-datei]
# Ohne Passwort wird das neue Kamera-Passwort aus config.json verwendet.
import base64
import json
import os
import re
import sys
import urllib.request

here = os.path.dirname(os.path.abspath(__file__))
ip = sys.argv[1] if len(sys.argv) > 1 else '192.168.8.21'
password = sys.argv[2] if len(sys.argv) > 2 else ''
if not password:
    with open(os.path.join(here, '..', 'config.json'), encoding='utf-8') as f:
        password = json.load(f).get('new_password', '')
path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(here, '..', 'backups', 'backup.json')
auth = 'Basic ' + base64.b64encode(('user:%s' % password).encode()).decode()


def get(section):
    req = urllib.request.Request('http://%s/%s' % (ip, section), headers={'Authorization': auth})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.load(res)


def short(value):
    text = json.dumps(value, ensure_ascii=False)
    text = re.sub(r'(api-key=)[^"&]+', r'\1***', text)
    return text if len(text) <= 70 else text[:67] + '...'


def compare(expected, actual, where):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected):
            if key not in actual:
                print('  fehlt in Kamera  %s/%s = %s' % (where, key, short(expected[key])))
            else:
                compare(expected[key], actual[key], '%s/%s' % (where, key))
    elif expected != actual and not (isinstance(expected, (int, float)) and isinstance(actual, (int, float))
                                     and not isinstance(expected, bool) and abs(expected - actual) < 1e-6):
        print('  anders  %s\n      Backup: %s\n      Kamera: %s' % (where, short(expected), short(actual)))


with open(path, encoding='utf-8') as f:
    backup = json.load(f)
for section, expected in backup.items():
    print('== %s' % section)
    try:
        compare(expected, get(section), '')
    except Exception as e:
        print('  Fehler beim Lesen: %s' % e)
