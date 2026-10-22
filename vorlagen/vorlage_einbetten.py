# Bettet die EasyPark-Vorlage als Base64 in Wizard.html ein.
# Aufruf aus dem Projektordner: python3 vorlagen/vorlage_einbetten.py
import base64
import os
import re

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
vorlage = os.path.join(root, 'vorlagen', 'EasyPark_Datenblatt_Vorlage.xlsx')
wizard = os.path.join(root, 'Wizard.html')

with open(vorlage, 'rb') as f:
    b64 = base64.b64encode(f.read()).decode('ascii')

with open(wizard, encoding='utf-8') as f:
    html = f.read()

html, n = re.subn(
    r"(// EASYPARK_TEMPLATE_START\n\s*var EASYPARK_TEMPLATE_BASE64 = ')[^']*(';)",
    lambda m: m.group(1) + b64 + m.group(2),
    html
)
if n != 1:
    raise SystemExit('Marker EASYPARK_TEMPLATE_START in Wizard.html nicht gefunden.')

with open(wizard, 'w', encoding='utf-8') as f:
    f.write(html)
print('Vorlage eingebettet (%d Zeichen).' % len(b64))
