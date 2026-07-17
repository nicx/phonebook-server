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


def test_three_slots_only():
    """Am Gerät verifiziert: das WP826 hat NUR Work/Home/Mobile. Fax und Other aus
    dem WP820-Guide (der auf den GXV3275 verweist) faltet die Firmware auf Work —
    und verwirft still, wenn Work schon belegt ist. Deshalb dürfen sie nie raus."""
    check(convert.SLOTS == ("Work", "Home", "Mobile"), "genau drei Slots")
    for verboten in ("Fax", "Other", "Cell"):
        check(verboten not in convert.SLOTS, f"{verboten!r} wird nie gesendet")


def test_three_numbers_fill_the_entry():
    raw = {"contactId": "C2", "firstName": "Bodo", "lastName": "Muster",
           "phones": [
               {"field": "+49 30 2312506", "label": "WORK"},
               {"field": "+49 30 2312507", "label": "HOME"},
               {"field": "+49 30 2312508", "label": "MOBILE"},
           ]}
    c = convert.contact_from_icloud(raw)
    entries = convert.plan_entries(c)
    check(len(entries) == 1, "drei Nummern passen in einen Eintrag")
    check(entries[0].spills == [], "nichts musste ausweichen")
    el = contacts_in(xml_of([c]))[0]
    check(phones_of(el) == {"Work": "+49302312506", "Home": "+49302312507",
                            "Mobile": "+49302312508"}, "alle drei stehen im XML")


def test_fax_label_goes_to_work():
    """Das Gerät hat keinen Fax-Slot und legt Faxnummern selbst auf Work. Also
    gleich selbst dorthin — dann löst plan_entries die Kollision, statt dass das
    Telefon still verwirft."""
    for label in ("WORK FAX", "HOME FAX", "Fax privat", "fax"):
        check(convert._slot_for(label) == "Work", f"Label {label!r} -> Work")
    check(convert._slot_for("OTHER") == "Work", "OTHER -> Work (kein eigener Slot)")
    check(convert._slot_for("PAGER") == "Work", "PAGER -> Work")


def test_fax_collision_does_not_lose_the_number():
    """Genau der Fall, an dem am echten Gerät zwei Nummern verschwanden: Work ist
    belegt, die Faxnummer will auch dorthin. Das Telefon würde sie kommentarlos
    wegwerfen — wir weichen aus."""
    raw = {"contactId": "F1", "firstName": "Gitta", "lastName": "Fax",
           "phones": [
               {"field": "+49 30 2312590", "label": "WORK"},
               {"field": "+49 30 2312591", "label": "WORK FAX"},
           ]}
    c = convert.contact_from_icloud(raw)
    entries = convert.plan_entries(c)
    placed = [n for e in entries for n in e.slots.values()]
    check(len(placed) == 2 and len(set(placed)) == 2, "beide Nummern bleiben erhalten")
    check("verlustfrei" in convert.build_report([c]), "und der Report sagt das auch")


def test_accountindex_is_first_account():
    """Spec: "From 0 to 5 for account 1 to account 6". 0 ist das ERSTE Konto.
    Das FusionPBX-Template schreibt 1 und zeigt damit auf ein zweites Konto,
    das es hier nicht gibt."""
    raw = {"contactId": "C2b", "firstName": "Ida",
           "phones": [{"field": "+49 30 2312520", "label": "MOBILE"}]}
    el = contacts_in(xml_of([convert.contact_from_icloud(raw)]))[0]
    check(el.find("Phone/accountindex").text == "0", "accountindex ist 0 = Konto 1")


def test_slot_collision_spills_into_free_slot():
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
    check("+49302312511" in entries[0].slots.values(), "zweite bleibt erhalten")
    check(set(entries[0].slots) <= set(convert.SLOTS), "und zwar in einem echten Slot")
    check(len(entries[0].spills) == 1, "das Ausweichen wird vermerkt")

    report = convert.build_report([c])
    check("ungenauem Typ" in report and "+49302312511" in report,
          "Report benennt die Nummer mit ungenauem Typ")
    check("verlustfrei" in report, "Report bestätigt: nichts verloren")


def test_only_real_slots_are_ever_used():
    """Kein Eintrag darf je einen Slot benutzen, den das Gerät nicht hat — sonst
    verwirft die Firmware bei Kollision still."""
    raw = {"contactId": "C3d", "firstName": "Frida",
           "phones": [{"field": f"+49 30 23125{50 + i}", "label": "MOBILE"} for i in range(5)]}
    c = convert.contact_from_icloud(raw)
    entries = convert.plan_entries(c)
    for e in entries:
        check(set(e.slots) <= set(convert.SLOTS), "nur echte Slots belegt")
    placed = [n for e in entries for n in e.slots.values()]
    check(len(placed) == 5, "und alle fünf Nummern gehen mit")


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


def test_long_names_are_reported():
    """Das WP826 kappt jedes Namensfeld bei 18 Zeichen (am Export gemessen). Wir
    teilen NICHT automatisch auf Vor-/Nachname auf — Timos Entscheidung: iCloud
    bleibt Quelle der Wahrheit. Aber melden muss der Report es."""
    name = "Robert-Bosch-Gymnasium Sekretariat"
    raw = {"contactId": "C3e", "firstName": name,
           "phones": [{"field": "+49 30 2312560", "label": "WORK"}]}
    c = convert.contact_from_icloud(raw)
    report = convert.build_report([c])
    check("kappt" in report and name in report, "der zu lange Name wird gemeldet")
    check("Robert-Bosch-Gymna" in report, "mitsamt dem, was übrig bleibt")

    kurz = {"contactId": "C3f", "firstName": "Kurt",
            "phones": [{"field": "+49 30 2312561", "label": "WORK"}]}
    r2 = convert.build_report([convert.contact_from_icloud(kurz)])
    check("Namen zu lang fürs Display: keine" in r2, "kurze Namen lösen keine Meldung aus")


def test_name_with_lastname_has_double_the_room():
    """Die 18 gelten pro FELD. Wer einen Nachnamen hat, bekommt 2x18 — deshalb
    meldet der Report nur Kontakte, deren Name ganz im Vornamen steht."""
    raw = {"contactId": "C3g", "firstName": "Andrea (Mama Edith)",
           "lastName": "Stolz-Lindemann",
           "phones": [{"field": "+49 30 2312562", "label": "MOBILE"}]}
    report = convert.build_report([convert.contact_from_icloud(raw)])
    check("Namen zu lang fürs Display: keine" in report,
          "mit Nachname reicht der Platz — keine Meldung")


def test_nickname_replaces_display_name():
    """Der Spitzname ist, wie die Leute im Haushalt wirklich heißen ("Oma Evi").
    Die Spec kennt kein Spitznamensfeld, also ersetzt er den Namen."""
    raw = {"contactId": "N1", "firstName": "Evi", "lastName": "Schmitt",
           "nickName": "Oma Evi",
           "phones": [{"field": "+49 30 2312595", "label": "MOBILE"}]}
    c = convert.contact_from_icloud(raw)
    check(c.nick == "Oma Evi", "nickName wird gelesen")
    check(c.name_fields() == ("Oma Evi", ""), "Spitzname ersetzt Vor- UND Nachname")

    el = contacts_in(xml_of([c]))[0]
    check(el.find("FirstName").text == "Oma Evi", "am Telefon steht der Spitzname")
    # ElementTree liefert für <LastName /> beim Zurücklesen None, nicht "".
    check(not el.find("LastName").text, "kein Nachname daneben")


def test_nickname_key_is_camelcase():
    """Die API liefert nickName, nicht nickname. Genau daran scheitert die vCard in
    icloud-sync — der Fehler darf sich hier nicht wiederholen."""
    c = convert.contact_from_icloud(
        {"contactId": "N2", "firstName": "Evi", "nickname": "FALSCH",
         "phones": [{"field": "+49 30 2312596", "label": "MOBILE"}]})
    check(c.nick == "", "kleingeschriebenes 'nickname' ist NICHT der API-Key")
    check(c.name_fields() == ("Evi", ""), "und wird folglich ignoriert")


def test_without_nickname_nothing_changes():
    c = convert.contact_from_icloud(
        {"contactId": "N3", "firstName": "Jens", "lastName": "Ohne",
         "phones": [{"field": "+49 30 2312597", "label": "MOBILE"}]})
    check(c.name_fields() == ("Jens", "Ohne"), "ohne Spitzname bleibt der echte Name")


# ---------------------------------------------------------------- Favoriten ---

def _fav_contact(cid, first, number, nick=""):
    raw = {"contactId": cid, "firstName": first,
           "phones": [{"field": number, "label": "MOBILE"}]}
    if nick:
        raw["nickName"] = nick
    return convert.contact_from_icloud(raw)


def test_favorite_by_name():
    cs = [_fav_contact("F1", "Evi", "+49 30 2312560", nick="Oma Evi"),
          _fav_contact("F2", "Jens", "+49 30 2312561")]
    unmatched = convert.mark_favorites(cs, ["Oma Evi"])
    check(unmatched == [], "der Name trifft")
    check(cs[0].favorite and not cs[1].favorite, "nur der Gemeinte ist Favorit")
    check("Oma Evi" in convert.build_report(cs), "Report nennt ihn")


def test_favorite_matches_display_name_not_real_name():
    """Gematcht wird gegen das, was am Telefon steht — sonst müsste man wissen, wie
    jemand im Datensatz heißt, statt wie er angezeigt wird."""
    cs = [_fav_contact("F3", "Evi", "+49 30 2312562", nick="Oma Evi")]
    check(convert.mark_favorites(cs, ["Evi"]) == ["Evi"],
          "der echte Vorname trifft NICHT, wenn ein Spitzname angezeigt wird")
    check(not cs[0].favorite, "und markiert folglich nichts")


def test_favorite_by_number_ignores_formatting():
    cs = [_fav_contact("F4", "Jens", "+49 (30) 2312563")]
    check(convert.mark_favorites(cs, ["+49 30 2312563"]) == [],
          "Nummer trifft trotz anderer Schreibweise")
    check(cs[0].favorite, "und markiert")


def test_favorite_is_case_insensitive():
    cs = [_fav_contact("F5", "Evi", "+49 30 2312564", nick="Oma Evi")]
    convert.mark_favorites(cs, ["oma evi"])
    check(cs[0].favorite, "Groß-/Kleinschreibung ist egal")


def test_unmatched_favorite_is_reported():
    """Ein Tippfehler in der Liste würde sonst nie auffallen — man sucht den Fehler
    am Telefon statt in der Konfiguration."""
    cs = [_fav_contact("F6", "Jens", "+49 30 2312565")]
    unmatched = convert.mark_favorites(cs, ["Gibt Es Nicht", "+49 30 9999999"])
    check(unmatched == ["Gibt Es Nicht", "+49 30 9999999"], "beide Fehltreffer werden gemeldet")
    report = convert.build_report(cs, unmatched)
    check("ACHTUNG" in report and "Gibt Es Nicht" in report, "und stehen im Report")


def test_favorite_writes_frequent():
    cs = [_fav_contact("F7", "Evi", "+49 30 2312566", nick="Oma Evi"),
          _fav_contact("F8", "Jens", "+49 30 2312567")]
    convert.mark_favorites(cs, ["Oma Evi"])
    els = contacts_in(xml_of(cs))
    check(els[0].find("Frequent").text == "1", "Favorit bekommt <Frequent>1</Frequent>")
    check(els[1].find("Frequent") is None, "Nicht-Favorit bekommt gar kein Frequent")


def test_favorite_only_on_first_entry():
    """Ein Folge-Eintrag ("Name (2)") ist eine Notlösung für überzählige Nummern,
    kein zweiter Favorit."""
    raw = {"contactId": "F9", "firstName": "Viel", "nickName": "Opa Viel",
           "phones": [{"field": f"+49 30 23125{70 + i}", "label": "MOBILE"} for i in range(6)]}
    c = convert.contact_from_icloud(raw)
    convert.mark_favorites([c], ["Opa Viel"])
    els = contacts_in(xml_of([c]))
    check(len(els) == 2, "sechs Nummern -> zwei Einträge")
    check(els[0].find("Frequent").text == "1", "erster Eintrag ist der Favorit")
    check(els[1].find("Frequent").text == "0", "der Folge-Eintrag ausdrücklich nicht")


def test_empty_favorites_list():
    cs = [_fav_contact("FA", "Jens", "+49 30 2312568")]
    check(convert.mark_favorites(cs, []) == [], "leere Liste -> nichts zu tun")
    check(convert.mark_favorites(cs, None) == [], "None -> nichts zu tun")
    check(convert.mark_favorites(cs, ["  ", ""]) == [], "leere Einträge werden ignoriert")
    check(not cs[0].favorite, "und nichts wird markiert")


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


def test_only_real_types_are_emitted():
    """Am Gerät verifiziert: nur Work/Home/Mobile werden echt gespeichert. Fax und
    Other faltet die Firmware auf Work — und verwirft still, wenn Work belegt ist."""
    allowed = set(convert.SLOTS)
    check(allowed == {"Work", "Home", "Mobile"}, "SLOTS ist, was das Gerät wirklich hat")
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
    test_three_slots_only()
    test_three_numbers_fill_the_entry()
    test_fax_label_goes_to_work()
    test_fax_collision_does_not_lose_the_number()
    test_accountindex_is_first_account()
    test_slot_collision_spills_into_free_slot()
    test_only_real_slots_are_ever_used()
    test_more_numbers_than_slots_creates_extra_entry()
    test_extra_entry_suffix_survives_truncation()
    test_long_names_are_reported()
    test_name_with_lastname_has_double_the_room()
    test_nickname_replaces_display_name()
    test_nickname_key_is_camelcase()
    test_without_nickname_nothing_changes()
    test_favorite_by_name()
    test_favorite_matches_display_name_not_real_name()
    test_favorite_by_number_ignores_formatting()
    test_favorite_is_case_insensitive()
    test_unmatched_favorite_is_reported()
    test_favorite_writes_frequent()
    test_favorite_only_on_first_entry()
    test_empty_favorites_list()
    test_company_only_contact()
    test_contact_without_phone_is_dropped()
    test_hostile_characters_are_escaped()
    test_field_sanity_limits()
    test_xml_shape()
    test_slot_order_is_stable()
    test_only_real_types_are_emitted()
    test_load_contacts()
    test_dedupe_across_accounts()
    test_report_unknown_detection_matches_mapping()
    test_report_runs_on_empty()

    print(f"\n{len(PASS)} ok, {len(FAIL)} fehlgeschlagen")
    sys.exit(1 if FAIL else 0)
