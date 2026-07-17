#!/usr/bin/env python3
"""Tests für den Konverter — eigenständig, kein pytest, kein Netz.

Muster wie icloud-sync/tests/test_sync.py: check()-Helfer, PASS-Liste, expliziter
Aufruf aller Testfunktionen am Ende.

    python3 tests/test_convert.py

ALLE Testdaten sind frei erfunden. Die *Strukturen* entsprechen echten
iCloud-API-Antworten, die *Inhalte* nicht — dieses Repo ist öffentlich.
Rufnummern stammen aus dem für fiktive Verwendung reservierten Block
+49 30 23125 xx (BNetzA-Dramanummern).
"""

from __future__ import annotations

import sys
import json
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    if not cond:
        print("FAIL: " + msg)


def xml_of(contacts):
    return ET.fromstring(convert.to_grandstream_xml(contacts))


def contacts_in(root):
    return root.findall("Contact")


def phones_of(el):
    return {p.get("type"): p.find("phonenumber").text for p in el.findall("Phone")}


# ----------------------------------------------------------- Nummern-Hygiene ---

def test_clean_number():
    check(convert.clean_number("+49 30 2312501") == "+49302312501", "Leerzeichen entfernt, + bleibt")
    check(convert.clean_number("(030) 2312502") == "0302312502", "Klammern entfernt, national bleibt national")
    check(convert.clean_number("030-2312.503") == "0302312503", "Trenner entfernt")
    check(convert.clean_number("") == "", "leere Nummer -> leer")
    check(convert.clean_number("keine Ziffern") == "", "Text ohne Ziffern -> leer")
    # Das + darf nur führend gelten, sonst würde aus einer Durchwahl-Notation Unsinn.
    check(convert.clean_number("030 2312504+5") == "03023125045", "+ mitten drin verschwindet")


def test_slot_mapping():
    cases = {
        "MOBILE": "Mobile", "IPHONE": "Mobile", "Mobil": "Mobile", "WhatsApp": "Mobile",
        "HOME": "Home", "Homeoffice": "Home", "WORK": "Work", "MAIN": "Work",
    }
    for label, want in cases.items():
        check(convert._slot_for(label) == want, f"Label {label!r} -> {want}")
    check(convert._slot_for(None) == "Mobile", "fehlendes Label -> Mobile (Default)")
    check(convert._slot_for("") == "Mobile", "leeres Label -> Mobile")
    check(convert._slot_for("Voodoo") == "Mobile", "unbekanntes Label -> Mobile statt Verlust")


# ------------------------------------------------------------------ Mapping ---

def test_contact_without_label():
    """35 von 41 Nummern eines echten Accounts haben kein `label` — das ist der
    Normalfall, nicht der Sonderfall."""
    raw = {"contactId": "C1", "firstName": "Anke", "lastName": "Beispiel",
           "phones": [{"field": "+49 30 2312505"}]}
    c = convert.contact_from_icloud(raw)
    check(len(c.phones) == 1, "Nummer ohne Label wird übernommen")
    check(c.phones[0].slot == "Mobile", "Nummer ohne Label landet in Mobile")
    check(c.phones[0].label == "(ohne Label)", "fehlendes Label wird für den Report benannt")


def test_all_five_slots():
    """Die WP820-Spec erlaubt Work/Home/Mobile/Fax/Other. Eine Faxnummer belegt den
    Fax-Slot und kostet damit KEINEN Sprach-Slot."""
    raw = {"contactId": "C2", "firstName": "Bodo", "lastName": "Muster",
           "phones": [
               {"field": "+49 30 2312506", "label": "WORK"},
               {"field": "+49 30 2312507", "label": "HOME"},
               {"field": "+49 30 2312508", "label": "MOBILE"},
               {"field": "+49 30 2312509", "label": "WORK FAX"},
               {"field": "+49 30 2312519", "label": "OTHER"},
           ]}
    c = convert.contact_from_icloud(raw)
    check(len(c.phones) == 5, "keine Nummer wird beim Einlesen verworfen — auch Fax nicht")
    entries = convert.plan_entries(c)
    check(len(entries) == 1, "fünf Nummern passen in einen Eintrag")
    check(entries[0].spills == [], "nichts musste ausweichen")

    el = contacts_in(xml_of([c]))[0]
    check(phones_of(el) == {"Work": "+49302312506", "Home": "+49302312507",
                            "Mobile": "+49302312508", "Fax": "+49302312509",
                            "Other": "+49302312519"},
          "alle fünf Typen stehen mit der richtigen Nummer im XML")


def test_fax_label_variants():
    for label in ("WORK FAX", "HOME FAX", "Fax privat", "fax"):
        check(convert._slot_for(label) == "Fax", f"Label {label!r} -> Fax")


def test_report_flags_fax_as_invisible():
    """Faxnummern stehen spec-konform im XML, das WP826 zeigt den Fax-Slot aber nicht
    an. Sie unter "verlustfrei" mitzuzählen wäre eine Lüge — der Report muss sie
    getrennt ausweisen."""
    raw = {"contactId": "F1", "firstName": "Gitta", "lastName": "Fax",
           "phones": [
               {"field": "+49 30 2312590", "label": "HOME"},
               {"field": "+49 30 2312591", "label": "HOME FAX"},
           ]}
    report = convert.build_report([convert.contact_from_icloud(raw)])
    check("NICHT sichtbar" in report, "Report warnt, dass Fax am Gerät unsichtbar ist")
    check("+49302312591" in report, "die betroffene Nummer wird benannt")
    check("davon am WP826 sichtbar: 1" in report,
          "die Sichtbarkeitszahl zählt das Fax nicht mit")

    ohne = {"contactId": "F2", "firstName": "Hein",
            "phones": [{"field": "+49 30 2312592", "label": "MOBILE"}]}
    r2 = convert.build_report([convert.contact_from_icloud(ohne)])
    check("Faxnummern: keine" in r2, "ohne Fax bleibt der Report ruhig")
    check("unsichtbar" not in r2, "und warnt dann auch nicht")


def test_accountindex_is_first_account():
    """Spec: "From 0 to 5 for account 1 to account 6". 0 ist das ERSTE Konto.
    Das FusionPBX-Template schreibt 1 und zeigt damit auf ein zweites Konto,
    das es hier nicht gibt."""
    raw = {"contactId": "C2b", "firstName": "Ida",
           "phones": [{"field": "+49 30 2312520", "label": "MOBILE"}]}
    el = contacts_in(xml_of([convert.contact_from_icloud(raw)]))[0]
    check(el.find("Phone/accountindex").text == "0", "accountindex ist 0 = Konto 1")


def test_slot_collision_spills_into_other():
    """Zwei Handys: der Mobile-Slot ist weg. Die zweite Nummer darf nicht verloren
    gehen, nur weil ihr Wunsch-Slot belegt ist — und "Other" ist die ehrlichste
    Aussage über eine Zweitnummer."""
    raw = {"contactId": "C3", "firstName": "Cem", "lastName": "Probe",
           "phones": [
               {"field": "+49 30 2312510", "label": "MOBILE"},
               {"field": "+49 30 2312511", "label": "IPHONE"},
           ]}
    c = convert.contact_from_icloud(raw)
    entries = convert.plan_entries(c)
    check(len(entries) == 1, "kein Zusatzeintrag nötig, es ist ja Platz")
    check(entries[0].slots["Mobile"] == "+49302312510", "erste Nummer behält den Wunsch-Slot")
    check(entries[0].slots.get("Other") == "+49302312511", "zweite landet in Other, nicht in Work")
    check(len(entries[0].spills) == 1, "das Ausweichen wird vermerkt")

    report = convert.build_report([c])
    check("ungenauem Typ" in report and "+49302312511" in report,
          "Report benennt die Nummer mit ungenauem Typ")
    check("verlustfrei" in report, "Report bestätigt: nichts verloren")


def test_voice_number_never_spills_into_fax():
    """Eine Sprachnummer als Fax auszuweisen wäre aktiv irreführend — man würde sie
    nicht anrufen. Lieber ein Zusatzeintrag als ein falsches Fax."""
    raw = {"contactId": "C3d", "firstName": "Frida",
           "phones": [{"field": f"+49 30 23125{50 + i}", "label": "MOBILE"} for i in range(5)]}
    c = convert.contact_from_icloud(raw)
    entries = convert.plan_entries(c)
    for e in entries:
        check("Fax" not in e.slots, "kein Sprachanschluss landet je im Fax-Slot")
    placed = [n for e in entries for n in e.slots.values()]
    check(len(placed) == 5, "trotzdem gehen alle fünf Nummern mit")


def test_more_numbers_than_slots_creates_extra_entry():
    """Sechs wählbare Nummern passen nicht in die vier Spill-tauglichen Slots."""
    raw = {"contactId": "C3b", "firstName": "Dirk", "lastName": "Viel",
           "phones": [{"field": f"+49 30 23125{30 + i}", "label": "MOBILE"} for i in range(6)]}
    c = convert.contact_from_icloud(raw)
    entries = convert.plan_entries(c)
    check(len(entries) == 2, "sechs gleichartige Nummern -> zwei Einträge")
    placed = [n for e in entries for n in e.slots.values()]
    check(len(placed) == 6 and len(set(placed)) == 6, "alle sechs im Plan, keine doppelt")

    els = contacts_in(xml_of([c]))
    check(len(els) == 2, "zwei Contact-Elemente im XML")
    check(els[0].find("LastName").text == "Viel", "erster Eintrag behält den Namen")
    check(els[1].find("LastName").text == "Viel (2)", "Folge-Eintrag bekommt ein Suffix")


def test_extra_entry_suffix_survives_truncation():
    """Bei absurd langem Namen darf das Suffix nicht selbst der Längengrenze zum
    Opfer fallen."""
    raw = {"contactId": "C3c", "firstName": "Emil", "lastName": "L" * 200,
           "phones": [{"field": f"+49 30 23125{40 + i}", "label": "MOBILE"} for i in range(6)]}
    c = convert.contact_from_icloud(raw)
    els = contacts_in(xml_of([c]))
    check(len(els) == 2, "sechs gleichartige Nummern -> zwei Einträge")
    last2 = els[1].find("LastName").text
    check(last2.endswith(" (2)"), "Suffix ist trotz Überlänge vorhanden")
    check(len(last2) <= convert.MAX_NAME, "Feld bleibt in der Vernunftgrenze")


def test_real_names_are_not_truncated():
    """Die 24 aus dem FusionPBX-Template ist GXP-Folklore; die WP-Spec nennt für
    Namen nur "String". Echte Einträge wie "Robert-Bosch-Gymnasium Sekretariat"
    dürfen nicht verstümmelt werden."""
    name = "Robert-Bosch-Gymnasium Sekretariat"
    raw = {"contactId": "C3e", "firstName": name,
           "phones": [{"field": "+49 30 2312560", "label": "WORK"}]}
    el = contacts_in(xml_of([convert.contact_from_icloud(raw)]))[0]
    check(el.find("FirstName").text == name, "34-Zeichen-Name kommt vollständig durch")


def test_company_only_contact():
    """Firmenkontakt ohne Personennamen: der Name muss trotzdem sichtbar sein."""
    raw = {"contactId": "C4", "companyName": "Muster GmbH", "isCompany": True,
           "phones": [{"field": "+49 30 2312512", "label": "WORK"}]}
    c = convert.contact_from_icloud(raw)
    el = contacts_in(xml_of([c]))[0]
    check(el.find("FirstName").text == "Muster GmbH", "Firmenname rückt in FirstName")
    check(el.find("Company").text == "Muster GmbH", "Company bleibt zusätzlich gesetzt")


def test_contact_without_phone_is_dropped():
    """~30 der 331 echten Kontakte haben keine Nummer. Sie gehören nicht ins Telefonbuch."""
    raw = {"contactId": "C5", "firstName": "Dana", "emailAddresses": [{"field": "d@example.org"}]}
    c = convert.contact_from_icloud(raw)
    check(c.phones == [], "Kontakt ohne Telefon hat keine Nummern")
    root = xml_of([c])
    check(contacts_in(root) == [], "und taucht im XML gar nicht auf")


def test_hostile_characters_are_escaped():
    """Apple-Labels enthalten real ':' und '<' — von Hand gebautes XML wäre hier tot."""
    raw = {"contactId": "C6", "firstName": "Eva <script>", "lastName": "A & B: C",
           "phones": [{"field": "+49 30 2312513", "label": "_$!<Friend>!$_X-PREF:IMPLICIT"}]}
    c = convert.contact_from_icloud(raw)
    xml = convert.to_grandstream_xml([c])
    root = ET.fromstring(xml)  # wirft, wenn kaputt escaped
    el = contacts_in(root)[0]
    check(el.find("FirstName").text == "Eva <script>", "'<' überlebt den Roundtrip")
    check(el.find("LastName").text == "A & B: C", "'&' und ':' überleben den Roundtrip")
    check(b"<script>" not in xml, "'<' steht escaped im Byte-Stream, nicht roh")


def test_field_sanity_limits():
    """Vernunftgrenze gegen kaputte Daten — greift bei echten Namen nie."""
    raw = {"contactId": "C7", "firstName": "F" * 200, "lastName": "L" * 200,
           "companyName": "C" * 200,
           "phones": [{"field": "+4930231251" + "4" * 60, "label": "WORK"}]}
    c = convert.contact_from_icloud(raw)
    el = contacts_in(xml_of([c]))[0]
    check(len(el.find("FirstName").text) == convert.MAX_NAME, "absurder FirstName wird gekappt")
    check(len(el.find("LastName").text) == convert.MAX_NAME, "absurder LastName wird gekappt")
    check(len(el.find("Company").text) == convert.MAX_NAME, "absurde Company wird gekappt")
    check(len(phones_of(el)["Work"]) == convert.MAX_NUMBER, "absurde Nummer wird gekappt")


# -------------------------------------------------------------------- Struktur ---

def test_xml_shape():
    raw = {"contactId": "C8", "firstName": "Gero", "lastName": "Test",
           "phones": [{"field": "+49 30 2312515", "label": "HOME"}]}
    c = convert.contact_from_icloud(raw)
    xml = convert.to_grandstream_xml([c])
    check(xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'), "XML-Deklaration wie dokumentiert")
    root = ET.fromstring(xml)
    check(root.tag == "AddressBook", "Wurzel ist AddressBook")
    check(root.find("version").text == "1", "version = 1")
    ph = contacts_in(root)[0].find("Phone")
    check(ph.get("type") == "Home", "Phone hat das type-Attribut")
    check(ph.find("accountindex").text == "0", "accountindex ist gesetzt")


def test_slot_order_is_stable():
    """Stabile Reihenfolge: sonst wechselt die Datei ohne Datenänderung und das
    Telefon lädt grundlos neu."""
    raw = {"contactId": "C9", "firstName": "Hans",
           "phones": [
               {"field": "+49 30 2312516", "label": "MOBILE"},
               {"field": "+49 30 2312517", "label": "WORK"},
               {"field": "+49 30 2312518", "label": "HOME"},
           ]}
    c = convert.contact_from_icloud(raw)
    el = contacts_in(xml_of([c]))[0]
    check([p.get("type") for p in el.findall("Phone")] == ["Work", "Home", "Mobile"],
          "Slots immer in SLOTS-Reihenfolge, unabhängig von der Eingabe")
    check(convert.to_grandstream_xml([c]) == convert.to_grandstream_xml([c]),
          "gleiche Eingabe -> byte-gleiche Ausgabe")


def test_only_spec_types_are_emitted():
    """Ein type-Wert außerhalb der Spec (etwa das GXP-"Cell") könnte das Gerät die
    Nummer verschlucken lassen — bei Handys wäre das die Mehrheit der Daten."""
    allowed = set(convert.SLOTS)
    check(allowed == {"Work", "Home", "Mobile", "Fax", "Other"},
          "SLOTS entspricht exakt der WP820-Spec")
    check("Cell" not in allowed, "kein GXP-'Cell' — das kennt die WP-Reihe nicht")
    raws = [
        {"contactId": "T1", "firstName": "A", "phones": [{"field": "+49 30 2312570", "label": lbl}]}
        for lbl in ("MOBILE", "IPHONE", "HOME", "WORK", "WORK FAX", "OTHER", "Voodoo", None)
    ]
    cs = [convert.contact_from_icloud(r) for r in raws]
    root = xml_of(cs)
    got = {p.get("type") for p in root.iter("Phone")}
    check(got <= allowed, f"alle erzeugten Typen sind spec-konform: {sorted(got)}")


# --------------------------------------------------------------------- Laden ---

def _write_account(base: Path, account: str, contacts: list[dict]):
    d = base / account / "Contacts"
    d.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(contacts):
        (d / f"kontakt_{i}.json").write_text(json.dumps(c), encoding="utf-8")


def test_load_contacts():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_account(base, "Familie", [
            {"contactId": "A", "firstName": "Ina", "phones": [{"field": "+49 30 2312519", "label": "MOBILE"}]},
            {"contactId": "B", "firstName": "Ohne Nummer"},
        ])
        got = convert.load_contacts(base, ["Familie"])
        check(len(got) == 1, "nur Kontakte mit Nummer werden geladen")
        check(got[0].account == "Familie", "Herkunfts-Account wird vermerkt")

        # Fehlendes Verzeichnis (SMB-Mount weg) darf nicht werfen.
        check(convert.load_contacts(base, ["GibtsNicht"]) == [], "fehlender Account -> leer statt Fehler")

        # Kaputte JSON-Datei darf den Lauf nicht kippen.
        (base / "Familie" / "Contacts" / "kaputt.json").write_text("{nicht json", encoding="utf-8")
        check(len(convert.load_contacts(base, ["Familie"])) == 1, "kaputte Datei wird übersprungen")


def test_dedupe_across_accounts():
    """contactId ist accountlokal — dieselbe Person hat je Account eine andere ID.
    Deshalb wird über die Rufnummern dedupliziert."""
    a = convert.contact_from_icloud(
        {"contactId": "base64id", "firstName": "Jan", "phones": [{"field": "+49 30 2312520", "label": "MOBILE"}]})
    a.account = "Familie"
    b = convert.contact_from_icloud(
        {"contactId": "550e8400-e29b-41d4-a716-446655440000", "firstName": "Jan",
         "phones": [{"field": "+49 (30) 2312520", "label": "IPHONE"}]})
    b.account = "Timo"
    out = convert.dedupe([a, b])
    check(len(out) == 1, "gleiche Nummer trotz anderer contactId und Schreibweise = ein Kontakt")
    check(out[0].account == "Familie", "erster Account in der Liste gewinnt")

    c = convert.contact_from_icloud(
        {"contactId": "X", "firstName": "Jan", "phones": [{"field": "+49 30 2312521", "label": "MOBILE"}]})
    check(len(convert.dedupe([a, c])) == 2, "Namensgleichheit allein ist kein Duplikat")


def test_report_unknown_detection_matches_mapping():
    """Der Report muss dieselben Mapping-Wege kennen wie _slot_for. Sonst meldet er
    Labels als "unbekannt", die er zwei Zeilen weiter oben korrekt zuordnet."""
    check(not convert._is_unknown_label("HOME FAX"), "'HOME FAX' ist nicht unbekannt — es geht auf Fax")
    check(not convert._is_unknown_label("MOBILE"), "'MOBILE' ist nicht unbekannt")
    check(convert._is_unknown_label("Voodoo"), "'Voodoo' ist tatsächlich unbekannt")

    raw = {"contactId": "R1", "firstName": "Rita",
           "phones": [{"field": "+49 30 2312580", "label": "HOME FAX"}]}
    report = convert.build_report([convert.contact_from_icloud(raw)])
    check("Unbekannte Labels: keine" in report,
          "ein sauber gemapptes Fax-Label taucht nicht in der Unbekannt-Liste auf")


def test_report_runs_on_empty():
    check("0" in convert.build_report([]), "Report kippt nicht bei null Kontakten")


if __name__ == "__main__":
    test_clean_number()
    test_slot_mapping()
    test_contact_without_label()
    test_all_five_slots()
    test_fax_label_variants()
    test_report_flags_fax_as_invisible()
    test_accountindex_is_first_account()
    test_slot_collision_spills_into_other()
    test_voice_number_never_spills_into_fax()
    test_more_numbers_than_slots_creates_extra_entry()
    test_extra_entry_suffix_survives_truncation()
    test_real_names_are_not_truncated()
    test_company_only_contact()
    test_contact_without_phone_is_dropped()
    test_hostile_characters_are_escaped()
    test_field_sanity_limits()
    test_xml_shape()
    test_slot_order_is_stable()
    test_only_spec_types_are_emitted()
    test_load_contacts()
    test_dedupe_across_accounts()
    test_report_unknown_detection_matches_mapping()
    test_report_runs_on_empty()

    print(f"\n{len(PASS)} ok, {len(FAIL)} fehlgeschlagen")
    sys.exit(1 if FAIL else 0)
