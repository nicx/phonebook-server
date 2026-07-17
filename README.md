# phonebook-server

Native macOS-Menüleisten-App, die lokal gespiegelte iCloud-Kontakte als
XML-Telefonbuch für IP-Telefone ausliefert. Entwickelt für ein **Grandstream WP826**,
das Format ist aber bei allen Grandstream-Geräten dasselbe.

**Kein Sync.** Das Telefon pollt selbst (Grandstream-Parameter `P332`), der Fluss ist
strikt einseitig. Diese App spricht **nie mit iCloud** — sie liest nur Dateien, die
[icloud-sync](https://github.com/nicx/icloud-sync) ohnehin schon auf die Platte spiegelt.

```
iCloud ──icloud-sync (5×/Tag)──▶ /Volumes/…/iCloudSync/Familie/Contacts/*.json
                                                   │
                                    phonebook-server│ baut bei Bedarf, cached lokal
                                                   ▼
              WP826 ──HTTP GET alle 240 min──▶ 192.168.2.1:8081/phonebook.xml
```

## Datenquelle: Vertrag mit icloud-sync

Gelesen werden die **`.json`**-Dateien unter `<source_base>/<Account>/Contacts/`, nicht
die daneben liegenden `.vcf`. Die vCard ist bereits eine verlustbehaftete Ableitung
(nicht-standardkonforme TYPEs wie `IPHONE`, `nickName` fehlt ganz); das JSON ist die
rohe Antwort der iCloud-API.

Dieses Layout ist damit ein **Vertrag zwischen zwei Repos**. Ändert icloud-sync die
Verzeichnisstruktur oder das JSON-Format, bricht diese App.

## Installation

```sh
make venv        # .venv + rumps
make test        # 97 Tests, kein Netz, keine echten Daten
make app         # -> dist/PhonebookServer.app
```

Die gebaute App nach `/Applications` kopieren und **per Doppelklick** starten.

> **Nicht aus dem Terminal starten** (kein `open -a`, kein direkter Aufruf). Eine so
> gestartete Instanz läuft headless weiter, belegt Port 8081 und ist ohne Menüleisten-
> Symbol kaum wieder loszuwerden.

Autostart: `launchagent/de.nicx.phonebook-server.plist` nach `~/Library/LaunchAgents/`
kopieren und laden (siehe Kommentar in der Datei).

## Konfiguration

`~/Library/Application Support/phonebook-server/settings.json` (wird beim ersten Start
mit Defaults angelegt):

```json
{
  "accounts": ["Familie"],
  "source_base": "/Volumes/macmini_data/iCloudSync",
  "bind": "0.0.0.0",
  "port": 8081,
  "basic_auth_user": "wp826"
}
```

- `accounts` — Reihenfolge ist die Priorität beim Dedup (erster Treffer gewinnt).
  Mehrere Accounts werden über **normalisierte Rufnummern** dedupliziert, nicht über
  `contactId`: die ist accountlokal, dieselbe Person hat je Account eine andere ID.
- `bind` — `0.0.0.0`, damit das WLAN-Telefon drankommt. Loopback wäre nutzlos.

**Passwort** über das Menü → „Passwort setzen…". Es landet im Schlüsselbund
(Service `phonebook-server`), nie in `settings.json`. Ohne Passwort startet der Server
**nicht** — das Telefonbuch enthält die Kontakte der ganzen Familie.

## WP826 einrichten

Web GUI → **Phone Book → Phone Book Management**:

| Einstellung | Wert | P-Wert |
|---|---|---|
| Enable Phonebook XML Download | HTTP | `P330=1` |
| Phonebook XML Server Path | `192.168.2.1:8081` | `P331` |
| HTTP/HTTPS User Name / Password | wie konfiguriert | — |
| Phonebook Download Interval | `240` | `P332` |
| Remove Manually-edited entries on Download | Yes | `P333=1` |

- Der Server Path steht **ohne Dateinamen** — das Telefon hängt `phonebook.xml` selbst an.
- `P332` erlaubt `0` (aus) oder `5`–`720` Minuten. 240 ist ein Kompromiss: icloud-sync
  läuft ohnehin nur 5×/Tag, und das WP826 ist ein Akku-Gerät.
- `P333=Yes` macht iCloud zur alleinigen Quelle der Wahrheit.
- Kapazität des Geräts: 1000 Einträge.

Prüfen:

```sh
curl -u wp826:… http://192.168.2.1:8081/phonebook.xml | xmllint --format -
```

**Aus dem LAN testen, nicht nur über `127.0.0.1`** — ein py2app-Bundle kann sich beim
Loopback anders verhalten als im Netz (bei MailRelay ist genau das aufgefallen).

## Das Drei-Slot-Problem

Das WP826 kennt je Kontakt nur **Work/Home/Cell**. iCloud kennt beliebig viele Nummern
mit freien Labels. Der Konverter arbeitet deshalb in Stufen und ist dabei **verlustfrei**:

1. Jede Nummer in ihren Wunsch-Slot (`MOBILE`/`IPHONE`/`Mobil`/`WhatsApp` → Cell,
   `HOME`/`Homeoffice` → Home, `WORK`/`MAIN` → Work, **kein Label** → Cell).
2. Wer verdrängt wird, rutscht in einen freien Slot desselben Eintrags. Der Typ am
   Telefon ist dann ungenau — aber die Nummer ist wählbar. Ein Label ist Kosmetik,
   eine fehlende Nummer nicht.
3. Bleibt dann noch etwas übrig (>3 Nummern), entsteht ein zweiter Eintrag „Name (2)".

Fax- und Pager-Nummern fliegen raus: das Telefon kann sie nicht anrufen, und sie würden
einen Slot belegen.

Was das bei den echten Daten kostet:

```sh
make report
```

Der Bericht zeigt Label-Verteilung, unbekannte Labels, Nummern mit ungenauem Typ und
Zusatzeinträge — und ob unterm Strich etwas verloren geht. **Enthält echte Namen und
Rufnummern**, deshalb nur auf den Bildschirm, nie ins Repo.

## Robustheit

Das Telefon bekommt nie einen 5xx, solange irgendein Cache existiert. Neu gebaut wird
nur, wenn die Quelle **vollständig** lesbar ist:

- Fehlt ein Account-Verzeichnis (SMB-Mount weg, icloud-sync mittendrin), wird **nicht**
  gebaut — sonst entstünde ein kürzeres Telefonbuch, das den guten Cache überschreibt.
- Null Kontaktdateien ist kein Ergebnis, sondern ein Symptom → Cache behalten.

Dasselbe Prinzip wie der Prune-Guard in icloud-sync: ein unvollständiger Blick auf die
Quelle darf gute Daten nicht zerstören.

Erkannt werden Änderungen über die jüngste mtime der Quell-JSONs **und die der
Verzeichnisse**. Letzteres ist nicht optional: icloud-sync ist ein Spiegel und löscht
die JSON eines in iCloud gelöschten Kontakts. Dabei ändert sich keine verbleibende
Datei — nur der Verzeichniseintrag. Ohne die Verzeichnis-mtime bliebe ein gelöschter
Kontakt für immer im Telefonbuch.

## Sicherheit

Basic Auth über unverschlüsseltes HTTP ist Base64 — kein Schutz gegen einen Angreifer
im LAN, aber es hält beiläufiges Stöbern ab. Das Telefon speichert das Passwort ohnehin
im Klartext, und Grandstream kann fürs Telefonbuch **nur Basic, kein Digest**.

TLS ist bewusst nicht vorgesehen: ein LAN-only-Host müsste einer privaten CA vertrauen,
die das Telefon nicht kennt — und ein öffentlich vertrauenswürdiges Zertifikat bräuchte
einen von außen erreichbaren ACME-Challenge-Pfad.

Das Keychain-Passwort wird über das Apple-signierte `/usr/bin/security` gelesen, nicht
in-process. Grund (wie in icloud-sync): in-process bindet macOS „Immer erlauben" an die
Code-Identität, die sich bei jedem self-signed Rebuild ändert — es gäbe nach jedem
Update einen Prompt.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
