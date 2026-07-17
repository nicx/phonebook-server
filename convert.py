#!/usr/bin/env python3
"""iCloud-Kontakte (Roh-JSON von icloud-sync) -> Telefonbuch-XML für IP-Telefone.

Quelle sind die ``.json``-Dateien, die **icloud-sync** je Kontakt ablegt
(``<source_base>/<Account>/Contacts/<name>_<kurz-id>.json``) — nicht die daneben
liegenden ``.vcf``. Die vCard ist bereits eine verlustbehaftete Ableitung
(nicht-standardkonforme TYPEs wie ``IPHONE``, ``nickName`` fehlt ganz); das JSON ist
die rohe Antwort der iCloud-API und damit die bessere Quelle.

Aufbau in zwei Schritten, damit ein zweites Ausgabeformat (z. B. FRITZ!Box) später
eine Schwesterfunktion ist und kein Umbau:

    load_contacts(...)      -> list[Contact]     # laden + normalisieren, formatneutral
    to_grandstream_xml(...) -> bytes             # rendern, Grandstream-spezifisch

Formatquelle ist der **WP820 XML Phonebook Guide** (nächstes Modell zum WP826), nicht
das FusionPBX-Template: letzteres gilt für die GXP16xx-Serie und weicht in drei
Punkten ab, die auf einem WP-Gerät wehtun — siehe SLOTS, ACCOUNT_INDEX und MAX_NAME.

CLI:
    python3 convert.py --report            # was ginge beim Mapping verloren?
    python3 convert.py -o phonebook.xml    # XML schreiben
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Erlaubte Werte des type-Attributs, wörtlich aus dem WP820 XML Phonebook Guide
# ("Table 8: <Phone> Element" -> "type: Work/Home/Mobile/Fax/Other"). Die
# Reihenfolge ist zugleich die Ausgabereihenfolge im XML.
#
# NICHT "Cell": das steht im FusionPBX-Template, gilt aber für die GXP16xx-Serie.
# Die WP-Reihe kennt "Mobile" — bestätigt durch den Kontakt-Editor des WP8x6
# (Admin Guide: Work/Home/Mobile) und die Online-Contacts-Schlüssel
# (extensionHome/extensionMobile).
SLOTS = ("Work", "Home", "Mobile", "Fax", "Other")

# Ziel-Slots für Nummern, deren Wunsch-Slot belegt ist. "Other" zuerst, weil das
# die ehrlichste Aussage über eine Zweitnummer ist. **Fax fehlt hier bewusst**:
# eine Sprachnummer als Fax auszuweisen wäre aktiv irreführend — man würde sie
# nicht anrufen. Der Fax-Slot bleibt echten Faxnummern vorbehalten.
SPILL_SLOTS = ("Other", "Work", "Home", "Mobile")

# Welches SIP-Konto wählt die Nummer. Spec: "From 0 to 5 for account 1 to
# account 6" — 0 ist also das ERSTE Konto, nicht "keins". Das FusionPBX-Template
# schreibt 1 und würde damit auf ein zweites Konto zeigen, das es hier nicht gibt.
ACCOUNT_INDEX = "0"

# Reine Vernunftgrenzen gegen kaputte Daten, KEINE Spec-Vorgabe: die WP820-Spec
# nennt für Namen nur "String" und für Nummern "Number", ohne Länge. Die 24 aus
# dem FusionPBX-Template ist GXP-Folklore und würde echte Einträge verstümmeln
# ("Robert-Bosch-Gymnasium Sekretariat"). Längster echter Wert: Name 34, Nummer 15.
MAX_NAME = 64
MAX_NUMBER = 32

# Label -> Slot. Die Labels kommen ungefiltert aus iCloud und sind KEIN
# geschlossenes Vokabular: neben den Apple-Vorgaben stehen dort freie
# Nutzer-Eingaben ("Mobil", "WhatsApp", "Homeoffice"), und `label` darf ganz
# fehlen. Alles Unbekannte fällt auf DEFAULT_SLOT zurück (siehe _slot_for).
LABEL_TO_SLOT = {
    "MOBILE": "Mobile",
    "IPHONE": "Mobile",
    "MOBIL": "Mobile",
    "CELL": "Mobile",
    "WHATSAPP": "Mobile",
    "HOME": "Home",
    "HOMEOFFICE": "Home",
    "WORK": "Work",
    "BUSINESS": "Work",
    "MAIN": "Work",
    "OTHER": "Other",
    "PAGER": "Other",
}
# Ohne Label: die klare Mehrheit aller Nummern ist mobil, und Mobile ist der
# Slot, den das Gerät garantiert anzeigt.
DEFAULT_SLOT = "Mobile"

# Fax-Varianten ("WORK FAX", "HOME FAX", "Fax privat", …) auf den Fax-Slot. Sie
# werden NICHT verworfen: "Fax" ist ein gültiger type-Wert, kostet keinen
# Sprach-Slot und bleibt so wenigstens nachschlagbar.
FAX_LABEL_RE = re.compile(r"FAX", re.IGNORECASE)


@dataclass
class Phone:
    number: str
    slot: str
    label: str  # Original-Label aus iCloud, nur für den Report


@dataclass
class Contact:
    first: str = ""
    last: str = ""
    company: str = ""
    phones: list[Phone] = field(default_factory=list)
    account: str = ""  # Herkunfts-Account, nur für den Report
    source: str = ""   # Dateiname, nur für den Report

    @property
    def display(self) -> str:
        """Name für Report/Logs — nie ins XML, dort zählen first/last einzeln."""
        name = " ".join(p for p in (self.first, self.last) if p).strip()
        return name or self.company or "(namenlos)"


# --------------------------------------------------------------- Normalisierung ---

def clean_number(raw: str) -> str:
    """Rufnummer auf Wählbares reduzieren: nur ``+`` (führend) und Ziffern.

    iCloud liefert unnormalisierten Freitext ("+49 (0711) 1234567"). Leerzeichen und
    Klammern bringen die Wählfunktion durcheinander und fressen das 24-Zeichen-Budget.
    Bewusst KEINE Umwandlung nach E.164: nationale Nummern ("0711…") blieben sonst
    nur mit einer Länder-Annahme korrekt, und die wäre bei Auslandskontakten falsch.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    return ("+" if plus else "") + digits


def _slot_for(label: str | None) -> str:
    if not label:
        # 51 von 359 Nummern haben gar kein Label (im Account Max sogar 35 von 41).
        return DEFAULT_SLOT
    norm = label.strip().upper()
    if norm in LABEL_TO_SLOT:
        return LABEL_TO_SLOT[norm]
    if FAX_LABEL_RE.search(norm):  # "WORK FAX", "HOME FAX", …
        return "Fax"
    return DEFAULT_SLOT


def contact_from_icloud(raw: dict) -> Contact:
    """Baut einen Contact aus einem rohen iCloud-Kontakt-Dict.

    Defensiv gegen fehlende Schlüssel: außer `contactId` ist praktisch jedes Feld
    optional. Verworfen wird nichts — nur Einträge ohne wählbare Ziffern.
    """
    c = Contact(
        first=(raw.get("firstName") or "").strip(),
        last=(raw.get("lastName") or "").strip(),
        company=(raw.get("companyName") or "").strip(),
    )
    for ph in raw.get("phones") or []:
        if not isinstance(ph, dict):
            continue
        number = clean_number(ph.get("field") or "")
        if not number:
            continue
        label = (ph.get("label") or "").strip()
        c.phones.append(Phone(number=number, slot=_slot_for(label), label=label or "(ohne Label)"))
    return c


def load_contacts(source_base, accounts) -> list[Contact]:
    """Lädt und normalisiert die Kontakte der genannten Accounts.

    Formatneutral — das Ergebnis taugt für jedes Ausgabeformat. Kontakte ohne
    verwertbare Rufnummer werden verworfen: sie gehören nicht in ein Telefonbuch.

    Fehlt ein Account-Verzeichnis (z. B. SMB-Mount weg), wird das geloggt und der
    Account übersprungen — der Aufrufer entscheidet, ob ein Teilergebnis brauchbar ist
    (der Server tut das über `expected_accounts`, siehe phonebook_server.py).
    """
    base = Path(source_base)
    out: list[Contact] = []
    for account in accounts:
        d = base / account / "Contacts"
        if not d.is_dir():
            LOGGER.warning("Account-Verzeichnis fehlt, übersprungen: %s", d)
            continue
        for f in sorted(d.glob("*.json")):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                LOGGER.warning("Kontakt nicht lesbar, übersprungen: %s (%s)", f.name, exc)
                continue
            if not isinstance(raw, dict):
                continue
            c = contact_from_icloud(raw)
            if not c.phones:
                continue
            c.account = account
            c.source = f.name
            out.append(c)
    return out


def dedupe(contacts: list[Contact]) -> list[Contact]:
    """Entfernt Kontakte, die sich über Accounts hinweg doppeln.

    Schlüssel ist die Menge der Rufnummern, NICHT die `contactId`: die ist
    accountlokal, dieselbe Person hat je Account eine andere ID (in einem Account
    Base64, in den anderen UUIDs). Der erste Treffer gewinnt, die Account-Reihenfolge
    aus der Config ist damit die Priorität.

    No-op bei nur einem Account, aber billig genug, um immer zu laufen.
    """
    seen: set[frozenset[str]] = set()
    out: list[Contact] = []
    for c in contacts:
        key = frozenset(p.number for p in c.phones)
        if key in seen:
            LOGGER.debug("Duplikat übersprungen: %s (%s)", c.display, c.account)
            continue
        seen.add(key)
        out.append(c)
    return out


@dataclass
class Entry:
    """Ein Telefonbuch-Eintrag: bis zu drei Nummern, eine je Slot."""
    slots: dict[str, str] = field(default_factory=dict)
    # Nummern, die nicht in ihren Wunsch-Slot passten: (Phone, tatsächlicher Slot).
    # Nur für den Report — ihr Typ am Telefon ist ungenau.
    spills: list[tuple[Phone, str]] = field(default_factory=list)


def plan_entries(contact: Contact) -> list[Entry]:
    """Verteilt ALLE Rufnummern verlustfrei auf so viele Einträge wie nötig.

    Je Eintrag kennt das Telefon jeden Slot nur einmal (Work/Home/Mobile/Fax/Other).
    Statt Überzähliges wegzuwerfen, in Stufen:

    1. Jede Nummer in ihren Wunsch-Slot, solange der frei ist.
    2. Wer verdrängt wurde, rutscht in einen freien Slot aus SPILL_SLOTS — "Other"
       zuerst. Der Typ am Telefon ist dann ungenau, aber die Nummer ist wählbar,
       und das ist der Zweck eines Telefonbuchs. Ein Label ist Kosmetik, eine
       fehlende Nummer nicht.
    3. Bleibt dann noch etwas übrig, beginnt ein weiterer Eintrag.

    Terminiert immer: die erste Nummer findet stets ihren Slot (e.slots ist leer),
    pro Runde wird also mindestens eine Nummer untergebracht.
    """
    remaining = list(contact.phones)
    entries: list[Entry] = []
    while remaining:
        e = Entry()
        displaced: list[Phone] = []
        for p in remaining:
            if p.slot in e.slots:
                displaced.append(p)
            else:
                e.slots[p.slot] = p.number
        leftover: list[Phone] = []
        for p in displaced:
            free = next((s for s in SPILL_SLOTS if s not in e.slots), None)
            if free is None:
                leftover.append(p)
            else:
                e.slots[free] = p.number
                e.spills.append((p, free))
        entries.append(e)
        remaining = leftover
    return entries


# ------------------------------------------------------------------- Rendering ---

def _sub(parent, tag, text):
    el = ET.SubElement(parent, tag)
    el.text = text
    return el


def to_grandstream_xml(contacts: list[Contact]) -> bytes:
    """Rendert das Grandstream-``AddressBook``-XML.

    Escaping macht ElementTree — von Hand wäre es fahrlässig: Apple-Labels und -Namen
    enthalten real Zeichen wie ``:`` und ``<`` (z. B. der Label
    ``_$!<MaleFriend>!$_X-SHARED-PHOTO-DISPLAY-PREF:IMPLICIT_AUTOUPDATE``).
    """
    root = ET.Element("AddressBook")
    _sub(root, "version", "1")

    for c in contacts:
        entries = plan_entries(c)
        for n, entry in enumerate(entries, start=1):
            for phone, actual in entry.spills:
                LOGGER.info("Nummer in freien Slot verschoben: %s / %s [%s -> %s]",
                            c.display, phone.number, phone.slot, actual)

            el = ET.SubElement(root, "Contact")
            first, last = c.first, c.last
            if not first and not last:
                # Reine Firmenkontakte: der Name muss irgendwo hin, sonst zeigt das
                # Telefon einen leeren Eintrag.
                first = c.company
            first, last = first[:MAX_NAME], last[:MAX_NAME]
            if n > 1:
                # Folge-Eintrag für einen Kontakt mit mehr als drei Nummern.
                # Suffix ans Nachnamensfeld, damit die Einträge beim üblichen
                # Sortieren nebeneinander stehen; vor dem Anhängen kürzen, damit
                # das Suffix nicht selbst der Längengrenze zum Opfer fällt.
                suffix = f" ({n})"
                if last:
                    last = last[:MAX_NAME - len(suffix)] + suffix
                else:
                    first = first[:MAX_NAME - len(suffix)] + suffix
            _sub(el, "FirstName", first)
            _sub(el, "LastName", last)
            if c.company:
                _sub(el, "Company", c.company[:MAX_NAME])
            for slot in SLOTS:  # feste Reihenfolge -> stabile Ausgabe
                if slot not in entry.slots:
                    continue
                ph = ET.SubElement(el, "Phone", {"type": slot})
                _sub(ph, "phonenumber", entry.slots[slot][:MAX_NUMBER])
                _sub(ph, "accountindex", ACCOUNT_INDEX)

    ET.indent(root, space="  ")
    # Deklaration selbst schreiben: ElementTree würde sie mit einfachen Anführungs-
    # zeichen und kleingeschriebenem "utf-8" ausgeben. Beides ist gültiges XML, aber
    # die Grandstream-Doku zeigt durchgängig die Großschreibung — bei einem
    # Embedded-Parser ist das kein Risiko, das sich zu testen lohnt.
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


# ---------------------------------------------------------------------- Report ---

def _is_unknown_label(label: str) -> bool:
    """True, wenn `label` nur über den DEFAULT_SLOT-Fallback landet.

    Muss dieselben Wege kennen wie `_slot_for` — sonst meldet der Report Labels als
    "unbekannt", die sehr wohl sauber gemappt werden (etwa "HOME FAX" -> Fax).
    """
    norm = label.strip().upper()
    return norm not in LABEL_TO_SLOT and not FAX_LABEL_RE.search(norm)


def build_report(contacts: list[Contact]) -> str:
    """Menschenlesbarer Bericht: was kostet das Drei-Slot-Limit, was ist unbekannt?

    Absichtlich nicht ins Repo schreiben — enthält echte Namen und Rufnummern.
    """
    lines: list[str] = []
    label_counts: dict[str, int] = {}
    unknown: dict[str, int] = {}
    spilled: list[tuple[Contact, Phone, str]] = []
    extra: list[tuple[Contact, int]] = []
    n_entries = 0

    for c in contacts:
        entries = plan_entries(c)
        n_entries += len(entries)
        if len(entries) > 1:
            extra.append((c, len(entries)))
        for e in entries:
            for phone, actual in e.spills:
                spilled.append((c, phone, actual))
        for p in c.phones:
            label_counts[p.label] = label_counts.get(p.label, 0) + 1
            if p.label != "(ohne Label)" and _is_unknown_label(p.label):
                unknown[p.label] = unknown.get(p.label, 0) + 1

    n_phones = sum(len(c.phones) for c in contacts)
    lines.append(f"Kontakte mit mindestens einer Rufnummer: {len(contacts)}")
    lines.append(f"Rufnummern gesamt: {n_phones}")
    lines.append(f"Telefonbuch-Einträge im XML: {n_entries}")
    lines.append("")

    lines.append(f"Label-Verteilung ({len(label_counts)} verschiedene):")
    for label, n in sorted(label_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:5d}  {label} -> {_slot_for(None if label == '(ohne Label)' else label)}")
    lines.append("")

    if unknown:
        lines.append(f"Unbekannte Labels (fallen auf {DEFAULT_SLOT} zurück):")
        for label, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:5d}  {label!r}")
    else:
        lines.append("Unbekannte Labels: keine")
    lines.append("")

    if spilled:
        lines.append(f"Nummern mit ungenauem Typ am Telefon (Wunsch-Slot belegt): {len(spilled)}")
        for c, p, actual in spilled:
            lines.append(f"  {c.display} ({c.account}): {p.number} [{p.label}] steht als {actual}")
    else:
        lines.append("Nummern mit ungenauem Typ: keine")
    lines.append("")

    if extra:
        lines.append(f"Kontakte mit Zusatzeintrag (mehr als 3 Nummern): {len(extra)}")
        for c, n in extra:
            lines.append(f"  {c.display} ({c.account}): {len(c.phones)} Nummern -> {n} Einträge")
    else:
        lines.append("Kontakte mit Zusatzeintrag: keine")
    lines.append("")

    # Die eigentliche Kernaussage: geht irgendwo etwas verloren?
    placed = sum(len(e.slots) for c in contacts for e in plan_entries(c))
    lines.append(f"Rufnummern im XML: {placed} von {n_phones} "
                 + ("— verlustfrei" if placed == n_phones else "— ACHTUNG, Verlust!"))

    return "\n".join(lines)


# ------------------------------------------------------------------------- CLI ---

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-base", default=None, help="Basis der icloud-sync-Spiegel")
    ap.add_argument("--account", action="append", dest="accounts", default=None,
                    help="Account-Name (mehrfach möglich)")
    ap.add_argument("--report", action="store_true", help="Mapping-Bericht statt XML")
    ap.add_argument("-o", "--output", default="-", help="Ziel für das XML (Default: stdout)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    # Settings der App als Default, damit die CLI ohne Argumente das Gleiche sieht
    # wie der laufende Server.
    from settings import load_settings
    cfg = load_settings()
    source_base = args.source_base or cfg["source_base"]
    accounts = args.accounts or cfg["accounts"]

    contacts = dedupe(load_contacts(source_base, accounts))
    if not contacts:
        print(f"Keine Kontakte gefunden unter {source_base} für {accounts}", file=sys.stderr)
        return 1

    if args.report:
        print(build_report(contacts))
        return 0

    xml = to_grandstream_xml(contacts)
    if args.output == "-":
        sys.stdout.buffer.write(xml)
    else:
        Path(args.output).write_bytes(xml)
        print(f"{len(contacts)} Kontakte -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
