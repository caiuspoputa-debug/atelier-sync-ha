# Atelier Sync — add-on pentru Home Assistant OS

Aceeași agendă pe tabletă și telefon, cu salvare locală și sincronizare prin Home Assistant. Telefonul poate trimite programări când tableta este oprită. Necesită aplicația Android Atelier cu protocol de sincronizare 1.

Versiunea **0.2.0** adaugă furnizori pentru cheltuieli. Actualizează add-on-ul și aplicația Android la **0.3.2-test** pe toate dispozitivele; datele existente se păstrează. Aplicația nouă păstrează lucrul offline și cere actualizarea HA înainte să sincronizeze cu un add-on mai vechi.

[Adaugă repository-ul în Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fcaiuspoputa-debug%2Fatelier-sync-ha)

## Instalare

1. În Home Assistant: **Setări → Add-ons / Apps → Magazin → ⋮ → Repositories**.
2. Adaugă `https://github.com/caiuspoputa-debug/atelier-sync-ha`.
3. Instalează **Atelier Sync**. Prima instalare construiește imaginea pe sistemul HA; necesită acces la internet pentru imaginea oficială Python.
4. În **Configuration**, introdu cheia de asociere și alege frecvența și durata păstrării copiilor de siguranță. Cheia are minimum 16 caractere; folosește o valoare privată aleatorie, ideal 32 de caractere. Nu introduce un token administrativ HA.
5. Pornește add-on-ul și activează **Start on boot** și **Watchdog**. Din **Network** poți schimba portul publicat, implicit `8787`.
6. Pe tabletă: alege **Dispozitiv principal**, apoi în Setări activează sincronizarea. Introdu `http://ADRESA_HA:8787` și cheia configurată în add-on. Prima conectare inițializează HA cu agenda tabletei.
7. Pe telefon: alege **Dispozitiv secundar**, configurează aceeași adresă și cheie, apoi confirmă preluarea agendei din HA. Datele vechi ale telefonului sunt păstrate într-o copie locală exportabilă.

Adresa se poate modifica oricând. Un singur dispozitiv principal funcționează fără add-on, server sau telefon asociat. Sincronizarea este opțională.

## Configurația add-on-ului

| Opțiune | Rol | Implicit |
| --- | --- | --- |
| `pairing_key` | Cheia folosită pentru asocierea dispozitivelor | goală; trebuie completată |
| `backup_interval_hours` | Cât de des se actualizează copia zilei, între 1 și 24 ore | 6 |
| `backup_retention_days` | Câte zile se păstrează copiile, între 1 și 90 | 14 |
| Network → port | Portul accesibil de pe telefon și tabletă | 8787 |

Datele sunt în baza proprie a add-on-ului, în `/data`, și pot fi incluse în backupurile Home Assistant. Actualizarea add-on-ului păstrează această stocare. Dezinstalarea add-on-ului și ștergerea datelor lui necesită un backup separat înainte.

## Acces prin Tailscale

În aplicația Android poți introduce și o adresă alternativă `http://100.x.y.z:8787` sau un nume `.ts.net`. Telefonul trebuie să poată ajunge la HA prin Tailscale: fie HA este în aceeași rețea Tailscale, fie există o rută Tailscale către rețeaua locală. Dacă folosești o astfel de rută, poate funcționa direct și adresa locală HA.

Nu publica portul 8787 direct pe internet. Serviciul este destinat rețelei locale sau VPN. Codul public din acest repository nu conține agenda utilizatorului, tokenuri de acces sau chei de asociere.

## Salvare și conflicte

Datele rămân pe fiecare dispozitiv după sincronizare. Modificările offline se trimit la revenirea conexiunii, când aplicația este activă. Fiecare modificare are un identificator stabil; retrimiterea nu creează duplicate. Suprapunerile și editările concurente se rezolvă explicit în aplicație. Sincronizarea nu înlocuiește un backup.

Add-on-ul este o versiune de test. [Documentația detaliată](atelier_sync/DOCS.md) descrie instalarea, securitatea, backupul și limitele. [Protocolul](atelier_sync/PROTOCOL.md) descrie comunicația cu aplicația.

## Verificare

```sh
python3 -m unittest discover -s atelier_sync/tests -v
docker build -t atelier-sync:test atelier_sync
```

Workflow-ul GitHub verifică testele Python, construirea imaginii și pornirea serviciului în container. Instalarea efectivă în Supervisor și testarea APK-ului pe dispozitive se verifică în sistemul de destinație.
