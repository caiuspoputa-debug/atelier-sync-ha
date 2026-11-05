# Atelier Sync pentru Home Assistant OS

Serviciu privat de sincronizare pentru agenda Atelier. Tableta și telefonul păstrează fiecare datele local și pot lucra fără internet. Când ajung la acest serviciu, trimit operațiile locale și primesc agenda comună. După prima inițializare de pe tabletă, telefonul poate sincroniza independent; tableta nu trebuie să rămână pornită sau cu aplicația deschisă.

Home Assistant este gazda serviciului. Datele Atelier sunt în propria bază SQLite, `/data/atelier-sync.sqlite3`, nu în baza Home Assistant. Aplicația nu cere acces administrativ la Home Assistant, Supervisor sau dispozitivele casei.

## Ștergerea clienților și actualizare la 0.3.0

Pentru funcția **Șterge clientul**, actualizează întâi Atelier Sync la **0.3.0**, apoi aplicația Atelier pe toate dispozitivele. Fă înainte un backup. Baza SQLite existentă, asocierile, cheia, portul, programările și rezultatele operațiilor se păstrează; nu este necesară reinițializarea serverului.

Un client fără referințe în programări sau în istoricul corecțiilor poate fi eliminat complet. Pentru un client referit de istoric se păstrează identificatorul și numele, cu `deleted:true` și `archived:true`, iar telefonul și observațiile de contact devin goale. Programările, numele afișat în istoric, serviciile, sumele și corecțiile lor nu sunt rescrise sau șterse.

Serverul nu permite reactivarea acestei identități prin dispariția câmpului `deleted`, crearea unei programări noi pe ea sau reasignarea altei programări către ea. Programările deja existente pentru același client pot fi în continuare finalizate, anulate și corectate după regulile obișnuite de audit. Restaurarea inițială a unui backup valid care conține clienți șterși și programările lor rămâne acceptată.

Versiunea nouă publică `client-deletion-v1` împreună cu `suppliers-v1`. Aplicațiile cu funcția de ștergere verifică aceste capabilități înainte de a trimite sau accepta date. Un serviciu HA mai vechi elimină câmpurile de client necunoscute și trebuie actualizat înaintea folosirii funcției. Actualizează și celelalte telefoane/tablete: aplicațiile vechi nu înțeleg ascunderea identităților șterse, iar editările lor pot produce conflicte sau refuzuri păstrând datele pe dispozitiv.

## Furnizori și actualizare la 0.2.0

Pentru furnizorii cheltuielilor, actualizează aplicația Home Assistant **Atelier Sync la 0.2.0** și aplicația Android **Atelier la 0.3.2 sau mai nouă** pe tabletă și telefon. Actualizează întâi serviciul HA. Opțiunile de configurare, cheia de asociere și portul rămân aceleași; baza existentă, asocierile și operațiile confirmate sunt păstrate. Fă înainte un backup și păstrează directorul `/data` al aplicației.

Furnizorii sunt salvați într-un catalog comun. Fiecare cheltuială păstrează numele furnizorului de la înregistrarea ei, chiar dacă denumirea din catalog se schimbă ulterior. Cheltuielile vechi rămân valide fără furnizor. Un furnizor folosit de cheltuieli nu poate fi șters fără rezolvarea acelor referințe în aceeași operație.

Versiunea Android nouă verifică suportul `suppliers-v1` înainte de conectare și sincronizare. Dacă serviciul HA este vechi, cere actualizarea lui și păstrează datele și modificările locale nesincronizate. Versiunile Android vechi pot folosi în continuare înregistrări fără furnizor; editarea unei cheltuieli care conține deja furnizor poate produce un conflict. Actualizează ambele dispozitive înainte de folosirea acestei funcții.

## Instalare locală în Home Assistant OS

1. Copiază acest director, inclusiv `config.yaml`, `Dockerfile`, `server.py` și `validation.py`, în directorul local `/addons/atelier_sync/` al sistemului Home Assistant OS.
2. Reîncarcă lista de aplicații locale din magazinul Apps și instalează **Atelier Sync**. Imaginea se construiește local din imaginea oficială Python, pentru `amd64` sau `aarch64`.
3. În configurația aplicației, completează `pairing_key` cu o cheie privată de minimum 16 caractere; recomandat, o valoare aleatorie de cel puțin 32 de caractere. Nu folosi cheia de acces administrativ Home Assistant. Cheia implicită este goală, iar serviciul refuză să pornească până când este configurată.
4. Poți configura `backup_interval_hours` între 1 și 24 (implicit 6) și `backup_retention_days` între 1 și 90 (implicit 14). Pornește aplicația. Portul implicit este `8787`, editabil în setările de rețea ale aplicației. Păstrează pornirea automată activă. Butonul de interfață web deschide numai pagina publică de stare.
5. În Atelier, configurează adresa sistemului Home Assistant, de exemplu `http://192.168.0.20:8787`. Acesta este un exemplu: folosește IP-ul real al sistemului tău.
6. Asociază întâi tableta cu rolul principal și inițializează serverul din agenda ei. Păstrează înainte un export local al datelor. Apoi asociază telefonul cu aceeași cheie și aceeași adresă.

Instalarea efectivă pe Home Assistant și construirea imaginii necesită acces la sistemul respectiv; testele sursei nu înlocuiesc această verificare.

## Acces din afara rețelei locale

Folosește o adresă VPN care ajunge la portul serviciului, de exemplu un IP Tailscale accesibil, sau un reverse proxy HTTPS configurat corespunzător. O adresă alternativă poate fi introdusă în Atelier. Verifică accesibilitatea de pe telefon înainte de a pleca din rețeaua locală.

**Nu redirecționa direct portul 8787 către internet.** HTTP-ul local nu criptează cheia și tokenurile pe rețea. Pentru conexiuni din afara unei rețele private de încredere folosește VPN sau HTTPS. Serviciul nu expune interfață de administrare și nu folosește ingress Home Assistant.

## Cum sunt protejate modificările

- Fiecare operație are un ID stabil. Retrimiterea după o întrerupere nu dublează programarea sau cheltuiala.
- Fiecare schimbare include valoarea de dinainte. Dacă aceasta diferă de cea de pe server, operația devine conflict; datele de pe telefon nu sunt suprascrise automat.
- Toate schimbările unei operații sunt validate și salvate într-o singură tranzacție, împreună cu rezultatul operației.
- Programările cu identificatori diferiți sunt tot verificate pentru suprapuneri. Intervalele lipite cap la cap sunt permise; cele anulate nu ocupă timpul.
- Corecțiile programărilor finalizate păstrează istoricul existent și valorile exacte de dinainte și de după. Ștergerile explicite sunt verificate prin aceeași comparație cu valoarea anterioară.
- Programările introduse corect offline pot fi sincronizate și după trecerea orei lor. Serverul nu aplică regula interfeței care împiedică introducerea unei programări noi în trecut.
- Lucrul pe două dispozitive fără conexiune poate produce conflicte chiar dacă există un singur operator. Verifică indicatorul de sincronizare înainte de a alterna dispozitivele.

Serverul este coordonatorul agendei comune. Rolul principal permite doar prima inițializare; nu îi oferă tabletei dreptul de a elimina automat modificările telefonului. Pot exista mai multe dispozitive cu rol principal. Schimbarea rolului sau numelui necesită cheia de asociere.

## Backup și recuperare

Serviciul creează la pornire și apoi la intervalul configurat o copie coerentă folosind API-ul SQLite de backup, în `/data/backups/`. Implicit rulează **la șase ore** și păstrează copiile din **ultimele 14 zile**; copia zilei curente este actualizată la fiecare rulare. Data și perioada de păstrare folosesc UTC. Backupul include agenda, identificatorul spațiului, asocierile dispozitivelor și rezultatele operațiilor.

Include aplicația Atelier Sync și datele ei în backupurile Home Assistant și păstrează periodic o copie în afara dispozitivului. Copiile din același `/data` nu protejează împotriva pierderii întregului suport de stocare.

Pentru recuperare, preferă restaurarea backupului Home Assistant. Pentru utilizare independentă, oprește serviciul și restaurează copia SQLite într-un **director nou**, apoi pornește cu `--database` indicând copia restaurată. Păstrează baza veche pentru verificare. Nu copia doar fișierul principal al unei baze active: tranzacțiile pot fi încă în fișierul WAL.

Restaurarea unei copii mai vechi poate readuce serverul la o revizie anterioară. Clienții trebuie să semnaleze această situație pentru recuperare explicită, fără să accepte în tăcere revenirea la date vechi. Exporturile JSON din Atelier rămân utile și separat de backupul serverului.

## Asociere și revocare

Cheia `pairing_key` autorizează asocierea. Fiecare dispozitiv primește propriul token. Jurnalele serviciului nu afișează cheia, tokenurile, cererile sau datele clienților.

**Schimbarea cheii de asociere nu revocă tokenurile deja emise.** Pentru un dispozitiv pierdut, schimbă cheia și revocă asocierea acelui dispozitiv. Revocarea locală poate fi făcută, în mediul serviciului, cu:

```text
python server.py --database /data/atelier-sync.sqlite3 --revoke-device IDENTIFICATOR_DISPOZITIV
```

Cheia configurată trebuie să rămână disponibilă prin `/data/options.json` sau `ATELIER_PAIRING_KEY`. Revocarea nu șterge datele salonului. O asociere nouă după revocare emite un token diferit.

## Rulare independentă și teste

Python 3.11 sau mai nou este suficient; nu există dependențe externe. Configurează `ATELIER_PAIRING_KEY` în mediul procesului și rulează:

```text
python server.py --database ./data/atelier-sync.sqlite3 --host 127.0.0.1 --port 8787
python -m unittest discover -s tests -v
```

Pentru a permite accesul din rețeaua privată, configurează `--host 0.0.0.0` și firewallul local. O copie manuală coerentă poate fi creată cu `--backup CALE_COPIE.sqlite3`. Testele HTTP pornesc temporar un server numai pe `127.0.0.1`.

## Limite ale acestei versiuni

Protocolul trimite agenda completă după sincronizare, cu maximum 50 de operații într-o cerere și maximum 32 MiB pe cerere. Agenda comună este limitată la 30 MiB de JSON canonic UTF-8; inițializările și modificările care ar depăși limita sunt refuzate înainte de salvare, pentru a păstra datele existente accesibile. Rețeaua, sistemul HA și stocarea lui trebuie să fie disponibile pentru confirmarea sincronizării. Dispozitivele continuă să lucreze local când serviciul nu poate fi contactat.

Sursa este verificată prin teste Python și HTTP locale. Imaginea Docker și instalarea pe hardware Home Assistant trebuie verificate în mediul de destinație.

Detaliile formatului de comunicație sunt în `PROTOCOL.md`.

## Logo comun — 0.4.0

Logo-ul personalizat din Setări este inclus în agenda sincronizată și în backup,
în câmpul opțional `settings.workspace.logoData`. Sunt acceptate PNG-uri pregătite
de aplicație, maximum 512 × 512 pixeli și 512 KiB. Valoarea `null` indică revenirea
explicită la logo-ul implicit. Datele vechi păstrează câmpul absent.

Pentru transmiterea unui logo nou sau a revenirii la cel implicit, actualizează
Atelier Sync la **0.4.0** și aplicația pe dispozitivele folosite. Capabilitatea
`salon-logo-v1` permite aplicației să verifice compatibilitatea. Funcțiile de
sincronizare obișnuite rămân compatibile cu 0.3.0 până când se folosește logo-ul.
Setările trimise de un dispozitiv vechi nu pot șterge implicit logo-ul deja salvat:
serverul răspunde cu conflict sau refuz atomic, fără să piardă datele.

Actualizarea add-on-ului păstrează baza SQLite, tokenurile și copiile existente.
