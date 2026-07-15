# Projekat: poređenje cena u srpskim marketima (radni naziv: Cenoteka klon → tražimo bolje ime)

## Ideja

Napraviti sajt/app koji poredi cene proizvoda kroz srpske trgovinske lance — kao Cenoteka.rs, ali bolji i sa više funkcija. Cilj: pomoći ljudima da uštede tako što vide gde je koji proizvod najjeftiniji.

## Ključno otkriće: podaci su javni i besplatni

Srbija ima **Uredbu o posebnim uslovima za obavljanje trgovine za određenu vrstu robe** ("Sl. glasnik RS", br. 76/2025 i 78/2025) koja obavezuje **27 trgovinskih lanaca** da nedeljno (ponedeljkom) dostavljaju svoje cenovnike državi. Ti podaci se javno objavljuju na:

- **data.gov.rs** — Nacionalni portal otvorenih podataka (mašinski čitljivi CSV fajlovi po trgovcu)
- **must.gov.rs** — vizuelna platforma "Cenovnici po uredbi" (Ministarstvo unutrašnje i spoljne trgovine), koja je zapravo samo UI nad istim data.gov.rs podacima

Portal data.gov.rs koristi **udata** platformu (ista tehnologija kao francuski data.gouv.fr), što znači da ima standardan REST API:
```
https://data.gov.rs/api/1/datasets/{slug}/
```
Ovaj API vraća JSON sa listom resursa (fajlova), a svaki resurs ima polje `"latest"` — **trajni link koji uvek automatski vodi ka najnovijoj verziji fajla**. Ovo je ključno jer eliminiše potrebu za ručnim ažuriranjem linkova.

### Struktura CSV podataka
Svaki fajl sadrži kolone: KATEGORIJA, NAZIV KATEGORIJE, Naziv proizvoda, Robna marka, **Barkod proizvoda (EAN)**, Jedinica mere, Naziv trgovca-formata, Datum cenovnika, Redovna cena, Cena po jedinici mere, Snižena cena, Datum početka/kraja sniženja, Stopa PDV.

**Barkod (EAN) je ključ za matching** — isti proizvod kod različitih trgovaca ima isti barkod, što omogućava pouzdano poređenje cena "jabuka za jabuku".

### Problemi sa sirovim podacima (bitno za dalji razvoj)
- Fajlovi su OGROMNI — sadrže celu istoriju cena od septembra 2025 (pojedinačni trgovac ide i do ~900MB-980MB)
- Encoding varira po trgovcu (neki šalju UTF-8, neki UTF-16LE sa BOM-om)
- Delimiter varira (zarez ili tačka-zarez)
- Format datuma varira (YYYY-MM-DD, DD.MM.YYYY, DD-MM-YYYY)
- Neki trgovci imaju drugačiju šemu kolona (npr. "bb_trade" nema barkod ni datum cenovnika uopšte — verovatno neupotrebljiv za matching)
- Redovi NISU uvek hronološki sortirani unutar fajla — kod nekih trgovaca (npr. Idea) "kraj fajla" ne znači "najnoviji podaci", kod drugih (npr. Domaća trgovina) znači

## Šta smo napravili (prototip #1: Google Apps Script + Google Drive)

Pošto su fajlovi preveliki da se u celosti skinu u ograničenom sandbox okruženju (Claude-ov alat ima limit 20MB po fetch-u; Google Apps Script ima limit ~50MB odgovora i 6 minuta izvršavanja po pokretanju), napravili smo **Google Apps Script** koji:

1. Radi na **dnevnom trigeru** (svaki dan ~6h ujutru)
2. Za svakog trgovca (27 unetih preko njihovog data.gov.rs "slug"-a) poziva API da dobije trenutni link ka CSV-u
3. Koristi **HTTP Range zahteve** da skine samo mali deo fajla (header sa početka + ~8MB sa kraja), umesto celog fajla
4. Auto-detektuje encoding (na osnovu BOM bajtova) i delimiter
5. Parsira i filtrira samo redove sa **najnovijim datumom cenovnika** iz tog uzorka
6. Snima manji, čist CSV po trgovcu u Google Drive folder
7. Piše log fajl (`_sync_log.txt`) sa statusom svakog trgovca

**Rezultat:** 26 od 27 trgovaca uspešno sync-ovano (samo "bb_trade" pada, jer nema potrebne kolone u svojoj šemi).

### Poznato ograničenje ovog pristupa
Pošto se skida samo poslednjih ~8MB fajla (manje od 1% kod većih trgovaca), a podaci nisu uvek hronološki sortirani, **"najnoviji" datum koji uhvatimo nije uvek stvarno najsvežiji u celom fajlu** — nego najsvežiji u tom malom uzorku. Kod nekih trgovaca (npr. Idea) ovo hvata podatke stare i po nekoliko meseci, kod drugih (npr. Domaća trgovina) hvata podatke od jučer/danas.

**Ovo je glavni razlog zašto razmišljamo o prelasku na GitHub Actions** — tamo nema tih limita (pravi Linux runner, gigabajti memorije, do 6h izvršavanja), pa bi se mogao skinuti CEO fajl i garantovano naći pravi najnoviji datum, bez trikova.

## Dokazan koncept: cross-matching preko barkoda

Skinuli smo sve fajlove iz Google Drive-a (preko Google Drive konektora) i uradili pun cross-match preko barkoda u Python-u (bash sandbox). Rezultati:

- **23 trgovca** uspešno učitano i parsirano (od 26 sync-ovanih; 3 manja trgovca — dexy_co_kids, qvattro, cash_carry_kula — nisu stigla do ovog koraka)
- **467.121** ukupno validnih redova/proizvoda
- **60.354** jedinstvenih barkodova
- **19.863** proizvoda uporedivo kod 2+ trgovca (isti barkod kod više njih)
- **653** proizvoda dostupno kod 14+ trgovaca istovremeno
- **6 proizvoda** dostupno kod **svih 18** relevantnih trgovaca istovremeno

### Konkretni verodostojni primeri (nakon filtriranja očiglednih grešaka u sirovim podacima)
- Pileće grudi mini 330g (Neoplanta): 307,99 RSD (Leon Conditors) – 433,00 RSD (Šumadija market), ~40% razlika
- Šunka stisnjena 330g (Neoplanta): 309,99 – 480,00 RSD
- Plazma stiksi 30g: 64,99 – 99,90 RSD
- Omekšivač Lenor 1,497l: 399,99 – 659,25 RSD

### Napomena o kvalitetu podataka
Neki "rekordi" u razlici cena (i do 10.000%+) u sirovim podacima su OČIGLEDNE greške kod izvora (npr. cena unesena po komadu umesto po celom pakovanju kod pelena), ne stvarna razlika cena. Prava aplikacija bi trebalo da ima logiku za detekciju/filtriranje ovakvih outlier-a pre prikazivanja korisnicima (npr. pravilo da ignoriše redove gde je razlika > neki razuman prag, ili unakrsna provera sa "cena po jedinici mere" kolonom).

## Trenutno stanje

- Google Apps Script (dnevni cron) radi i puni Google Drive folder sa 26 CSV-ova
- Napravljen je i mali vizuelni prototip (HTML kartice) koji prikazuje 4 primera proizvoda sa rangiranim cenama, u stilu Cenoteke
- Sledeći tehnički korak koji smo najavili (nismo još uradili): napraviti pretraživu stranicu koja čita direktno iz podataka i pušta korisnika da ukuca npr. "mleko" i vidi rangirane cene

## Diskusija o arhitekturi (GitHub)

Odlučili smo da pređemo na GitHub jer:
- GitHub Actions nema limite koji su nas mučili kod Apps Script-a (može da skine ceo fajl, ne samo Range trik)
- Omogućava pravi CI/CD: scheduled workflow koji povlači sveže podatke i čuva ih u repo-u ili šalje u bazu
- Prirodna osnova za dalji rast: frontend hostovan na GitHub Pages/Vercel/Netlify, eventualno prava baza (npr. Postgres na Supabase) umesto fajlova, za brzu pretragu kroz stotine hiljada proizvoda i istoriju cena

### Predložena arhitektura
**Kratkoročno:**
1. GitHub repo sa Python skriptom (čistija zamena za Apps Script, bez limita)
2. GitHub Actions scheduled workflow (dnevni cron) koji vuče podatke sa data.gov.rs, radi matching, čuva rezultat kao JSON/CSV u repo-u
3. Statična stranica (GitHub Pages) koja čita taj fajl i nudi pretragu, bez potrebe za serverom

**Dugoročno:**
- Prava baza (npr. Postgres na Supabase, besplatan tier) umesto fajlova, radi brze pretrage i praćenja istorije cena kroz vreme
- Frontend na Vercel/Netlify
- GitHub Actions i dalje puni bazu svežim podacima

## Status: biranje imena

Ne želimo da zovemo projekat "Cenoteka klon" — hoćemo sopstveni brend, ambicija je da bude **bolji** od Cenoteke (više funkcija, ne samo kopija). U toku je brainstorming imena.

Kriterijum koji je definisan: ime treba da nosi vajbu **"voli da uštedi, ali nije cicija/tvrdica"** — pozitivna, ne škrta konotacija.

Predlozi do sad:
- Cenko, CenaSad, PravaCena
- Pazi Cenu, Košarko, Ušteko
- Cenolov, Cenotron
- Ušteda.rs (predlog korisnika)
- **Štediša.rs** (trenutni favorit — "štediša" je pozitivna srpska reč za nekog ko pametno štedi, bez negativne konotacije kao "cicija"; nema poznatog konkurenta sa ovim imenom)
- Fina ušteda, Vredna kupovina, Domaćinski.rs

Sledeći korak: provera dostupnosti `.rs` domena za kandidate, pa finalna odluka imena, pa nastavak GitHub setupa (repo, prvi kod, workflow).
