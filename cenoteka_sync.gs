/**
 * CENOTEKA SYNC — Google Apps Script
 * ------------------------------------------------------------
 * Automatski (na dnevnom tajmeru) povlači sveže cenovnike sa
 * data.gov.rs, izdvaja SAMO najnoviji datum cenovnika (da ne
 * čuvamo 900MB istorije), i snima manji CSV u Drive folder.
 *
 * Zasto ovako umesto "skini ceo fajl":
 * - Fajlovi sa data.gov.rs sadrze CELU istoriju cena od septembra
 *   2025 i idu i preko 900MB po trgovcu.
 * - Apps Script (UrlFetchApp) ima limit odgovora ~50MB i ogranicenu
 *   memoriju/vreme izvrsavanja (6 min za trigere), pa ceo fajl NE MOZE
 *   da se povuce ovde.
 * - Zato koristimo HTTP Range zahteve: skinemo mali komad sa POCETKA
 *   fajla (da dobijemo header/kolone) i mali komad sa KRAJA fajla
 *   (gde su, ako je fajl hronoloski appendovan, najnoviji redovi).
 * - Ako server ne podrzava Range, skript to detektuje i upise
 *   gresku u log fajl u Drive-u umesto da pukne.
 *
 * PODESAVANJE (uradi jednom):
 * 1. Kreiraj Drive folder (npr. "cenoteka-podaci") i iz URL-a
 *    kopiraj njegov ID (deo posle /folders/).
 * 2. Zalepi taj ID u FOLDER_ID ispod.
 * 3. U script.google.com: Project Settings > uveri se da je
 *    V8 runtime ukljucen (podrazumevano jeste).
 * 4. Pokreni funkciju `createDailyTrigger` RUCNO jednom (odobri
 *    dozvole kad zatrazi). Ovo podesava dnevni okidac.
 * 5. Gotovo — od sada se `syncCenovnici` sam pokrece svaki dan za
 *    SVIH 27 trgovaca definisanih Uredbom (spisak u IZVORI ispod).
 *
 * Kako radi pronalazenje linkova (VAZNA IZMENA):
 * Umesto hardkodovanih CSV linkova (koji zastarevaju kad trgovac
 * posalje nov fajl), skript u runtime-u pita zvanicni API portala
 * (data.gov.rs/api/1/datasets/{slug}/) za trenutni link svakog
 * trgovca. API ima polje "latest" koje garantovano uvek vodi ka
 * najnovijoj verziji resursa. Ovo znaci da IZVORI lista ispod sadrzi
 * samo trajne "slug" identifikatore, ne direktne fajl-linkove.
 *
 * Mozes i rucno pokrenuti `syncCenovnici` da odmah testiras.
 */

// ====== PODESI OVO ======
const FOLDER_ID = 'STAVI_OVDE_ID_DRIVE_FOLDERA';

// Izvori: SVIH 27 trgovaca definisanih Uredbom, identifikovani preko
// njihovog "slug"-a na data.gov.rs (deo URL-a posle /sr/datasets/).
// Umesto da hardkodujemo CSV linkove (koji se menjaju), skript u
// runtime-u pita zvanicni API portala za trenutni link svakog trgovca
// (API ima polje "latest" - trajni link koji uvek vodi ka najnovijoj
// verziji fajla). Ovo resava problem "linkovi umiru za par nedelja".
const IZVORI = [
  { naziv: 'idea',              slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-7' },
  { naziv: 'domaca_trgovina',   slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-10' },
  { naziv: 'lidl',              slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-13' },
  { naziv: 'gomex',             slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-23' },
  { naziv: 'univerexport',      slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-12' },
  { naziv: 'beltorg',           slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-22' },
  { naziv: 'dis',               slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-8' },
  { naziv: 'qvattro',           slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-25' },
  { naziv: 'ros_produkt',       slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-11' },
  { naziv: 'metalac_proleter',  slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-9' },
  { naziv: 'cash_carry_kula',   slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-5' },
  { naziv: 'aman',              slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-16' },
  { naziv: 'sumadija_market',   slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-17' },
  { naziv: 'trnava_promet',     slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-18' },
  { naziv: 'tekijanka',         slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-19' },
  { naziv: 'maxi_delhaize',     slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-27' },
  { naziv: 'medius',            slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-28' },
  { naziv: 'leon_conditors',    slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-4' },
  { naziv: 'prima_nova',        slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-31' },
  { naziv: 'podunavlje',        slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-3' },
  { naziv: 'senta_promet',      slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-2' },
  { naziv: 'veropoulos',        slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-29' },
  { naziv: 'fortuna_market',    slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-30' },
  { naziv: 'vum',               slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-20' },
  { naziv: 'dexy_co_kids',      slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-1' },
  { naziv: 'bb_trade',          slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena' },
  { naziv: 'europrom',          slug: 'cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-24' },
];

const HEAD_BYTES = 8000;        // koliko skidamo sa pocetka (za header)
const TAIL_BYTES = 8000000;     // koliko skidamo sa kraja (~8MB, za najnovije redove)
// =========================


function createDailyTrigger() {
  // Ukloni stare trigere za ovu funkciju da ne dupliramo
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'syncCenovnici') {
      ScriptApp.deleteTrigger(t);
    }
  });

  ScriptApp.newTrigger('syncCenovnici')
    .timeBased()
    .everyDays(1)
    .atHour(6)
    .create();

  Logger.log('Dnevni okidac podesen (svaki dan oko 06h). Pokrecem i prvi sync odmah...');
  syncCenovnici();
}


// ========================================================================
// DIJAGNOSTIKA — pokreni ovo RUCNO da vidimo sta server stvarno vraca
// ========================================================================
function dijagnostikuj() {
  const url = resolveCsvUrl(IZVORI[0].slug); // idea

  const resp = UrlFetchApp.fetch(url, {
    headers: { Range: 'bytes=0-300' },
    muteHttpExceptions: true,
  });

  const status = resp.getResponseCode();
  const headers = resp.getHeaders();
  const bytes = resp.getContent(); // sirovi bajtovi, bez tekst-dekodiranja

  const prvih30Hex = bytes.slice(0, 30)
    .map(b => {
      const nn = (b + 256) % 256; // Apps Script bajtovi mogu biti signed
      return nn.toString(16).padStart(2, '0');
    })
    .join(' ');

  const poruka = [
    `STATUS: ${status}`,
    `HEADERS: ${JSON.stringify(headers, null, 2)}`,
    `PRVIH 30 BAJTOVA (hex): ${prvih30Hex}`,
    `DUZINA ODGOVORA (bajtova): ${bytes.length}`,
    `TEKST (UTF-8, prvih 200 char): ${resp.getContentText('UTF-8').slice(0, 200)}`,
  ].join('\n\n');

  Logger.log(poruka);

  // Upisi i u Drive da lakse kopiras/podelis
  const folder = DriveApp.getFolderById(FOLDER_ID);
  snimiFajl(folder, '_dijagnostika.txt', poruka);
}


function syncCenovnici() {
  const folder = DriveApp.getFolderById(FOLDER_ID);
  const logLinije = [];

  IZVORI.forEach(izvor => {
    try {
      const rezultat = obradiIzvor(izvor);
      logLinije.push(`[OK] ${izvor.naziv}: ${rezultat.brojRedova} redova, datum=${rezultat.datum}`);
      snimiFajl(folder, `${izvor.naziv}_najnovije.csv`, rezultat.csv);
    } catch (e) {
      logLinije.push(`[GRESKA] ${izvor.naziv}: ${e.message}`);
    }
  });

  const logTekst = `Sync ${new Date().toISOString()}\n` + logLinije.join('\n');
  Logger.log(logTekst);
  snimiFajl(folder, '_sync_log.txt', logTekst);
}


function resolveCsvUrl(slug) {
  const apiUrl = `https://data.gov.rs/api/1/datasets/${slug}/`;
  const resp = UrlFetchApp.fetch(apiUrl, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error(`API poziv nije uspeo za slug "${slug}" (status ${resp.getResponseCode()})`);
  }
  const data = JSON.parse(resp.getContentText('UTF-8'));
  const resursi = data.resources || [];

  // Trazimo CSV resurse (moze ih biti vise - glavni fajl sa cenama i
  // pomocni fajlovi kao sifarnik/dokumentacija). Biramo najveci CSV,
  // jer je to skoro uvek glavni fajl sa podacima o cenama.
  const csvResursi = resursi.filter(r =>
    (r.format || '').toLowerCase() === 'csv' ||
    (r.mime || '').toLowerCase().includes('csv')
  );

  if (csvResursi.length === 0) {
    throw new Error(`Nema CSV resursa za slug "${slug}". Dostupni formati: ${resursi.map(r => r.format).join(', ')}`);
  }

  csvResursi.sort((a, b) => (b.filesize || 0) - (a.filesize || 0));
  const glavni = csvResursi[0];

  // "latest" je trajni link koji API garantuje da uvek vodi ka
  // najnovijoj verziji ovog konkretnog resursa
  return glavni.latest || glavni.url;
}


function obradiIzvor(izvor) {
  const csvUrl = resolveCsvUrl(izvor.slug);
  // VAZNO: trazimo Accept-Encoding: identity (bez gzip). Range zahtevi
  // na gzip-kompresovanom odgovoru vracaju korumpirane podatke jer se
  // gzip stream ne moze dekompresovati od proizvoljnog bajt-offseta.
  const NO_GZIP = { 'Accept-Encoding': 'identity' };

  // 1) Proveri da li server podrzava Range zahteve
  const headCheck = UrlFetchApp.fetch(csvUrl, {
    method: 'get',
    headers: Object.assign({ Range: 'bytes=0-100' }, NO_GZIP),
    muteHttpExceptions: true,
    followRedirects: true,
  });

  const status = headCheck.getResponseCode();
  const podrzavaRange = status === 206; // 206 Partial Content = radi

  if (!podrzavaRange) {
    throw new Error(
      `Server ne vraca 206 za Range zahtev (vratio ${status}). ` +
      `Fajl je verovatno prevelik da se skine na ovaj nacin — ` +
      `potrebna je izmena pristupa (npr. spoljni server/Cloud Function ` +
      `bez ogranicenja Apps Script-a).`
    );
  }

  // 2) Skini pocetak (header/kolone)
  const headResp = UrlFetchApp.fetch(csvUrl, {
    headers: Object.assign({ Range: `bytes=0-${HEAD_BYTES}` }, NO_GZIP),
    muteHttpExceptions: true,
  });
  // VAZNO: razliciti trgovci mogu slati fajl u razlicitom encoding-u
  // (videli smo da "idea" salje UTF-16LE, dok drugi mogu slati obican
  // UTF-8). Umesto da nagadjamo, detektujemo na osnovu BOM bajtova.
  const headBytes = headResp.getContent();
  const encoding = detektujEncoding(headBytes);

  const headText = headResp.getContentText(encoding).replace(/^\uFEFF/, '');
  const headerLinija = headText.split(/\r?\n/)[0];
  // Delimiter takodje moze da varira — probaj tacka-zarez, pa zarez
  const delimiter = headerLinija.includes(';') ? ';' : ',';
  const kolone = parsirajCsvLiniju(headerLinija, delimiter);

  // 3) Saznaj ukupnu velicinu fajla (iz Content-Range headera)
  const contentRange = headResp.getHeaders()['Content-Range'] || headResp.getHeaders()['content-range'];
  let ukupnaVelicina = null;
  if (contentRange) {
    const m = contentRange.match(/\/(\d+)$/);
    if (m) ukupnaVelicina = parseInt(m[1], 10);
  }

  // 4) Skini kraj fajla (najnoviji redovi, pod pretpostavkom da su
  // podaci hronoloski appendovani)
  let rangeOd = 0;
  if (ukupnaVelicina) {
    rangeOd = Math.max(0, ukupnaVelicina - TAIL_BYTES);
    // UTF-16 karakteri su 2 bajta — ako pocnemo na neparnom bajtu,
    // CELO dekodiranje ce biti pomereno i sve ce izgledati kao smece.
    // Poravnajmo na paran broj (fajl pocinje BOM-om na bajtu 0, sto je parno).
    if (rangeOd % 2 !== 0) rangeOd -= 1;
  }
  const tailResp = UrlFetchApp.fetch(csvUrl, {
    headers: Object.assign(
      { Range: rangeOd ? `bytes=${rangeOd}-` : `bytes=-${TAIL_BYTES}` },
      NO_GZIP
    ),
    muteHttpExceptions: true,
  });
  const tailText = tailResp.getContentText(encoding);

  // Prvi red u tail komadu je verovatno "presecen" (pocinje usred reda,
  // moze i usred UTF-16 par-bajta) — bacamo ga
  const tailLinije = tailText.split(/\r?\n/).slice(1);

  // 5) Parsiraj redove i nadji kolonu za datum cenovnika
  const idxDatum = pronadjiIndeksKolone(kolone, ['datum cenovnika']);
  if (idxDatum === -1) {
    throw new Error(`Ne mogu da pronadjem kolonu "Datum cenovnika" u headeru: ${kolone.join(' | ')}`);
  }

  const parsiraniRedovi = [];
  let najnovijiDatum = null;

  tailLinije.forEach(linija => {
    if (!linija.trim()) return;
    const polja = parsirajCsvLiniju(linija, delimiter);
    if (polja.length < kolone.length) return; // nepotpun/presecen red

    const datumStr = (polja[idxDatum] || '').trim();
    const datum = parsirajDatum(datumStr);
    if (!datum) return;

    if (!najnovijiDatum || datum > najnovijiDatum) {
      najnovijiDatum = datum;
    }
    parsiraniRedovi.push({ datum, polja });
  });

  if (!najnovijiDatum) {
    const primer = tailLinije.slice(0, 3).join(' || ');
    throw new Error(
      `Nije pronadjen nijedan validan datum u poslednjem delu fajla. ` +
      `Kolona datuma (indeks ${idxDatum}, "${kolone[idxDatum]}"). ` +
      `Primer prvih redova (delimiter="${delimiter}"): ${primer.slice(0, 500)}`
    );
  }

  // 6) Zadrzi samo redove sa najnovijim datumom
  const filtrirani = parsiraniRedovi
    .filter(r => r.datum.getTime() === najnovijiDatum.getTime())
    .map(r => r.polja);

  // 7) Sastavi novi (mnogo manji) CSV
  const noviCsv = [kolone, ...filtrirani]
    .map(red => red.map(csvEscape).join(','))
    .join('\n');

  return {
    csv: noviCsv,
    brojRedova: filtrirani.length,
    datum: najnovijiDatum.toISOString().slice(0, 10),
  };
}


function snimiFajl(folder, imeFajla, sadrzaj) {
  const postojeci = folder.getFilesByName(imeFajla);
  if (postojeci.hasNext()) {
    const fajl = postojeci.next();
    fajl.setContent(sadrzaj);
  } else {
    folder.createFile(imeFajla, sadrzaj, MimeType.CSV);
  }
}


// ---------- pomocne funkcije ----------

function detektujEncoding(bytes) {
  // Apps Script vraca bajtove kao signed (-128..127), normalizujemo
  const b = i => (bytes[i] + 256) % 256;

  if (bytes.length >= 2 && b(0) === 0xff && b(1) === 0xfe) return 'UTF-16LE';
  if (bytes.length >= 2 && b(0) === 0xfe && b(1) === 0xff) return 'UTF-16BE';
  if (bytes.length >= 3 && b(0) === 0xef && b(1) === 0xbb && b(2) === 0xbf) return 'UTF-8';
  return 'UTF-8'; // podrazumevano, bez BOM-a
}

function parsirajCsvLiniju(linija, delimiter) {
  // Jednostavan CSV parser koji ume da izadje na kraj sa delimiterom
  // unutar navodnika. Izvorni fajl koristi TACKA-ZAREZ (;) kao delimiter.
  delimiter = delimiter || ';';
  const rezultat = [];
  let trenutno = '';
  let unutarNavodnika = false;

  for (let i = 0; i < linija.length; i++) {
    const c = linija[i];
    if (c === '"') {
      unutarNavodnika = !unutarNavodnika;
    } else if (c === delimiter && !unutarNavodnika) {
      rezultat.push(trenutno);
      trenutno = '';
    } else {
      trenutno += c;
    }
  }
  rezultat.push(trenutno);
  return rezultat.map(s => s.trim());
}

function csvEscape(vrednost) {
  const v = (vrednost || '').toString();
  if (v.includes(',') || v.includes('"') || v.includes('\n')) {
    return '"' + v.replace(/"/g, '""') + '"';
  }
  return v;
}

function pronadjiIndeksKolone(kolone, moguciNazivi) {
  const norm = s => s.toLowerCase().trim().replace(/[–_]/g, ' ');
  for (let i = 0; i < kolone.length; i++) {
    const k = norm(kolone[i]);
    if (moguciNazivi.some(n => k === norm(n) || k.includes(norm(n)))) {
      return i;
    }
  }
  return -1;
}

function parsirajDatum(str) {
  if (!str) return null;
  // Pokusaj YYYY-MM-DD
  let m = str.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  // Pokusaj DD.MM.YYYY
  m = str.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})/);
  if (m) return new Date(Date.UTC(+m[3], +m[2] - 1, +m[1]));
  // Pokusaj DD-MM-YYYY (npr. "09-03-2026")
  m = str.match(/^(\d{1,2})-(\d{1,2})-(\d{4})/);
  if (m) return new Date(Date.UTC(+m[3], +m[2] - 1, +m[1]));
  return null;
}
