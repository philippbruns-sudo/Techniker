// ==UserScript==
// @name         SP – Projekt aus Zoho ins SP-Projekt Studio laden
// @namespace    smartparking
// @version      0.2
// @description  Liest die Grunddaten eines Zoho-CRM-Deals aus und öffnet damit das SP-Projekt Studio (Admin) vorbefüllt.
// @match        https://crm.zoho.eu/crm/*
// @grant        GM_openInTab
// ==/UserScript==

(function () {
  'use strict';

  // ------------------------------------------------------------------
  // KONFIGURATION
  // ------------------------------------------------------------------
  // /exec-URL der veröffentlichten Apps-Script-Web-App (ohne Parameter)
  const STUDIO_URL = 'https://script.google.com/macros/s/HIER_DEINE_DEPLOYMENT_ID/exec';

  // ------------------------------------------------------------------
  // 1. DATEN AUS DER ZOHO-DETAILSEITE LESEN
  // ------------------------------------------------------------------

  // Liefert die Datensatz-ID aus der URL, z.B. .../tab/Potentials/698580000040431079
  function getRecordId() {
    const m = location.pathname.match(/\/tab\/Potentials\/(\d+)/);
    return m ? m[1] : null;
  }

  function clean(text) {
    return (text || '').replace(/ /g, ' ').replace(/[ \t]+\n/g, '\n').trim();
  }

  // Sammelt alle Felder der Detailansicht:
  //   { 'Site City': { key: 'POTENTIALCF12', value: 'Monschau' }, ... }
  // Zoho-Aufbau: Label = #labelTD_<KEY>, Wert = #value_<KEY>
  function readAllFields() {
    const fields = {};

    // Titel (Deal-Name) steht im Kopfbereich
    const title = document.querySelector('#title_POTENTIALNAME');
    if (title) fields['Site/Deal-Name'] = { key: 'POTENTIALNAME', value: clean(title.innerText) };

    document.querySelectorAll('[id^="labelTD_"]').forEach(labelEl => {
      const key = labelEl.id.substring('labelTD_'.length);
      const label = clean(labelEl.innerText);
      if (!label) return;
      const valueEl = document.getElementById('value_' + key) ||
        document.querySelector('#mouseArea__' + CSS.escape(key) + ' .dv_info_value');
      const value = valueEl ? clean(valueEl.innerText) : '';
      // Ersten nicht-leeren Treffer behalten (Labels können doppelt vorkommen)
      if (!fields[label] || (!fields[label].value && value)) {
        fields[label] = { key, value };
      }
    });
    return fields;
  }

  // Wert per Bezeichnung holen – mehrere Alternativen möglich (z.B. Deutsch/Englisch)
  function pick(fields, ...labels) {
    for (const l of labels) {
      if (fields[l] && fields[l].value && fields[l].value !== '-') return fields[l].value;
    }
    return '';
  }

  // ------------------------------------------------------------------
  // 2. ZOHO-FELDER -> STUDIO-FELDER
  // ------------------------------------------------------------------
  function buildProject(fields, recordId) {
    const street = [pick(fields, 'Site Street'), pick(fields, 'Site Street Number')].filter(Boolean).join(' ');
    const city = [pick(fields, 'Site Zip Code'), pick(fields, 'Site City')].filter(Boolean).join(' ');

    const notes = [];
    const addNote = (title, label) => { const v = pick(fields, label); if (v) notes.push(title + ': ' + v); };
    addNote('Beschreibung', 'Beschreibung');
    addNote('Ein-/Ausfahrt', 'Entry Exit Comment');
    addNote('Bewirtschaftungszeiten (geplant)', 'Proposed Hours of Management');
    addNote('Tarif (geplant)', 'Proposed Paid Parking Tariff');
    addNote('Kassenautomat-Standort(e)', 'Pay Station Location(s)');
    addNote('Tablet-Standorte', 'Tablet Locations');
    addNote('Interne Notizen', 'Internal Site Notes');

    return {
      name: pick(fields, 'Site/Deal-Name', 'Site Won'),
      id: recordId || pick(fields, 'CRM Record ID'),
      adresse: [street, city].filter(Boolean).join(', '),
      ansprechpartner: pick(fields, 'Contact Name', 'Kontaktname', 'Ansprechpartner'),
      telefon: pick(fields, 'Phone', 'Telefon', 'Mobile', 'Mobil'),
      email: pick(fields, 'Email', 'E-Mail'),
      notizen: notes.join('\n')
    };
  }

  // ------------------------------------------------------------------
  // 3. STUDIO ÖFFNEN
  // ------------------------------------------------------------------
  function openStudio() {
    const recordId = getRecordId();
    const fields = readAllFields();
    if (Object.keys(fields).length === 0) {
      alert('Keine Zoho-Felder gefunden – ist die Detailseite vollständig geladen?');
      return;
    }

    const project = buildProject(fields, recordId);

    // Alle nicht-leeren Zoho-Felder als Rohdaten mitgeben (für spätere Schritte)
    const raw = {};
    Object.keys(fields).forEach(l => { if (fields[l].value) raw[l] = fields[l].value; });

    const params = new URLSearchParams({ page: 'admin', src: 'zoho', zohoId: recordId || '' });
    Object.keys(project).forEach(k => params.set(k, project[k]));
    params.set('zoho', JSON.stringify(raw));

    let url = STUDIO_URL + '?' + params.toString();
    // Sehr lange URLs werden von Google abgelehnt -> Rohdaten dann weglassen
    if (url.length > 7500) {
      params.delete('zoho');
      url = STUDIO_URL + '?' + params.toString();
    }

    if (STUDIO_URL.includes('HIER_DEINE_DEPLOYMENT_ID')) {
      alert('Bitte im Tampermonkey-Skript oben STUDIO_URL eintragen.');
      return;
    }
    GM_openInTab(url, { active: true });
  }

  // ------------------------------------------------------------------
  // 4. BUTTON EINBLENDEN (Zoho ist eine Single-Page-App -> regelmäßig prüfen)
  // ------------------------------------------------------------------
  function updateButton() {
    const onDealDetail = !!getRecordId() && !!document.querySelector('[id^="labelTD_"]');
    let btn = document.getElementById('sp-studio-btn');

    if (!onDealDetail) {
      if (btn) btn.remove();
      return;
    }
    if (btn) return;

    btn = document.createElement('button');
    btn.id = 'sp-studio-btn';
    btn.textContent = '🚀 Projekt in SP-Projekt Studio laden';
    btn.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:999999;padding:12px 18px;' +
      'background:#ff6600;color:#fff;border:0;border-radius:8px;font:600 14px sans-serif;cursor:pointer;' +
      'box-shadow:0 4px 12px rgba(0,0,0,.25)';
    btn.addEventListener('click', openStudio);
    document.body.appendChild(btn);
  }

  setInterval(updateButton, 1500);
})();
