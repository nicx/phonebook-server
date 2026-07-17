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

`make app` **verweigert den Build, solange die App aus `dist/` läuft** — sonst löscht
er ihr das Bundle unter den Füßen weg, und der Prozess läuft ausgegraut mit altem Code
weiter (beenden geht dann nur noch per `kill`). Erst über die Menüleiste beenden.

**Autostart:** Einstellungen → System → **„Beim Login starten"**. Schreibt einen
LaunchAgent (`~/Library/LaunchAgents/de.nicx.phonebook-server.plist`) — bewusst statt
`SMAppService`, das kommt ohne registrierten Helfer aus und funktioniert auch für ein
ad-hoc-signiertes Eigengebrauch-Bundle. Bewusst **ohne** `KeepAlive`: das würde gegen
„Beenden" im Menü ankämpfen. Aus dem Quelltext heraus geht es nicht — dann fehlt das
Bundle, auf das der Agent zeigen müsste, und die App sagt das auch.

## Konfiguration

Menü → **„Einstellungen…"** öffnet ein natives Fenster mit allen Feldern auf einen
Blick (Quelle, Server, Fehler-E-Mail) inklusive Passwort. Änderungen greifen
**sofort** — nur `bind`, `port`, `basic_auth_user` und ein neues Passwort lösen einen
gezielten Neustart des Listeners aus, alles andere wirkt im Laufen.

Wer lieber die Datei editiert, kann das weiterhin: sie wird per mtime beobachtet und
automatisch neu geladen, ein Neustart der App ist auch dafür nicht nötig.

`~/Library/Application Support/phonebook-server/settings.json` (wird beim ersten Start
mit Defaults angelegt):

```json
{
  "accounts": ["Familie"],
  "source_base": "/Volumes/macmini_data/iCloudSync",
  "favorites": ["Oma Evi", "+491701234567"],
  "bind": "0.0.0.0",
  "port": 8081,
  "basic_auth_user": "wp826",

  "notify_enabled": false,
  "notify_to": "",
  "notify_from": "",
  "smtp_host": "localhost",
  "smtp_port": 2525
}
```

- `accounts` — Reihenfolge ist die Priorität beim Dedup (erster Treffer gewinnt).
  Mehrere Accounts werden über **normalisierte Rufnummern** dedupliziert, nicht über
  `contactId`: die ist accountlokal, dieselbe Person hat je Account eine andere ID.
- `bind` — `0.0.0.0`, damit das WLAN-Telefon drankommt. Loopback wäre nutzlos.

**Passwort** im Einstellungsfenster. Es landet im Schlüsselbund (Service
`phonebook-server`), nie in `settings.json`. Leer lassen = unverändert; beim ersten
Mal steht schon ein Vorschlag drin. Ohne Passwort startet der Server **nicht** — das
Telefonbuch enthält die Kontakte der ganzen Familie.

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

## Spitzname als Anzeigename

**Spitzname ersetzt den Anzeigenamen.** Hat ein Kontakt in iCloud einen Spitznamen,
steht am Telefon nur dieser: aus „Evi Schmitt" wird „Oma Evi". So heißen die Leute im
Haushalt wirklich, und genau dafür ist das Feld gepflegt. Die Grandstream-Spec kennt
kein Spitznamensfeld, also muss er in `FirstName`; `LastName` bleibt leer.

> Der API-Schlüssel ist **`nickName`**, nicht `nickname`. An genau dieser Stelle
> verliert `icloud-sync` den Spitznamen in seiner vCard — ein weiterer Grund, das
> JSON zu lesen und nicht die vCard.

## Favoriten sind eine Geräte-Funktion

**Nicht über das XML setzbar.** Der WP8x6 User Guide beschreibt „Favorite" ausschließlich
als Teil von **Quick Transfer**: eine Liste, aus der man im Gespräch per Quick-Access-Taste
weiterverbindet.

- Markieren am Telefon: **Contacts → Options → „Add to Favorite"** — und zwar pro
  **Rufnummer**, nicht pro Kontakt.
- Taste belegen: Web UI → **Application → Quick Access → Quick Access Key Long Press →
  Favorite**.
- Die Liste ist **unsichtbar, solange sie leer ist**. Für die Block List steht das wörtlich
  im User Guide („at least one contact must be blocked for this list to be visible"); die
  Favorite-Liste verhält sich genauso. Wer sie sucht, bevor der erste Favorit existiert,
  findet sie nicht.

`<Frequent>1</Frequent>` **erzeugt keine Favoriten** — am Gerät geprüft: sieben Kontakte
trugen das Flag, in der Liste stand nur der von Hand markierte. Das Feld stammt aus dem
WP820-Guide, dessen Contact-Tabelle auf den **GXV3275** verweist; im ganzen WP8x6 User
Guide kommt `frequent` im Kontakt-Zusammenhang nicht vor. Ein Erbstück.

### Warum Favoriten trotzdem verschwinden können

**Ein stabiles XML ist die Voraussetzung.** Ändert sich der Datensatz eines Kontakts,
legt das Telefon ihn beim nächsten Download neu an — und die Markierung ist weg.

Genau das war die Ursache, als die Favoriten hier erstmals verschwanden: nicht die
Einstellung `Remove Manually-edited Entries on Download` (die ist entlastet — ein Favorit
überlebte zwei Syncs mit `Yes`), sondern ein Umbau des Konverters von `Cell` auf `Mobile`,
der **jeden** Kontakt neu schrieb.

Daraus die wichtigste Betriebsregel dieses Projekts:

> **Das XML-Format nicht mehr ohne Not ändern.** Es ist gegen das Gerät verifiziert. Jede
> Formatänderung schreibt alle Kontakte neu und kostet sämtliche Favoriten.

## Was das WP826 wirklich tut

Maßgeblich ist **nicht** die Doku, sondern das Gerät. Ermittelt, indem ein Export des
eigenen Telefonbuchs (Web GUI → Phone Book → **„Download XML Phonebook"**) gegen das
gesendete XML gehalten wurde. Das ist die verlässlichste Referenz überhaupt — und sie
widerlegt beide Papierquellen:

| gesendet | WP826 speichert |
|---|---|
| `Mobile` | `Cell` — akzeptiert, umbenannt |
| `Home` | `Home` |
| `Work` | `Work` |
| `Fax` | **`Work`** — kein eigener Slot |
| `Other` | **`Work`** — kein eigener Slot, und bei belegtem `Work`: **kommentarlos verworfen** |

Das Gerät hat also **drei Slots**, nicht fünf. Der WP820 XML Phonebook Guide nennt
zwar `Work/Home/Mobile/Fax/Other`, aber seine Contact-Tabelle verweist auf den
**GXV3275** — er ist streckenweise aus der GXV-Doku kopiert, und Fax/Other sind dort
Erbstücke.

Daraus die wichtigste Regel: **niemals `Fax` oder `Other` senden.** Sonst überlässt man
dem Gerät die Slot-Vergabe, und das verwirft bei Kollision still. Zwei echte Nummern
gingen genau so verloren, während der Report „verlustfrei" meldete. Alle Verteilung
passiert deshalb hier, wo sie sichtbar ist.

Weitere am Gerät gemessene Fakten:

- **`accountindex` = `0`** (Spec: „0 to 5 for account 1 to account 6"). Das
  FusionPBX-Template schreibt `1` und zeigt damit auf ein zweites SIP-Konto.
- **Namensfelder werden bei 18 Zeichen gekappt** — pro Feld. Wer einen Nachnamen hat,
  bekommt also 2×18 und die Anzeige scrollt. Steht der ganze Name im Vornamen (typisch
  bei Firmen), sind bei 18 Schluss. Die Spec nennt gar keine Länge, das
  FusionPBX-Template 24 — beides falsch.
- **`<Company>` wird angezeigt**, obwohl der WP820-Guide es in der Contact-Spec gar
  nicht führt (nur FirstName/LastName/Primary/Frequent/Ringtone/Phone/Group). Der
  Guide ist dort unvollständig, nicht der Code.
- **`<Frequent>1</Frequent>` wird gespeichert, aber nicht benutzt** — der Export zeigt
  das Flag auf genau den konfigurierten Kontakten, die Favorite-Liste ignoriert es.
  Speichern heißt hier nicht auswerten.

## Rufnummern-Verteilung

iCloud kennt beliebig viele Nummern mit freien Labels, das Gerät drei Slots. Der
Konverter ist trotzdem **verlustfrei**:

1. Jede Nummer in ihren Wunsch-Slot: `MOBILE`/`IPHONE`/`Mobil`/`WhatsApp` → Mobile,
   `HOME`/`Homeoffice` → Home, `WORK`/`MAIN`/`OTHER`/`PAGER` und alles mit „FAX" →
   Work, **kein Label** → Mobile.
2. Wer verdrängt wird, rutscht in einen freien Slot. Der Typ ist dann ungenau, aber
   die Nummer wählbar — ein Label ist Kosmetik, eine fehlende Nummer nicht.
3. Passt dann noch immer nicht alles (mehr als drei Nummern), entsteht ein zweiter
   Eintrag „Name (2)".

Faxnummern gehen nicht verloren, erscheinen am Telefon aber als `Work` — das ist die
Realität des Geräts, nicht unsere Wahl.

Was das bei den echten Daten kostet:

```sh
make report
```

Der Bericht zeigt Label-Verteilung, unbekannte Labels, Nummern mit ungenauem Typ,
Zusatzeinträge, zu lange Namen — und ob unterm Strich etwas verloren geht. **Enthält
echte Namen und Rufnummern**, deshalb nur auf den Bildschirm, nie ins Repo.

Zu lange Namen werden **gemeldet, nicht automatisch aufgeteilt**: iCloud bleibt Quelle
der Wahrheit. Wen die Kürzung stört, kürzt dort — oder gibt dem Kontakt einen
Nachnamen, dann sind es 2×18.

## Fehler-E-Mail

Versand über das lokale [MailRelay](https://github.com/nicx/mailrelay) per einfachem
SMTP (kein Auth/TLS auf diesem Hop — das macht das Relay). `notify_to` setzen und
`notify_enabled` auf `true`; Menü → **„Test-E-Mail senden"** prüft den Weg sofort.

Relay-Default ist **`localhost`**, bewusst nicht `127.0.0.1`: macOS löst `localhost`
zuerst nach `::1` auf und umgeht damit die Eigenheit des MailRelay-Bundles, reines
IPv4-Loopback nur sporadisch anzunehmen — behält aber den IPv4-Fallback.

Gemailt wird **nur bei einem Zustandswechsel** (Debounce-State-Machine, portiert aus
`evcc/src/notifier_state.py`): eine Mail gesund→Problem, eine Mail Problem→gesund.
Eine anhaltende Störung schweigt, sonst käme bei jedem Telefon-Poll eine Mail.

| Bedingung | Auslöser |
|---|---|
| `source_broken` | SMB-Mount weg, 0 Kontaktdateien, Lese-/Schreibfehler |
| `server_down` | Port belegt oder kein Passwort im Schlüsselbund → Telefon bekommt „Connection refused" |
| *(One-Shot)* | Beim Start lag noch der Marker des Vorlaufs → letzter Lauf endete unsauber |
| *(One-Shot)* | Kontakte, deren Name das Gerät kappt — siehe unten |

Die Namens-Mail läuft **nicht** über die Ja-Nein-Debounce, sondern dedupliziert über
den **Inhalt**: sie kommt, sobald sich die Liste ändert. Sonst käme sie genau einmal
und ein später hinzugekommener langer Name bliebe unbemerkt, weil die Bedingung schon
auf „Problem" steht. Dasselbe Prinzip wie bei icloud-syncs Fehler-Mails („nur bei
neuem/geändertem Problem"). Der Zustand liegt in `notified.json` — ohne Persistenz
käme nach jedem App-Neustart dieselbe Mail, und die App wird nach jedem Rebuild neu
gestartet. Sind alle Namen wieder darstellbar, gibt es eine Entwarnung.

### Was das *nicht* abdeckt

**Eine tote App kann sich nicht selbst melden.** Bleibt der Prozess dauerhaft weg,
kommt keine Mail — es gibt niemanden, der ihn startet.

Das teilt phonebook-server mit allen Menüleisten-Apps hier: `home-assistant`, `evcc`,
`matter-server` und `esphome` sind **Supervisor + Kindprozess** und überwachen ihr
Kind, nicht sich selbst. Der Unterschied: phonebook-server *hat* kein Kind — es ist
der Server. Deshalb decken die In-Process-Bedingungen hier genau die Fälle ab, in
denen die App lebt, der Dienst aber nicht.

Der Absturz-Marker schließt die Lücke nur halb: er meldet beim **nächsten** Start,
dass der Vorlauf unsauber endete. Wer eine Mail will, *während* die App tot ist,
braucht einen Beobachter von außen (z. B. einen REST-Sensor in Home Assistant auf
`http://<mac>:8081/phonebook.xml`).

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
