#!/usr/bin/env python3
"""
Dnevni pipeline: povlaci sveze cenovnike sa data.gov.rs, matchuje
proizvode preko barkoda za 10 velikih lanaca, i regenerise
app/uporedi-cene-mvp.html sa svezim podacima.

Predvidjen za GitHub Actions (nema limita velicine/vremena kao
Apps Script), ali radi i lokalno:

    pip install requests
    python data-pipeline/osvezi_podatke.py

Kljucne osobine (naucene iz prethodnih pokusaja):
- Linkove NE hardkodujemo: pitamo zvanicni udata API portala
  (data.gov.rs/api/1/datasets/{slug}/) koji ima "latest" polje -
  trajni link ka najnovijoj verziji fajla.
- Skidamo CEO fajl (moze biti i ~1GB - sadrzi istoriju od 2025),
  ali parsiramo strim-om, red po red, drzeci u memoriji SAMO redove
  najnovijeg datuma. Ovo resava bag gde je "uzorak sa kraja fajla"
  hvatao stare podatke kod trgovaca ciji fajlovi nisu hronoloski.
- Encoding se detektuje po BOM bajtovima (UTF-16LE / UTF-8),
  delimiter po headeru (; ili ,), datum u 3 formata.
"""

import csv
import io
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime

import requests

API_URL = "https://data.gov.rs/api/1/datasets/{slug}/"
USER_AGENT = "cena-app-pipeline/1.0 (github.com/vojinovic/cena-app)"

# ============================================================
# DIREKTNI IZVORI — trgovci koji objavljuju dnevne cenovnike na
# svom sajtu (po cl. 6 novog Zakona o zastiti potrosaca), jer su
# prestali da salju na data.gov.rs portal.
#
# Maxi: static.maxi.rs/assets/pricelist/{DD-MM-YYYY}/{FAJL}_{YYYYMMDD}.csv
# Fajl je cenovnik JEDNE prodavnice (reprezentativna, Beograd),
# format: BARKOD;NAZIV;REDOVNA CENA;CENA PO JM;SNIZENA CENA
# (cene sa " rsd" sufiksom, delimiter ;, UTF-8 sa BOM)
# ============================================================
MAXI_STORE = "201_BUKOVIK_TAKOVSKA_9_STARI_GRAD_BEOGRAD"
MAXI_URL_TEMPLATE = (
    "https://static.maxi.rs/assets/pricelist/"
    "{dd}-{mm}-{yyyy}/" + MAXI_STORE + "_{yyyy}{mm}{dd}.csv"
)

# 10 velikih lanaca: prikazno ime -> slug na data.gov.rs
LANCI = {
    "Lidl":            "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-13",
    "Idea":            "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-7",
    "Dis":             "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-8",
    # Maxi vise NE ide preko portala (zastareo, feb 2026) — ima direktan
    # dnevni izvor na static.maxi.rs, vidi preuzmi_maxi_direktno()
    "Univerexport":    "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-12",
    "Gomex":           "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-23",
    "Aman":            "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-16",
    "Veropoulos":      "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-29",
    "Fortuna Market":  "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-30",
    "Domaća trgovina": "cenovnici-proizvoda-po-uredbi-o-obaveznoj-evidenciji-i-dostavljanju-cena-10",
}

PLACEHOLDER_BRENDOVI = {
    "brend", "rm nije definisana", "ostalo", "nema", "n/a", "nepoznato", "-", "", "roba"
}

IKONE = {
    "slatki konditori i cerealije": "🍫",
    "lična higijena i kozmetika": "🧴",
    "sveže i prerađeno meso": "🥩",
    "bezalkoholna pića, kafa, čaj": "☕",
    "mleko, mlečni, mešoviti jaja": "🥛",
    "mleko, mlečni, mešoviti, jaja": "🥛",
    "kućna hemija": "🧹",
    "slani konditori": "🥨",
    "mahunarke": "🫘",
    "smrznuti proizvodi": "❄️",
    "prerada voća i povrća": "🥫",
    "hleb i peciva": "🍞",
    "papirna i kuhinjska galanterija": "🧻",
    "sveže voće i povrće": "🍎",
    "sveža i prerađena riba": "🐟",
    "so i začini": "🧂",
    "hrana za bebe": "🍼",
    "testenine": "🍝",
    "alkoholna pića": "🍺",
    "ulja i masti": "🫒",
    "med, džem, namazi": "🍯",
    "pirinač, brašno, šećer": "🌾",
    "hrana za kućne ljubimce": "🐾",
}


def log(msg):
    print(msg, flush=True)


def resolve_csv_urls(slug):
    """Pita udata API i vraca linkove SVIH CSV resursa dataseta.

    Neki trgovci imaju vise resursa (npr. stara kumulativna istorija +
    svezi nedeljni fajl, ili poseban sifarnik). Obradjujemo sve, a
    resurse bez kljucnih kolona parser sam preskace.
    """
    resp = requests.get(API_URL.format(slug=slug), headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    csv_resursi = [
        r for r in data.get("resources", [])
        if (r.get("format") or "").lower() == "csv" or "csv" in (r.get("mime") or "").lower()
    ]
    if not csv_resursi:
        raise RuntimeError(f"Nema CSV resursa za slug {slug}")
    # veci prvo — cesto je glavni; ali obradjujemo sve
    csv_resursi.sort(key=lambda r: r.get("filesize") or 0, reverse=True)
    return [r.get("latest") or r["url"] for r in csv_resursi]


def detektuj_encoding(prvi_bajtovi):
    if prvi_bajtovi[:2] == b"\xff\xfe":
        return "utf-16-le"
    if prvi_bajtovi[:2] == b"\xfe\xff":
        return "utf-16-be"
    if prvi_bajtovi[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    return "utf-8"


def parsiraj_datum(s):
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})", s)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def parsiraj_cenu(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def normalizuj_header(h):
    return (h or "").strip().lower().replace("–", "-").replace("_", " ")


def nadji_kolonu(fieldnames, kandidati):
    for f in fieldnames:
        fn = normalizuj_header(f)
        for k in kandidati:
            if fn == normalizuj_header(k):
                return f
    for f in fieldnames:
        fn = normalizuj_header(f)
        for k in kandidati:
            if normalizuj_header(k) in fn:
                return f
    return None


def cist_brend(b):
    b = (b or "").strip()
    return "" if b.lower() in PLACEHOLDER_BRENDOVI else b


def ikona_za(kat, naziv=""):
    """Ikona po nazivu proizvoda (preciznije), pa po kategoriji (fallback).

    Razlog: kategorije su preskiroke — npr. 'Bezalkoholna pica, kafa, caj'
    pokriva i kiselu vodu i kafu, pa je voda dobijala solju kafe.
    """
    n = (naziv or "").lower()
    # redosled bitan: specificnije prvo
    PO_NAZIVU = [
        (("voda ", " voda", "voda,"), "💧"),
        (("sok ", " sok", "nektar", "juice"), "🧃"),
        (("pivo",), "🍺"),
        (("vino", "vinjak"), "🍷"),
        (("kafa", "espresso", "cappuc", "kapuc"), "☕"),
        (("caj ", " caj", "čaj"), "🫖"),
        (("energet",), "⚡"),
        (("mleko", "jogurt", "kefir", "pavlaka", "kiselo ml"), "🥛"),
        (("sir ", " sir", "kackavalj", "kačkavalj", "gauda", "trapist"), "🧀"),
        (("jaja", "jaje"), "🥚"),
        (("hleb", "pecivo", "kifla", "lepinja", "tost "), "🍞"),
        (("cokolad", "čokolad", "bombon", "keks", "napolitank", "vafl"), "🍫"),
        (("cips", "čips", "flips", "smoki", "stapici", "štapići", "krekeri", "grickalice"), "🥨"),
        (("sladoled",), "🍦"),
    ]
    for kljucevi, ik in PO_NAZIVU:
        if any(k in n for k in kljucevi):
            return ik

    k = (kat or "").strip().lower().replace(",", ", ").replace("  ", " ")
    return IKONE.get(k, "🛒")


def parsiraj_maxi_cenu(s):
    """Maxi cene dolaze kao '749.99 rsd' — skidamo sufiks pa parsiramo."""
    s = (s or "").strip().lower().replace("rsd", "").strip()
    return parsiraj_cenu(s)


def preuzmi_maxi_direktno():
    """
    Povlaci dnevni Maxi cenovnik direktno sa static.maxi.rs (objavljen
    po cl. 6 Zakona o zastiti potrosaca). Proba danas, pa unazad do 7
    dana (fajl za tekuci dan ponekad kasni).

    Vraca (redovi, datum) u istom formatu kao preuzmi_i_parsiraj().
    """
    from datetime import timedelta

    for pomak in range(0, 7):
        datum = datetime.now() - timedelta(days=pomak)
        url = MAXI_URL_TEMPLATE.format(
            dd=datum.strftime("%d"), mm=datum.strftime("%m"), yyyy=datum.strftime("%Y")
        )
        try:
            resp = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200 or len(resp.content) < 1000:
                continue

            tekst = resp.content.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(tekst), delimiter=";")

            col_barkod = nadji_kolonu(reader.fieldnames, ["BARKOD PROIZVODA"])
            col_naziv = nadji_kolonu(reader.fieldnames, ["NAZIV PROIZVODA"])
            col_redovna = nadji_kolonu(reader.fieldnames, ["REDOVNA CENA"])
            col_snizena = nadji_kolonu(reader.fieldnames, ["SNIZENA CENA"])

            if not col_barkod or not col_redovna:
                log(f"[Maxi direktno] Neocekivan header: {reader.fieldnames}")
                continue

            redovi = []
            for row in reader:
                bk = (row.get(col_barkod) or "").strip()
                if not bk or not bk.isdigit() or len(bk) < 8:
                    continue
                redovna = parsiraj_maxi_cenu(row.get(col_redovna))
                if redovna is None or redovna <= 0:
                    continue
                snizena = parsiraj_maxi_cenu(row.get(col_snizena)) if col_snizena else None
                cena = snizena if (snizena and snizena > 0) else redovna
                redovi.append({
                    "barkod": bk,
                    "naziv": (row.get(col_naziv) or "").strip(),
                    "brend": "",   # Maxi fajl nema kolonu brenda
                    "kat": "",     # ni kategorije — ikona ce biti default
                    "cena": cena,
                })

            if redovi:
                log(f"[Maxi direktno] {len(redovi)} zapisa za {datum.strftime('%d.%m.%Y')}")
                return redovi, datum
        except requests.exceptions.RequestException as e:
            log(f"[Maxi direktno] {datum.strftime('%d.%m.%Y')}: {e}")

    raise RuntimeError("Maxi direktni cenovnik nedostupan za poslednjih 7 dana")


def preuzmi_i_parsiraj(ime_lanca, url):
    """
    Skida ceo CSV (strim na disk), parsira red po red, i vraca listu
    zapisa SAMO sa najnovijim datumom cenovnika u celom fajlu.

    Memorijski trik: drzimo samo redove trenutno-najnovijeg datuma;
    kad naidjemo na noviji, brisemo skupljeno i pocinjemo ispocetka.
    """
    log(f"[{ime_lanca}] Preuzimam {url[:100]}...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp_path = tmp.name
        with requests.get(url, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            velicina = 0
            for chunk in r.iter_content(chunk_size=1 << 20):
                tmp.write(chunk)
                velicina += len(chunk)
        log(f"[{ime_lanca}] Preuzeto {velicina / 1e6:.0f} MB")

    try:
        with open(tmp_path, "rb") as f:
            encoding = detektuj_encoding(f.read(4))

        max_datum = None
        redovi = []

        with open(tmp_path, "r", encoding=encoding, errors="replace", newline="") as f:
            prva = f.readline()
            delimiter = ";" if prva.count(";") > prva.count(",") else ","
            reader = csv.DictReader(io.StringIO(prva.lstrip("\ufeff")), delimiter=delimiter)
            fieldnames = reader.fieldnames

            col_barkod = nadji_kolonu(fieldnames, ["Barkod proizvoda"])
            col_naziv = nadji_kolonu(fieldnames, ["Naziv proizvoda"])
            col_brend = nadji_kolonu(fieldnames, ["Robna marka"])
            col_kat = nadji_kolonu(fieldnames, ["NAZIV KATEGORIJE"])
            col_datum = nadji_kolonu(fieldnames, ["Datum cenovnika"])
            col_redovna = nadji_kolonu(fieldnames, ["Redovna cena"])
            col_snizena = nadji_kolonu(fieldnames, ["Snižena cena", "Snizena cena"])

            if not col_barkod or not col_datum or not col_redovna:
                raise RuntimeError(
                    f"Nedostaju kljucne kolone. Header: {fieldnames}"
                )

            data_reader = csv.DictReader(f, fieldnames=fieldnames, delimiter=delimiter)
            broj_redova = 0
            for row in data_reader:
                broj_redova += 1
                datum = parsiraj_datum(row.get(col_datum))
                if not datum:
                    continue
                if max_datum is None or datum > max_datum:
                    max_datum = datum
                    redovi = []
                if datum != max_datum:
                    continue

                bk = (row.get(col_barkod) or "").strip()
                if not bk or bk == "0000000000000":
                    continue
                redovna = parsiraj_cenu(row.get(col_redovna))
                if redovna is None or redovna <= 0:
                    continue
                snizena = parsiraj_cenu(row.get(col_snizena)) if col_snizena else None
                cena = snizena if (snizena and snizena > 0) else redovna

                redovi.append({
                    "barkod": bk,
                    "naziv": (row.get(col_naziv) or "").strip(),
                    "brend": cist_brend(row.get(col_brend)),
                    "kat": (row.get(col_kat) or "").strip() if col_kat else "",
                    "cena": cena,
                })

        log(f"[{ime_lanca}] {broj_redova} redova ukupno; najnoviji datum "
            f"{max_datum.date() if max_datum else '???'} sa {len(redovi)} zapisa")
        return redovi, max_datum

    finally:
        os.unlink(tmp_path)


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template_path = os.path.join(repo_root, "app", "template.html")
    izlaz_path = os.path.join(repo_root, "app", "uporedi-cene-mvp.html")

    if not os.path.exists(template_path):
        log(f"GRESKA: nema template fajla na {template_path}")
        sys.exit(1)

    po_barkodu = defaultdict(dict)
    statusi = []
    lanci_datumi = {}

    # --- Direktni izvori (sajtovi trgovaca) ---
    try:
        maxi_redovi, maxi_datum = preuzmi_maxi_direktno()
        for z in maxi_redovi:
            bk = z["barkod"]
            if "Maxi" not in po_barkodu[bk] or z["cena"] < po_barkodu[bk]["Maxi"]:
                po_barkodu[bk]["Maxi"] = z["cena"]
            if "_naziv" not in po_barkodu[bk]:
                po_barkodu[bk]["_naziv"] = z["naziv"]
                po_barkodu[bk]["_brend"] = ""
                po_barkodu[bk]["_ikona"] = "🛒"
        statusi.append(f"[OK] Maxi (direktno sa maxi.rs): {len(maxi_redovi)} zapisa, "
                       f"datum {maxi_datum.date()}")
        lanci_datumi["Maxi"] = maxi_datum.strftime("%d.%m.%Y.")
    except Exception as e:
        statusi.append(f"[GRESKA] Maxi (direktno): {e}")
        log(f"[Maxi direktno] GRESKA: {e}")

    # --- Portal izvori (data.gov.rs) ---

    for ime, slug in LANCI.items():
        try:
            urls = resolve_csv_urls(slug)
            rezultati = []  # (redovi, max_datum) po resursu
            for i, url in enumerate(urls, 1):
                try:
                    redovi, max_datum = preuzmi_i_parsiraj(f"{ime} #{i}", url)
                    if redovi and max_datum:
                        rezultati.append((redovi, max_datum))
                except Exception as e:
                    log(f"[{ime} #{i}] Preskacem resurs: {e}")

            if not rezultati:
                raise RuntimeError("nijedan resurs nije dao upotrebljive podatke")

            # zadrzi podatke iz resursa sa globalno najnovijim datumom
            # (ako vise resursa deli isti najnoviji datum, spajamo ih)
            globalni_max = max(m for _, m in rezultati)
            redovi = []
            for r, m in rezultati:
                if m == globalni_max:
                    redovi.extend(r)

            for z in redovi:
                bk = z["barkod"]
                if ime not in po_barkodu[bk] or z["cena"] < po_barkodu[bk][ime]:
                    po_barkodu[bk][ime] = z["cena"]
                if "_naziv" not in po_barkodu[bk]:
                    po_barkodu[bk]["_naziv"] = z["naziv"]
                    po_barkodu[bk]["_brend"] = z["brend"]
                    po_barkodu[bk]["_ikona"] = ikona_za(z["kat"], z["naziv"])
                else:
                    # dopuni bogatije podatke (Maxi direktni izvor nema
                    # brend/kategoriju, pa ih preuzimamo od portal-izvora)
                    if not po_barkodu[bk]["_brend"] and z["brend"]:
                        po_barkodu[bk]["_brend"] = z["brend"]
                    if po_barkodu[bk].get("_ikona", "🛒") == "🛒" and z["kat"]:
                        po_barkodu[bk]["_ikona"] = ikona_za(z["kat"], z["naziv"])
            statusi.append(
                f"[OK] {ime}: {len(redovi)} zapisa, datum {globalni_max.date()} "
                f"(od {len(urls)} resursa)"
            )
            lanci_datumi[ime] = globalni_max.strftime("%d.%m.%Y.")
        except Exception as e:
            statusi.append(f"[GRESKA] {ime}: {e}")
            log(f"[{ime}] GRESKA: {e}")

    proizvodi = []
    for bk, podaci in po_barkodu.items():
        cene = [[t, c] for t, c in podaci.items() if not t.startswith("_")]
        if len(cene) < 2:
            continue
        cene.sort(key=lambda x: x[1])
        proizvodi.append([podaci["_naziv"], podaci["_brend"], cene, podaci.get("_ikona", "🛒")])

    proizvodi.sort(key=lambda x: -len(x[2]))
    log(f"\nUkupno uporedivih proizvoda: {len(proizvodi)}")

    if len(proizvodi) < 1000:
        log("GRESKA: premalo proizvoda — nesto nije u redu sa izvorima, "
            "NE prepisujem postojecu stranicu.")
        for s in statusi:
            log("  " + s)
        sys.exit(1)

    data_json = json.dumps(proizvodi, ensure_ascii=False, separators=(",", ":"))

    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    danas = datetime.now().strftime("%d.%m.%Y")
    final = template.replace("__DATA_PLACEHOLDER__", data_json)
    final = final.replace("__DATUM_AZURIRANJA__", danas)
    final = final.replace("__LANCI_DATUMI__", json.dumps(lanci_datumi, ensure_ascii=False))

    with open(izlaz_path, "w", encoding="utf-8") as f:
        f.write(final)

    log(f"Stranica regenerisana: {izlaz_path} "
        f"({os.path.getsize(izlaz_path) / 1024:.0f} KB)")
    log("\nStatusi:")
    for s in statusi:
        log("  " + s)


if __name__ == "__main__":
    main()
