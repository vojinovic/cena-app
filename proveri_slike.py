#!/usr/bin/env python3
"""
Provera koliko naših proizvoda ima sliku na Open Food Facts.

Cita mvp_data_veliki.json (isti fajl koji koristi MVP stranica), izvlaci
sve barkodove... cekaj, taj fajl NEMA barkodove (namerno smo ih izbacili
da smanjimo velicinu za web). Zato ovaj skript radi direktno nad
svi_podaci.pkl (ili bilo kojim CSV-om sa kolonom "Barkod proizvoda").

Kako radi:
1. Cita listu barkodova (iz CSV-a ili liste koju sam zadas)
2. Za svaki zove Open Food Facts API (besplatan, bez key-a potreban)
3. Belezi da li postoji slika, i cuva URL ako postoji
4. Postuje rate limit (OFF trazi max ~100 zahteva/min za spor pristup,
   budi pristojan - ovaj skript ide na ~1 zahtev/sekundi)
5. Na kraju ispisuje statistiku i cuva rezultate u CSV

Pokretanje:
    pip install requests
    python proveri_slike.py cene-svih-trgovaca.csv

Ili sa manjim uzorkom za brzi test:
    python proveri_slike.py cene-svih-trgovaca.csv --limit 200
"""

import argparse
import csv
import json
import sys
import time

import requests

API_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
USER_AGENT = "UporediCene-MVP/1.0 (test pokrivenosti slika)"


def proveri_barkod(barkod):
    """Vraca (ima_sliku: bool, url_slike: str|None, naziv_u_off: str|None)"""
    try:
        resp = requests.get(
            API_URL.format(barcode=barkod),
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        if resp.status_code != 200:
            return False, None, None
        data = resp.json()
        if data.get("status") != 1:
            return False, None, None
        product = data.get("product", {})
        slika = product.get("image_front_url") or product.get("image_url")
        naziv = product.get("product_name")
        return bool(slika), slika, naziv
    except requests.exceptions.RequestException:
        return False, None, None


def ucitaj_barkodove_iz_csv(putanja, limit=None):
    barkodovi = []
    with open(putanja, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # trazimo kolonu barkoda, ime moze malo da varira
        col = None
        for kandidat in ["Barkod proizvoda", "barkod", "Bar kod proizvoda"]:
            if kandidat in reader.fieldnames:
                col = kandidat
                break
        if not col:
            print(f"Ne nalazim kolonu barkoda. Dostupne kolone: {reader.fieldnames}")
            sys.exit(1)

        vidjeni = set()
        for row in reader:
            bk = (row.get(col) or "").strip()
            if not bk or bk in vidjeni or bk.startswith("0000") or len(bk) < 12:
                continue
            vidjeni.add(bk)
            barkodovi.append((bk, row.get("Naziv proizvoda", "")))
            if limit and len(barkodovi) >= limit:
                break
    return barkodovi


def main():
    parser = argparse.ArgumentParser(description="Proveri pokrivenost slika na Open Food Facts")
    parser.add_argument("csv_fajl", help="CSV fajl sa kolonom barkoda (npr. neki od *_najnovije.csv fajlova)")
    parser.add_argument("--limit", type=int, default=None, help="Ogranici broj provera (za brzi test)")
    parser.add_argument("--izlaz", default="rezultati_slike.csv", help="Gde da sacuva rezultate")
    args = parser.parse_args()

    barkodovi = ucitaj_barkodove_iz_csv(args.csv_fajl, args.limit)
    print(f"Proveravam {len(barkodovi)} barkodova...\n")

    rezultati = []
    pogodaka = 0

    for i, (bk, naziv) in enumerate(barkodovi, 1):
        ima_sliku, url, off_naziv = proveri_barkod(bk)
        if ima_sliku:
            pogodaka += 1
        rezultati.append({
            "barkod": bk,
            "naziv_kod_nas": naziv,
            "ima_sliku": ima_sliku,
            "url_slike": url or "",
            "naziv_na_off": off_naziv or "",
        })

        if i % 20 == 0 or i == len(barkodovi):
            pct = pogodaka / i * 100
            print(f"  {i}/{len(barkodovi)} provereno — {pogodaka} pogodaka ({pct:.1f}%)")

        time.sleep(1.0)  # pristojan rate limit prema OFF serverima

    with open(args.izlaz, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["barkod", "naziv_kod_nas", "ima_sliku", "url_slike", "naziv_na_off"])
        w.writeheader()
        w.writerows(rezultati)

    print(f"\n{'='*50}")
    print(f"UKUPNO: {pogodaka}/{len(barkodovi)} ima sliku ({pogodaka/len(barkodovi)*100:.1f}%)")
    print(f"Rezultati sacuvani u: {args.izlaz}")


if __name__ == "__main__":
    main()
