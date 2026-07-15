# cena-app

Poređenje cena u srpskim marketima preko javno dostupnih podataka. Radni naziv — pravo ime i brend se još biraju.

## Ideja

Kao Cenoteka.rs, ali bolji. Cilj: pomoći ljudima da vide gde je koji proizvod najjeftiniji, na osnovu zvaničnih podataka koje su trgovinski lanci u obavezi da dostavljaju državi.

## Izvor podataka

Srbija ima Uredbu koja obavezuje 27 trgovinskih lanaca da nedeljno dostavljaju cenovnike. Podaci su javno dostupni preko:
- [data.gov.rs](https://data.gov.rs) — API-based pristup (`/api/1/datasets/{slug}/`), CSV fajlovi po trgovcu
- Matching proizvoda preko EAN barkoda

Detaljan opis pipeline-a, poznatih problema i arhitekture: videti `docs/projekat-rezime.md`.

## Struktura repo-a

```
app/                    -- frontend (trenutno: statični MVP, search + rangirane cene)
data-pipeline/           -- skripte za povlačenje i obradu podataka
docs/                    -- rezime projekta, beleške
```

## Trenutni status

- ✅ Dokazan koncept: cross-matching preko barkoda radi (23 trgovca, 460k+ proizvoda, 19.8k uporedivih)
- ✅ Mini MVP: statična HTML stranica sa pretragom (`app/uporedi-cene-mvp.html`), radi lokalno bez servera
- ✅ Data pipeline prototip: Google Apps Script koji dnevno povlači podatke u Google Drive (`data-pipeline/cenoteka_sync.gs`)
- ⏳ Sledeće: dorada UI-ja, razmatranje prelaska na GitHub Actions za pipeline (bez limita koje ima Apps Script)

## Kako pokrenuti MVP lokalno

Samo otvori `app/uporedi-cene-mvp.html` u browseru — nema instalacije, nema servera, podaci su ugrađeni u fajl.
