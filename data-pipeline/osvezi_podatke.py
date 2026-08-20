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

# DIS: javni JSON API njihovog sajta (dis.rs/artikli ga koristi).
# Daje svez katalog sa cenama, ALI BEZ BARKODA - samo internu sifru.
# Zato pravimo most naziv->barkod iz DIS CSV-a sa portala (koji ima
# barkod ali su cene stare), vidi napravi_most_naziv_barkod().
DIS_API_URL = "https://www.dis.rs/api/Dis/Articles"
DIS_PER_PAGE = 200
DIS_MAX_STRANA = 60          # zastita od beskonacne petlje

# Univerexport: JSON API sa "Jedinicna cena artikala" stranice.
# Daje cene, ali BEZ barkoda i BEZ sifre - samo naziv skracen na 20
# znakova. Zato most ide prefiks-poredjenjem sa punim nazivima iz CSV-a.
UNIVER_API_URL = "https://univerexport.rs/api/fetchArtikliForObjekat"
UNIVER_OBJEKAT = "MP004"        # Resavska 4, Novi Sad

# Lidl: javni search API njihovog sajta. Daje naziv, brend, cenu i
# cenu po jedinici mere - ali BEZ EAN-a (samo interni "ian"/erpNumber).
# Srecom, Lidl na portalu ima DRUGI resurs koji je sifarnik
# (EANCODE + NAZIV_PROIZVODA) - njega koristimo kao most do barkoda.
LIDL_API_URL = "https://www.lidl.rs/q/api/search"
LIDL_FETCHSIZE = 100

# ============================================================
# PROVENANCE - kako je cena povezana sa proizvodom.
# Nije "kvalitet podatka" nego NACIN spajanja, jer od toga
# zavisi koliko smemo da verujemo da je to bas taj proizvod.
#   EAN_DIRECT  - izvor sam daje barkod (nema nagadjanja)
#   CODE_BRIDGE - most preko interne sifre artikla (jednoznacna)
#   NAME_MATCH  - poklapanje po nazivu (heuristika, moze da promasi)
# Pravilo: NAME_MATCH cena se prikazuje, ali NE MOZE biti
# proglasena najjeftinijom niti ulaziti u racunicu ustede.
# ============================================================
EAN_DIRECT = "ean"
CODE_BRIDGE = "code"
NAME_MATCH = "name"

# Obogacivanje: otvorena baza normalizovanih proizvoda iz projekta
# cijene.dev (Hrvatska). Barkod je globalan standard, pa njihovi podaci
# vaze i za nase artikle - dobijamo cist naziv, brend i KOLICINU
# (koja nam otvara cenu po jedinici mere).
# Licenca: CC BY-NC-SA 4.0 - nekomercijalno, uz obaveznu atribuciju.
ENRICH_URL = ("https://raw.githubusercontent.com/senko/cijene-api/"
              "main/enrichment/products.csv")

# Idea (Mercator-S): webshop API online.idea.rs. Najbolji izvor do
# sada - daje BARKODOVE, akcijske cene i cenu po jedinici mere, pa se
# spaja direktno kao Maxi, bez ikakvog mosta.
IDEA_API = "https://online.idea.rs/v2"
IDEA_PER_PAGE = 100

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
                na_akciji = bool(snizena and snizena > 0 and snizena < redovna)
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


def normalizuj_naziv(naziv):
    """Svodi naziv proizvoda na uporedivi oblik.

    'Dobro mleko UHT 2,8% m.m. 1 l' i 'Dobro mleko UHT 2,8% 1l'
    treba da daju isti kljuc.
    """
    n = (naziv or "").lower().strip()
    for a, b in (("\u010d","c"),("\u0107","c"),("\u0161","s"),("\u017e","z"),("\u0111","dj")):
        n = n.replace(a, b)
    n = n.replace(",", ".")
    n = re.sub(r"\bm\s*\.?\s*m\s*\.?", " ", n)
    n = re.sub(r"(\d)\s+(l|ml|g|kg|kom|pak)\b", r"\1\2", n)
    n = re.sub(r"\b(kom|kompak|pak|komad|art|vp)\b", " ", n)
    n = re.sub(r"[^a-z0-9%.]+", " ", n)
    reci = [r for r in n.split() if r not in (".", "")]
    return " ".join(sorted(reci))


def preuzmi_dis_api():
    """Povlaci ceo DIS katalog sa njihovog javnog API-ja."""
    svi = []
    ukupno = None
    for strana in range(1, DIS_MAX_STRANA + 1):
        try:
            resp = requests.get(
                DIS_API_URL,
                params={"page": strana, "perPage": DIS_PER_PAGE},
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            log(f"[Dis API] strana {strana}: {e}")
            break

        if ukupno is None:
            ukupno = payload.get("totalCount")
            log(f"[Dis API] totalCount = {ukupno}")

        stavke = payload.get("data") or []
        if not stavke:
            break

        for a in stavke:
            redovna = a.get("price") or 0
            akcijska = a.get("discountedPrice") or 0
            cena = akcijska if akcijska > 0 else redovna
            if cena <= 0:
                continue
            svi.append({
                "sifra": (a.get("code") or "").strip(),
                "naziv": (a.get("name") or "").strip(),
                "cena": float(cena),
                "kat": (a.get("categoryName") or "").strip(),
                "akcija": "akcija" if (redovna > 0 and akcijska > 0 and akcijska < redovna) else None,
            })

        if ukupno and len(svi) >= ukupno:
            break

    log(f"[Dis API] preuzeto {len(svi)} artikala sa cenom")
    return svi


def izvuci_dis_sifru(naziv):
    """DIS u CSV-u pise sifru artikla na pocetku naziva:
    '000066 Paradajz cherry sljivar 500 g' -> '000066'
    Ista sifra je u API polju 'code', pa preko nje spajamo izvore."""
    m = re.match(r"^\s*(\d{4,8})\s+", naziv or "")
    return m.group(1).lstrip("0") if m else None


def napravi_most_naziv_barkod(csv_redovi):
    """Mapa DIS_sifra -> (barkod, csv_red). Sifra je pouzdanija od naziva."""
    most = {}
    bez_sifre = 0
    for z in csv_redovi:
        sifra = izvuci_dis_sifru(z["naziv"])
        if not sifra:
            bez_sifre += 1
            continue
        most.setdefault(sifra, (z["barkod"], z))
    log(f"[Dis most] {len(most)} sifri iz CSV-a, {bez_sifre} redova bez sifre")
    for i, (k, v) in enumerate(list(most.items())[:3]):
        log(f"[Dis most] primer: sifra={k} barkod={v[0]} naziv='{v[1]['naziv'][:50]}'")
    return most


KOLICINA_RE = re.compile(r"^\d+[.,]?\d*(l|ml|g|kg|kom|komad|komada|pranje|pranja)$")


def normalizuj_tokene(naziv):
    """'BRAS PS T500 SENT1KG' -> bras ps t 500 sent 1kg"""
    n = (naziv or "").lower().strip()
    for a, b in (("\u010d","c"),("\u0107","c"),("\u0161","s"),("\u017e","z"),("\u0111","dj")):
        n = n.replace(a, b)
    n = n.replace(",", ".")
    n = re.sub(r"\bm\s*\.?\s*m\s*\.?", " ", n)
    n = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", n)
    n = re.sub(r"(\d)\s+(l|ml|g|kg|kom|komada?|pranja?)\b", r"\1kom" if False else r"\1\2", n)
    n = re.sub(r"[^a-z0-9%.]+", " ", n)
    n = re.sub(r"(?<=[a-z])(?=\d)", " ", n)
    return [t for t in n.split() if t != "."]


def kolicina_iz(tokeni):
    for t in tokeni:
        if KOLICINA_RE.match(t):
            return t
    return None


def kljuc_grupe(tokeni):
    if not tokeni:
        return None
    return (tokeni[0][:4], kolicina_iz(tokeni))


def prefiks_slaganje(kratki, puni):
    preostali = list(puni)
    for k in kratki:
        nasao = False
        for i, pp in enumerate(preostali):
            if pp.startswith(k) or k.startswith(pp):
                preostali.pop(i)
                nasao = True
                break
        if not nasao:
            return False
    return True


def preuzmi_univer_api():
    resp = requests.get(UNIVER_API_URL, params={"sif_site": UNIVER_OBJEKAT},
                        headers={"User-Agent": USER_AGENT}, timeout=90)
    resp.raise_for_status()
    svi = []
    for a in resp.json().get("data") or []:
        cena = a.get("MALOPRODAJNA_CENA")
        if not cena or float(cena) <= 0:
            continue
        svi.append({"naziv": (a.get("NAZIV_ARTIKLA") or "").strip(), "cena": float(cena)})
    log(f"[Univer API] preuzeto {len(svi)} artikala (objekat {UNIVER_OBJEKAT})")
    return svi


def napravi_most_prefiks(csv_redovi):
    most = defaultdict(list)
    vidjeni = set()
    for z in csv_redovi:
        tok = normalizuj_tokene(z["naziv"])
        g = kljuc_grupe(tok)
        if g:
            kljuc = (tuple(tok), z["barkod"])
            if kljuc not in vidjeni:
                vidjeni.add(kljuc)
                most[g].append((tok, z["barkod"], z))
    log(f"[Univer most] {len(most)} grupa iz CSV-a")
    for i, (g, v) in enumerate(list(most.items())[:6]):
        log(f"[Univer most] CSV primer {i+1}: grupa={g} naziv='{v[0][2]['naziv'][:55]}' tokeni={v[0][0][:8]}")
    return most


GENERICKE_RECI = {
    "select", "strong", "corner", "corn", "day", "snack", "sweet",
    "chef", "fresh", "gold", "classic", "premium", "natural", "extra",
    "original", "special", "super", "max", "plus", "mini", "maxi",
    "delikates", "family", "home", "kids", "bio", "eco", "light",
}


def brend_se_poklapa(brend, tokeni_drugog):
    """Da li se brend javlja u nazivu kod drugog lanca?

    Trazimo poklapanje CELE reci - ne prefiksa - i ignorisemo genericke
    reci ('select', 'strong'), jer bi inace 'CHEF SELECT salata'
    pogodila 'sir kozji select milk'."""
    if not brend:
        return False
    kandidati = [t for t in normalizuj_tokene(brend)
                 if len(t) >= 4 and t not in GENERICKE_RECI]
    if not kandidati:
        return False
    drugi = set(tokeni_drugog)
    return any(k in drugi for k in kandidati)


def skor_slaganja(a, b):
    """Broj tokena koji se poklapaju (prefiksno). Sto vise, to bolje."""
    skor = 0
    preostali = list(b)
    for t in a:
        for i, p2 in enumerate(preostali):
            if p2.startswith(t) or t.startswith(p2):
                preostali.pop(i)
                skor += 1
                break
    return skor


def delimicno_slaganje(a, b, min_zajednickih=2):
    """Blazi uslov od prefiks_slaganje: dovoljno je da se poklopi
    bar N tokena (prefiksno). Koristi se kad poredimo nazive IZMEDJU
    lanaca, gde se skracenice razlikuju ('deterdzent' vs 'det',
    'posudje' vs 'sudove')."""
    zajednickih = 0
    preostali = list(b)
    for t in a:
        for i, p2 in enumerate(preostali):
            if p2.startswith(t) or t.startswith(p2):
                preostali.pop(i)
                zajednickih += 1
                break
    return zajednickih >= min_zajednickih


def preuzmi_lidl_api():
    """Povlaci sve Lidl proizvode sa cenom (offset paginacija)."""
    svi, offset = [], 0
    for _ in range(60):
        resp = requests.get(LIDL_API_URL, params={
            "assortment": "RS", "locale": "sr_RS", "version": "v2.0.0",
            "fetchsize": LIDL_FETCHSIZE, "offset": offset,
        }, headers={"User-Agent": USER_AGENT}, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items") or []
        if not items:
            break
        for it in items:
            data = (it.get("gridbox") or {}).get("data") or {}
            pr = data.get("price") or {}
            cena = pr.get("price")
            if not cena or float(cena) <= 0:
                continue
            brend = ((data.get("brand") or {}).get("name") or "").strip()
            naziv = (data.get("fullTitle") or "").strip()
            # Lidl naziv nema gramazu, ali je ima u basePrice tekstu
            # ("750 ml / 1 l = 279.99") - lepimo je da bi match radio
            bp = ((pr.get("basePrice") or {}).get("text") or "")
            mg = re.match(r"\s*([\d.,]+\s*(?:x\s*[\d.,]+\s*)?(?:ml|l|g|kg|kom))\b", bp, re.I)
            if mg:
                naziv = naziv + " " + mg.group(1)
            svi.append({"naziv": naziv, "brend": brend, "cena": float(cena)})
        offset += len(items)
        ukupno = payload.get("numFound") or 0
        if offset >= ukupno:
            break
    log(f"[Lidl API] preuzeto {len(svi)} artikala sa cenom")
    return svi


def preuzmi_lidl_sifarnik(urls):
    """Iz Lidl resursa sa portala nalazi sifarnik (EANCODE /
    NAZIV_PROIZVODA) i pravi mapu tokeni -> EAN."""
    import tempfile as _tf
    for i, url in enumerate(urls, 1):
        tmp_path = None
        try:
            with _tf.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                tmp_path = tmp.name
                with requests.get(url, stream=True, timeout=120,
                                  headers={"User-Agent": USER_AGENT}) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        tmp.write(chunk)
            with open(tmp_path, "rb") as f:
                enc = detektuj_encoding(f.read(4))
            with open(tmp_path, "r", encoding=enc, errors="replace", newline="") as f:
                prva = f.readline()
                delim = ";" if prva.count(";") > prva.count(",") else ","
                fieldnames = next(csv.reader(io.StringIO(prva.lstrip("\ufeff")), delimiter=delim))
                col_ean = nadji_kolonu(fieldnames, ["EANCODE"])
                col_naz = nadji_kolonu(fieldnames, ["NAZIV_PROIZVODA"])
                if not col_ean or not col_naz:
                    continue
                rdr = csv.DictReader(f, fieldnames=fieldnames, delimiter=delim)
                most = defaultdict(list)
                broj = 0
                for row in rdr:
                    ean = (row.get(col_ean) or "").strip()
                    naz = (row.get(col_naz) or "").strip()
                    if not ean or not naz or not ean.isdigit():
                        continue
                    tok = normalizuj_tokene(naz)
                    g = kljuc_grupe(tok)
                    if g:
                        most[g].append((tok, ean, {"naziv": naz, "brend": "", "kat": ""}))
                        broj += 1
                log(f"[Lidl sifarnik] resurs #{i}: {broj} EAN zapisa, {len(most)} grupa")
                for j, (g, v) in enumerate(list(most.items())[:5]):
                    log(f"[Lidl sifarnik] primer {j+1}: grupa={g} naziv='{v[0][2]['naziv'][:55]}'")
                return most
        except Exception as e:
            log(f"[Lidl sifarnik] resurs #{i} preskocen: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return None


def izaberi_barkod(barcodes):
    """Bira pravi EAN iz liste. Barkodovi koji pocinju sa 2 su interni
    (roba na meru), koristimo ih samo ako nema pravog."""
    if not barcodes:
        return None
    pravi = [b for b in barcodes if len(b) == 13 and b.isdigit() and not b.startswith("2")]
    if pravi:
        return pravi[0]
    validni = [b for b in barcodes if b.isdigit() and len(b) >= 8]
    return validni[0] if validni else None


def preuzmi_idea_kategorije():
    """Skup ID-jeva svih kategorija (rekurzivno kroz podkategorije)."""
    ids = set()

    def pokupi(cvor):
        if isinstance(cvor, dict):
            if isinstance(cvor.get("id"), int):
                ids.add(cvor["id"])
            for v in cvor.values():
                pokupi(v)
        elif isinstance(cvor, list):
            for v in cvor:
                pokupi(v)

    try:
        r = requests.get(f"{IDEA_API}/categories", headers={"User-Agent": USER_AGENT}, timeout=60)
        r.raise_for_status()
        pokupi(r.json())
    except Exception as e:
        log(f"[Idea] /categories neuspesno ({e}), koristim poznate ID-jeve")

    if not ids:
        ids = {60007924, 60007925, 60028453, 60014703, 60014227, 60011867, 60014705}
    log(f"[Idea] {len(ids)} kategorija za obilazak")
    return sorted(ids)


def preuzmi_enrichment():
    """Mapa barkod -> {naziv, brend, kolicina, jedinica} iz cijene.dev baze."""
    try:
        r = requests.get(ENRICH_URL, headers={"User-Agent": USER_AGENT}, timeout=90)
        r.raise_for_status()
        rdr = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", errors="replace")))
        baza = {}
        for row in rdr:
            bk = (row.get("barcode") or "").strip()
            naziv = (row.get("name") or "").strip()
            if not bk or not naziv:
                continue
            try:
                kol = float(row.get("quantity") or 0)
            except ValueError:
                kol = 0
            baza[bk] = {"naziv": naziv, "brend": (row.get("brand") or "").strip(),
                        "kolicina": kol, "jedinica": (row.get("unit") or "").strip()}
        log(f"[Enrich] ucitano {len(baza)} normalizovanih proizvoda")
        return baza
    except Exception as e:
        log(f"[Enrich] neuspesno ({e}) - nastavljam bez obogacivanja")
        return {}


def preuzmi_idea_api():
    """Prolazi kroz kategorije i skuplja proizvode sa barkodom i cenom."""
    po_bk = {}
    kategorije = preuzmi_idea_kategorije()
    for i, kat in enumerate(kategorije, 1):
      try:
        strana = 1
        while strana <= 40:
            try:
                r = requests.get(f"{IDEA_API}/categories/{kat}/products",
                                 params={"per_page": IDEA_PER_PAGE, "page": strana},
                                 headers={"User-Agent": USER_AGENT}, timeout=60)
                if r.status_code != 200:
                    break
                payload = r.json()
            except Exception:
                break
            proizvodi = payload.get("products") or []
            if not isinstance(proizvodi, list) or not proizvodi:
                break
            for pr in proizvodi:
                if not isinstance(pr, dict):
                    continue
                bk = izaberi_barkod(pr.get("barcodes") or [])
                if not bk:
                    continue
                iznos = ((pr.get("price") or {}).get("amount") or 0) / 100.0
                if iznos <= 0:
                    continue
                kats = pr.get("categories") or []
                kat_ime = kats[0].get("name", "") if kats else ""
                ponuda = pr.get("offer") or {}
                akcija = None
                if ponuda:
                    kraj = (ponuda.get("end_on") or "").strip().rstrip(".")
                    stara = ((ponuda.get("original_price") or {}).get("amount") or 0) / 100.0
                    if kraj:
                        akcija = f"do {kraj[:5]}"
                    elif stara > iznos:
                        akcija = "akcija"
                if bk not in po_bk or iznos < po_bk[bk]["cena"]:
                    po_bk[bk] = {"barkod": bk, "naziv": (pr.get("name") or "").strip(),
                                 "brend": (pr.get("manufacturer") or "").strip(),
                                 "kat": kat_ime, "cena": iznos, "akcija": akcija}
            info = payload.get("_page") or {}
            if strana >= (info.get("page_count") or 1):
                break
            strana += 1
      except Exception as e:
        log(f"[Idea API] kategorija {kat} preskocena: {e}")
      if i % 25 == 0:
            log(f"[Idea API] {i}/{len(kategorije)} kategorija, {len(po_bk)} proizvoda")
    log(f"[Idea API] ukupno {len(po_bk)} proizvoda sa barkodom i cenom")
    return list(po_bk.values())


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


# ============================================================
# DETEKTOR NOVIH IZVORA
# Lanci koji nam jos fale. Zakon (cl. 6) ih obavezuje da objave
# cenovnik na sajtu I na portalu; ministarka je najavila da od
# septembra 2026. mora svakodnevno. Ovo svako jutro proveri da li
# se nesto pojavilo, da ne moramo rucno da pratimo.
# ============================================================
LANCI_KOJI_FALE = {
    "Roda": ["https://roda.rs/cenovnik", "https://roda.rs/cenovnici",
             "https://www.roda.rs/Cenovnik"],
    "Mercator": ["https://mercator.rs/cenovnik", "https://www.mercator.rs/Cenovnik"],
    "Gomex": ["https://gomex.rs/cenovnik", "https://www.gomex.rs/cenovnici"],
    "Aman": ["https://aman.co.rs/cenovnik", "https://www.aman.co.rs/cenovnici"],
    "Veropoulos": ["https://www.veropoulos.rs/cenovnik"],
    "Fortuna": ["https://fortunamarket.rs/cenovnik"],
    "Idea (zakonski)": ["https://www.idea.rs/cenovnik-objekat",
                        "https://www.idea.rs/jedinicna-cena-artikala"],
}

ZNACI_CENOVNIKA = ("csv", "cenovnik", "cjenik", "preuzmi", "download", "xml")


def detektuj_nove_izvore():
    """Proverava sajtove lanaca koji nam fale + portal za nove datasete."""
    nalazi = []

    for lanac, urls in LANCI_KOJI_FALE.items():
        for url in urls:
            try:
                r = requests.get(url, timeout=20, allow_redirects=True,
                                 headers={"User-Agent": USER_AGENT})
                if r.status_code != 200 or len(r.content) < 500:
                    continue
                tekst = r.text.lower()
                pogodci = [z for z in ZNACI_CENOVNIKA if z in tekst]
                if len(pogodci) >= 2:
                    nalazi.append(f"NOVO? {lanac}: {url} vraca 200 "
                                  f"({len(r.content)//1024} KB, sadrzi: {', '.join(pogodci[:4])})")
                    break
            except requests.exceptions.RequestException:
                continue

    for ime, slug in LANCI.items():
        if ime in ("Domaća trgovina",):
            continue
        try:
            r = requests.get(API_URL.format(slug=slug),
                             headers={"User-Agent": USER_AGENT}, timeout=30)
            if r.status_code != 200:
                continue
            data = r.json()
            zadnja = (data.get("last_modified") or data.get("last_update") or "")[:10]
            if zadnja:
                d = parsiraj_datum(zadnja)
                if d and (datetime.now() - d).days <= 14:
                    nalazi.append(f"NOVO? {ime}: portal dataset osvezen {zadnja}")
        except Exception:
            continue

    if nalazi:
        log("")
        log("=" * 60)
        for n in nalazi:
            log(f"[Detektor] {n}")
        log("=" * 60)
    else:
        log("[Detektor] nista novo (Roda, Gomex, Aman, Veropoulos, Fortuna i dalje cute)")
    return nalazi


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
    dis_csv_redovi = []      # cuvamo za naziv->barkod most
    univer_csv_redovi = []
    lidl_urls = []

    # --- Direktni izvori (sajtovi trgovaca) ---
    try:
        maxi_redovi, maxi_datum = preuzmi_maxi_direktno()
        for z in maxi_redovi:
            bk = z["barkod"]
            if "Maxi" not in po_barkodu[bk] or z["cena"] < po_barkodu[bk]["Maxi"]:
                po_barkodu[bk]["Maxi"] = z["cena"]
                po_barkodu[bk]["_prov_Maxi"] = EAN_DIRECT
                if z.get("akcija"):
                    po_barkodu[bk]["_akcija_Maxi"] = z["akcija"]
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
            if ime == "Lidl":
                lidl_urls = list(urls)
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

            # DIS CSV cuvamo posebno - iz njega pravimo naziv->barkod most
            if ime == "Dis":
                dis_csv_redovi = list(redovi)
            if ime == "Univerexport":
                univer_csv_redovi = list(redovi)

            for z in redovi:
                bk = z["barkod"]
                if ime not in po_barkodu[bk] or z["cena"] < po_barkodu[bk][ime]:
                    po_barkodu[bk][ime] = z["cena"]
                    po_barkodu[bk]["_prov_" + ime] = EAN_DIRECT
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

    # --- DIS: sveze cene sa API-ja + barkod preko mosta iz CSV-a ---
    if dis_csv_redovi:
        try:
            most = napravi_most_naziv_barkod(dis_csv_redovi)
            dis_api = preuzmi_dis_api()

            pogodaka = 0
            promasaji_primeri = []
            for a in dis_api:
                kljuc = (a["sifra"] or "").lstrip("0")
                nadjeno = most.get(kljuc)
                if not nadjeno:
                    if len(promasaji_primeri) < 8:
                        promasaji_primeri.append(a["naziv"])
                    continue
                bk, csv_z = nadjeno
                pogodaka += 1
                po_barkodu[bk]["Dis"] = a["cena"]
                po_barkodu[bk]["_prov_Dis"] = CODE_BRIDGE
                if a.get("akcija"):
                    po_barkodu[bk]["_akcija_Dis"] = a["akcija"]
                if "_naziv" not in po_barkodu[bk]:
                    po_barkodu[bk]["_naziv"] = a["naziv"]
                    po_barkodu[bk]["_brend"] = csv_z.get("brend", "")
                    po_barkodu[bk]["_ikona"] = ikona_za(a["kat"], a["naziv"])

            pct = (pogodaka / len(dis_api) * 100) if dis_api else 0
            log(f"[Dis most] POKLAPANJE: {pogodaka}/{len(dis_api)} ({pct:.1f}%)")
            if promasaji_primeri:
                log("[Dis most] primeri promasaja: " + " | ".join(promasaji_primeri))

            if pogodaka > 0:
                statusi.append(
                    f"[OK] Dis (svez API + most): {pogodaka} artikala dobilo svezu cenu "
                    f"({pct:.0f}% poklapanja od {len(dis_api)})"
                )
                lanci_datumi["Dis"] = datetime.now().strftime("%d.%m.%Y.")
            else:
                statusi.append("[GRESKA] Dis API: nijedan naziv se nije poklopio sa CSV-om")
        except Exception as e:
            statusi.append(f"[GRESKA] Dis (API/most): {e}")
            log(f"[Dis API] GRESKA: {e}")

    # --- Univerexport: svez API + barkod preko prefiks-mosta ---
    if univer_csv_redovi:
        try:
            most = napravi_most_prefiks(univer_csv_redovi)
            api = preuzmi_univer_api()
            pogodaka, dvosmislenih = 0, 0
            promasaji = []
            for a in api:
                tok = normalizuj_tokene(a["naziv"])
                kandidati = most.get(kljuc_grupe(tok), [])
                pogodci = [k for k in kandidati if prefiks_slaganje(tok, k[0])]
                barkodovi = {k[1] for k in pogodci}
                if len(barkodovi) == 1:
                    bk, csv_z = pogodci[0][1], pogodci[0][2]
                    pogodaka += 1
                    po_barkodu[bk]["Univerexport"] = a["cena"]
                    po_barkodu[bk]["_prov_Univerexport"] = NAME_MATCH
                    if "_naziv" not in po_barkodu[bk]:
                        po_barkodu[bk]["_naziv"] = csv_z["naziv"]
                        po_barkodu[bk]["_brend"] = csv_z.get("brend", "")
                        po_barkodu[bk]["_ikona"] = ikona_za(csv_z.get("kat", ""), csv_z["naziv"])
                elif len(barkodovi) > 1:
                    dvosmislenih += 1
                elif len(promasaji) < 8:
                    promasaji.append(a["naziv"])
            pct = (pogodaka / len(api) * 100) if api else 0
            log(f"[Univer most] POKLAPANJE: {pogodaka}/{len(api)} ({pct:.1f}%), {dvosmislenih} dvosmislenih")
            if promasaji:
                log("[Univer most] primeri promasaja: " + " | ".join(promasaji))
            if pogodaka > 0:
                statusi.append(f"[OK] Univerexport (svez API + most): {pogodaka} artikala ({pct:.0f}% od {len(api)})")
                lanci_datumi["Univerexport"] = datetime.now().strftime("%d.%m.%Y.")
            else:
                statusi.append("[GRESKA] Univerexport API: nijedan naziv se nije poklopio")
        except Exception as e:
            statusi.append(f"[GRESKA] Univerexport (API/most): {e}")
            log(f"[Univer API] GRESKA: {e}")

    # --- Lidl: svez API + barkod preko EAN sifarnika sa portala ---
    if lidl_urls:
        try:
            most = preuzmi_lidl_sifarnik(lidl_urls) or defaultdict(list)
            # Lidl sifarnik pokriva samo svezu/rinfuznu robu, pa kao
            # drugi izvor koristimo nazive SVIH ostalih lanaca -
            # brendirana roba (Ariel, Fairy...) se tako moze spojiti.
            most_svi_lanci = defaultdict(list)
            grupe_po_svim_recima = True
            for _bk, _pod in po_barkodu.items():
                _naz = _pod.get("_naziv")
                if not _naz:
                    continue
                _tok = normalizuj_tokene(_naz)
                _kol = kolicina_iz(_tok)
                if not _kol:
                    continue
                # indeksiraj pod svakom recju duzom od 3 slova
                for _r in set(t[:4] for t in _tok if len(t) > 3 and not t[0].isdigit()):
                    most_svi_lanci[(_r, _kol)].append((_tok, _bk, _pod))
            log(f"[Lidl most] {len(most_svi_lanci)} grupa iz ostalih lanaca")
            api = preuzmi_lidl_api()
            pogodaka, dvosmislenih = 0, 0
            promasaji = []
            for a in api:
                tok = normalizuj_tokene(a["naziv"])
                g = kljuc_grupe(tok)
                kandidati = most.get(g, [])
                pogodci = [k for k in kandidati if prefiks_slaganje(tok, k[0])]
                if not pogodci:
                    _kol = kolicina_iz(tok)
                    _vidjeni = set()
                    for _r in set(t[:4] for t in tok if len(t) > 3 and not t[0].isdigit()):
                        for k in most_svi_lanci.get((_r, _kol), []):
                            if k[1] in _vidjeni:
                                continue
                            if delimicno_slaganje(tok, k[0], 2) and brend_se_poklapa(a["brend"], k[0]):
                                _vidjeni.add(k[1])
                                pogodci.append(k)
                barkodovi = {k[1] for k in pogodci}
                najbolji = None
                if len(barkodovi) == 1:
                    najbolji = pogodci[0]
                elif len(barkodovi) > 1:
                    # rangiraj po broju zajednickih tokena; uzmi prvog
                    # samo ako je strogo bolji od drugog (bez nagadjanja)
                    rangirani = sorted(pogodci, key=lambda k: -skor_slaganja(tok, k[0]))
                    s1 = skor_slaganja(tok, rangirani[0][0])
                    s2 = skor_slaganja(tok, rangirani[1][0]) if len(rangirani) > 1 else 0
                    if s1 > s2:
                        najbolji = rangirani[0]
                if najbolji is not None:
                    bk = najbolji[1]
                    pogodaka += 1
                    if pogodaka <= 15:
                        log(f"[Lidl spoj] '{a['naziv'][:38]}' == '{' '.join(najbolji[0])[:38]}' (bk={bk})")
                    po_barkodu[bk]["Lidl"] = a["cena"]
                    po_barkodu[bk]["_prov_Lidl"] = NAME_MATCH
                    po_barkodu[bk]["_skor_Lidl"] = skor_slaganja(tok, najbolji[0])
                    if "_naziv" not in po_barkodu[bk]:
                        po_barkodu[bk]["_naziv"] = a["naziv"]
                        po_barkodu[bk]["_brend"] = a["brend"]
                        po_barkodu[bk]["_ikona"] = ikona_za("", a["naziv"])
                elif len(barkodovi) > 1:
                    dvosmislenih += 1
                    if len(promasaji) < 3:
                        log(f"[Lidl neresen] '{a['naziv']}' -> " +
                            " | ".join(" ".join(k[0])[:40] for k in pogodci[:3]))
                elif len(promasaji) < 8:
                    promasaji.append(a["naziv"])
                    # detaljna dijagnostika: sta ima u istoj grupi?
                    _kol = kolicina_iz(tok)
                    _sve = []
                    for _r in set(t[:4] for t in tok if len(t) > 3 and not t[0].isdigit()):
                        for k in most_svi_lanci.get((_r, _kol), [])[:3]:
                            _sve.append(" ".join(k[0])[:45])
                    log(f"[Lidl dbg] '{a['naziv']}' tok={tok} kol={_kol} kandidati={_sve[:4]}")
            pct = (pogodaka / len(api) * 100) if api else 0
            log(f"[Lidl most] POKLAPANJE: {pogodaka}/{len(api)} ({pct:.1f}%), {dvosmislenih} dvosmislenih")
            if promasaji:
                log("[Lidl most] primeri promasaja: " + " | ".join(promasaji))
            if pogodaka > 0:
                statusi.append(f"[OK] Lidl (svez API + sifarnik): {pogodaka} artikala ({pct:.0f}% od {len(api)})")
                lanci_datumi["Lidl"] = datetime.now().strftime("%d.%m.%Y.")
            else:
                statusi.append("[GRESKA] Lidl API: nijedan naziv se nije poklopio sa sifarnikom")
        except Exception as e:
            statusi.append(f"[GRESKA] Lidl (API/sifarnik): {e}")
            log(f"[Lidl API] GRESKA: {e}")

    # --- Idea: webshop API, ima barkodove pa ide direktno ---
    try:
        idea_artikli = preuzmi_idea_api()
        for z in idea_artikli:
            bk = z["barkod"]
            # sveza cena sa webshopa UVEK pregazi staru portal cenu
            if True:
                po_barkodu[bk]["Idea"] = z["cena"]
                po_barkodu[bk]["_prov_Idea"] = EAN_DIRECT
                if z.get("akcija"):
                    po_barkodu[bk]["_akcija_Idea"] = z["akcija"]
            if "_naziv" not in po_barkodu[bk]:
                po_barkodu[bk]["_naziv"] = z["naziv"]
                po_barkodu[bk]["_brend"] = z["brend"]
                po_barkodu[bk]["_ikona"] = ikona_za(z["kat"], z["naziv"])
        if idea_artikli:
            statusi.append(f"[OK] Idea (webshop API): {len(idea_artikli)} artikala sa barkodom")
            lanci_datumi["Idea"] = datetime.now().strftime("%d.%m.%Y.")
        else:
            statusi.append("[GRESKA] Idea API: nijedan artikal")
    except Exception as e:
        statusi.append(f"[GRESKA] Idea (API): {e}")
        log(f"[Idea API] GRESKA: {e}")

    # --- DIJAGNOSTIKA PREKLAPANJA: koliko lanaca deli iste barkodove ---
    from collections import Counter
    po_lancu = Counter()
    parovi = Counter()
    for _bk, _pod in po_barkodu.items():
        lanci = sorted(t for t in _pod if not t.startswith("_"))
        for l in lanci:
            po_lancu[l] += 1
        for a_ in lanci:
            for b_ in lanci:
                if a_ < b_:
                    parovi[(a_, b_)] += 1
    log("[Dijag] barkodova po lancu: " + ", ".join(f"{k}={v}" for k, v in po_lancu.most_common()))
    log("[Dijag] najcesci parovi lanaca:")
    for (a_, b_), n in parovi.most_common(12):
        log(f"[Dijag]   {a_} + {b_}: {n}")
    samo_jedan = sum(1 for _p in po_barkodu.values() if len([t for t in _p if not t.startswith("_")]) == 1)
    log(f"[Dijag] barkodova samo kod 1 lanca: {samo_jedan} od {len(po_barkodu)}")

    # --- obogacivanje naziva/brenda/kolicine po barkodu ---
    enrich = preuzmi_enrichment()
    obogaceno = 0
    for _bk, _pod in po_barkodu.items():
        e = enrich.get(_bk)
        if not e:
            continue
        obogaceno += 1
        _pod["_naziv"] = e["naziv"]
        if e["brend"]:
            _pod["_brend"] = e["brend"]
        if e["kolicina"] > 0 and e["jedinica"]:
            _pod["_kol"] = e["kolicina"]
            _pod["_jm"] = e["jedinica"]
        if _pod.get("_ikona", "🛒") == "🛒":
            _pod["_ikona"] = ikona_za("", e["naziv"])
    if enrich:
        log(f"[Enrich] obogaceno {obogaceno} od {len(po_barkodu)} proizvoda "
            f"({obogaceno/len(po_barkodu)*100:.1f}%)")

    odbaceno_sumnjivih = [0]
    proizvodi = []
    for bk, podaci in po_barkodu.items():
        sirove = []
        for t, c in podaci.items():
            if t.startswith("_"):
                continue
            sirove.append((t, c, podaci.get("_akcija_" + t),
                           podaci.get("_prov_" + t, EAN_DIRECT),
                           podaci.get("_skor_" + t)))

        # Sumnjiv match: cena spojena po NAZIVU koja jako odudara od
        # pouzdanih cena. Dva signala moraju da se poklope - odstupanje
        # cene I slabo poklapanje naziva - da ne bacamo prave akcije.
        pouzdane = [c for _t, c, _a, prov, _sk in sirove if prov != NAME_MATCH]
        prag_dole = prag_gore = None
        if len(pouzdane) >= 2:
            ps = sorted(pouzdane)
            sredina = ps[len(ps) // 2]
            prag_dole, prag_gore = sredina / 2.5, sredina * 2.5

        cene = []
        for t, c, ak, prov, skor in sirove:
            if prov == NAME_MATCH and prag_dole is not None:
                odudara = c < prag_dole or c > prag_gore
                slabo_ime = skor is not None and skor < 3
                if odudara and slabo_ime:
                    odbaceno_sumnjivih[0] += 1
                    continue
            red = [t, c]
            if ak or prov == NAME_MATCH:
                red.append(ak)
                if prov == NAME_MATCH:
                    red.append(1)
            cene.append(red)
        if len(cene) < 2:
            continue
        # Heuristicki match nikad ne "pobedjuje" - priblizne cene idu
        # iza pouzdanih, pa oznaka "najjeftinije" pripada proverenoj.
        cene.sort(key=lambda x: (len(x) > 3, x[1]))
        stavka = [podaci["_naziv"], podaci["_brend"], cene, podaci.get("_ikona", "🛒")]
        # peti element: "X RSD/kg" - iz najjeftinije pouzdane cene
        kol, jm = podaci.get("_kol"), podaci.get("_jm")
        if kol and jm:
            pouzdane_c = [c[1] for c in cene if len(c) <= 3]
            if pouzdane_c:
                po_jm = min(pouzdane_c) / kol
                stavka.append(f"{po_jm:,.0f}".replace(",", ".") + f" RSD/{jm}")
        proizvodi.append(stavka)

    proizvodi.sort(key=lambda x: -len(x[2]))
    if odbaceno_sumnjivih[0]:
        log(f"[Kontrola] odbaceno {odbaceno_sumnjivih[0]} sumnjivih cena "
            f"(match po nazivu + odudara od ostalih)")
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
    detektuj_nove_izvore()

    log("\nStatusi:")
    for s in statusi:
        log("  " + s)


if __name__ == "__main__":
    main()
