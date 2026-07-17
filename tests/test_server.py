#!/usr/bin/env python3
"""Tests für Builder, Guards und HTTP — eigenständig, kein pytest.

    .venv/bin/python tests/test_server.py

Braucht `rumps` (Import von phonebook_server), aber startet keine Menüleisten-App.
Der HTTP-Server lauscht auf 127.0.0.1 und einem Wegwerf-Port.

Alle Daten frei erfunden; Rufnummern aus dem für fiktive Verwendung reservierten
Block +49 30 23125 xx.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as cfgmod  # noqa: E402

# App-Support in einen Sandkasten umbiegen, BEVOR phonebook_server importiert wird —
# sonst schriebe der Test in die echte Konfiguration.
_TMP = Path(tempfile.mkdtemp(prefix="phonebook-test-"))
cfgmod.SUPPORT = _TMP / "support"
cfgmod.CACHE_PATH = cfgmod.SUPPORT / "phonebook.xml"
cfgmod.LOGS_DIR = cfgmod.SUPPORT / "logs"
cfgmod.LOG_PATH = cfgmod.LOGS_DIR / "test.log"
cfgmod.SETTINGS_PATH = cfgmod.SUPPORT / "settings.json"
cfgmod.secure_dir(cfgmod.SUPPORT)

import phonebook_server as ps  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    if not cond:
        print("FAIL: " + msg)


def _contact(cid, name, number, label="MOBILE"):
    return {"contactId": cid, "firstName": name,
            "phones": [{"field": number, "label": label}]}


def _write(src: Path, account: str, contacts: list[dict]):
    d = src / account / "Contacts"
    d.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(contacts):
        (d / f"k{i}.json").write_text(json.dumps(c), encoding="utf-8")
    return d


def _fresh_source(names=("Ina", "Uwe")):
    src = _TMP / f"src{time.monotonic_ns()}"
    _write(src, "Familie", [_contact(f"C{i}", n, f"+49 30 23125{i:02d}")
                            for i, n in enumerate(names)])
    return src


def _cfg(src, port=0):
    return {"accounts": ["Familie"], "source_base": str(src), "bind": "127.0.0.1",
            "port": port, "basic_auth_user": "wp826"}


def _reset_cache():
    cfgmod.CACHE_PATH.unlink(missing_ok=True)


# ------------------------------------------------------------------ Builder ---

def test_build_and_cache():
    _reset_cache()
    b = ps.PhonebookBuilder()
    check(b.refresh(_cfg(_fresh_source())), "erster Build gelingt")
    check(b.contact_count == 2, "beide Kontakte gebaut")
    check(b.last_error is None, "kein Fehler vermerkt")
    check(cfgmod.CACHE_PATH.exists(), "Cache-Datei liegt auf der Platte")
    check(b.read_cache().count(b"<Contact>") == 2, "Cache enthält beide Einträge")


def test_no_rebuild_when_unchanged():
    _reset_cache()
    src = _fresh_source()
    b = ps.PhonebookBuilder()
    b.refresh(_cfg(src))
    first = cfgmod.CACHE_PATH.stat().st_mtime_ns
    b.refresh(_cfg(src))
    check(cfgmod.CACHE_PATH.stat().st_mtime_ns == first,
          "unveränderte Quelle -> kein zweiter Schreibvorgang")


def test_deletion_is_detected():
    """Der wichtigste Test. icloud-sync ist ein Spiegel: wird ein Kontakt in iCloud
    gelöscht, verschwindet seine JSON. Dabei ändert sich die mtime KEINER
    verbleibenden Datei — nur die des Verzeichnisses. Wer nur Datei-mtimes
    vergleicht, behält den Gelöschten für immer im Telefonbuch."""
    _reset_cache()
    src = _fresh_source(("Ina", "Uwe", "Rolf"))
    cfg = _cfg(src)
    b = ps.PhonebookBuilder()
    b.refresh(cfg)
    check(b.contact_count == 3, "drei Kontakte vor der Löschung")

    d = src / "Familie" / "Contacts"
    # Datei-mtimes künstlich altern lassen: ein reiner Datei-mtime-Vergleich
    # könnte die Löschung jetzt garantiert nicht mehr bemerken.
    old = time.time() - 3600
    for f in d.glob("*.json"):
        os.utime(f, (old, old))
    now = time.time()
    os.utime(cfgmod.CACHE_PATH, (now, now))

    (d / "k2.json").unlink()
    b.refresh(cfg)
    xml = b.read_cache()
    check(xml.count(b"<Contact>") == 2, "nach Löschung nur noch zwei Einträge")
    check(b"Rolf" not in xml, "der gelöschte Kontakt ist wirklich raus")


def test_guard_missing_mount_keeps_cache():
    """SMB-Mount weg: load_contacts würde den Account still überspringen und ein
    kürzeres Telefonbuch über den guten Cache schreiben. Darf nicht passieren."""
    _reset_cache()
    src = _fresh_source()
    cfg = _cfg(src)
    b = ps.PhonebookBuilder()
    b.refresh(cfg)
    shutil.rmtree(src / "Familie")

    check(b.refresh(cfg) is True, "Cache bleibt trotz fehlender Quelle nutzbar")
    check(b.read_cache().count(b"<Contact>") == 2, "Cache wurde NICHT überschrieben")
    check("Mount" in (b.last_error or ""), "Ursache wird korrekt benannt")


def test_guard_empty_source_keeps_cache():
    """Verzeichnis da, aber leer — anderer Fehler als ein fehlender Mount, und die
    Diagnose muss das auseinanderhalten."""
    _reset_cache()
    src = _fresh_source()
    cfg = _cfg(src)
    b = ps.PhonebookBuilder()
    b.refresh(cfg)
    for f in (src / "Familie" / "Contacts").glob("*.json"):
        f.unlink()

    check(b.refresh(cfg) is True, "Cache bleibt bei leerer Quelle nutzbar")
    check(b.read_cache().count(b"<Contact>") == 2, "Cache wurde NICHT überschrieben")
    check("0 Kontaktdateien" in (b.last_error or ""),
          "leere Quelle wird nicht als fehlender Mount fehldiagnostiziert")


def test_no_cache_and_no_source():
    _reset_cache()
    b = ps.PhonebookBuilder()
    check(b.refresh(_cfg(_TMP / "gibtsnicht")) is False,
          "ohne Cache UND ohne Quelle gibt es nichts auszuliefern")


# --------------------------------------------------------------------- HTTP ---

class _Server:
    def __init__(self, cfg, password="geheim"):
        self.cfg = dict(cfg)
        self.password = password
        self.builder = ps.PhonebookBuilder()
        self.builder.refresh(self.cfg, force=True)
        self.httpd = ps.serve(self.builder, self.cfg, password)
        self.port = self.httpd.server_address[1]  # port=0 -> Kernel wählt

    def url(self, path="/phonebook.xml"):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path="/phonebook.xml", user="wp826", pw="geheim", method="GET"):
        req = urllib.request.Request(self.url(path), method=method)
        if user is not None:
            tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_http_auth():
    _reset_cache()
    s = _Server(_cfg(_fresh_source()))
    try:
        check(s.get(user=None)[0] == 401, "ohne Authorization-Header -> 401")
        check(s.get(pw="falsch")[0] == 401, "falsches Passwort -> 401")
        check(s.get(user="root")[0] == 401, "falscher Benutzer -> 401")
        code, _, hdrs = s.get(user=None)
        check("Basic" in (hdrs.get("WWW-Authenticate") or ""),
              "401 fordert Basic an (Grandstream kann kein Digest)")
        code, body, hdrs = s.get()
        check(code == 200, "korrekte Zugangsdaten -> 200")
        check(hdrs.get("Content-Type") == "text/xml; charset=utf-8", "Content-Type ist XML")
        check(int(hdrs.get("Content-Length")) == len(body), "Content-Length stimmt")
        check(body.count(b"<Contact>") == 2, "Antwort enthält das Telefonbuch")
    finally:
        s.close()


def test_http_routes():
    _reset_cache()
    s = _Server(_cfg(_fresh_source()))
    try:
        check(s.get(path="/")[0] == 404, "Wurzel -> 404, kein Directory-Listing")
        check(s.get(path="/../settings.json")[0] == 404, "Pfad-Traversal -> 404")
        check(s.get(path="/phonebook.xml?x=1")[0] == 200, "Query-String stört nicht")
        check(s.get(method="HEAD")[0] == 200, "HEAD wird beantwortet")
        code, body, _ = s.get(method="HEAD")
        check(body == b"", "HEAD liefert keinen Body")
    finally:
        s.close()


def test_http_serves_stale_cache_on_broken_source():
    """Das Telefon darf nie einen Fehler sehen, solange irgendein Cache existiert."""
    _reset_cache()
    src = _fresh_source()
    s = _Server(_cfg(src))
    try:
        shutil.rmtree(src / "Familie")
        code, body, _ = s.get()
        check(code == 200, "kaputte Quelle -> trotzdem 200 (kein 5xx ans Telefon)")
        check(body.count(b"<Contact>") == 2, "es kommt der letzte gute Stand")
    finally:
        s.close()


# ----------------------------------------------------------- Live-Übernahme ---

class _HeadlessApp(ps.PhonebookApp):
    """Nur die Reload-Mechanik, ohne Menüleiste und ohne Server.

    Umgeht bewusst ``PhonebookApp.__init__`` (das würde rumps hochfahren), nutzt
    aber die **echten** ``_reload_if_changed``/``_apply_settings``. Neustarts werden
    nur gezählt.
    """

    def __init__(self, cfg):
        import logging
        self.log = logging.getLogger("test")
        cfgmod.save_settings(cfg)
        self.cfg = cfgmod.load_settings()
        self._cfg_mtime = cfgmod.SETTINGS_PATH.stat().st_mtime_ns
        self.builder = ps.PhonebookBuilder()
        self.httpd = None
        self.restarts = 0

    def restart(self):
        self.restarts += 1


def _edit(**changes):
    cfg = cfgmod.load_settings()
    cfg.update(changes)
    time.sleep(0.01)  # mtime_ns muss sich messbar unterscheiden
    cfgmod.save_settings(cfg)


def test_reload_picks_up_source_change_without_restart():
    """Der Bug, der das ausgelöst hat: self.cfg wurde nur beim Start geladen, der
    settings_provider des Notifiers war damit ein leeres Versprechen."""
    _reset_cache()
    src = _TMP / f"live{time.monotonic_ns()}"
    _write(src, "Familie", [_contact("A", "Ina", "+49 30 2312501")])
    _write(src, "Timo", [_contact("B", "Rolf", "+49 30 2312502")])

    a = _HeadlessApp({**cfgmod.DEFAULTS, "accounts": ["Familie"],
                      "source_base": str(src), "port": 18100})
    a.builder.refresh(a.cfg, force=True)
    check(a.builder.contact_count == 1, "startet mit einem Account")

    _edit(accounts=["Familie", "Timo"])
    a._reload_if_changed()
    check(a.cfg["accounts"] == ["Familie", "Timo"], "Änderung an der Datei wird übernommen")
    check(a.builder.contact_count == 2, "und wirkt sofort — ohne Neustart neu gebaut")
    check(a.restarts == 0, "eine Quellenänderung braucht keinen Neustart")


def test_reload_restarts_only_for_listener_fields():
    _reset_cache()
    src = _TMP / f"live{time.monotonic_ns()}"
    _write(src, "Familie", [_contact("A", "Ina", "+49 30 2312503")])
    a = _HeadlessApp({**cfgmod.DEFAULTS, "accounts": ["Familie"],
                      "source_base": str(src), "port": 18100})
    a.builder.refresh(a.cfg, force=True)

    _edit(notify_to="neu@example.org")
    a._reload_if_changed()
    check(a.cfg["notify_to"] == "neu@example.org", "Mail-Feld wird übernommen")
    check(a.restarts == 0, "Mail-Felder greifen ohne Neustart")

    for field, value in (("port", 18101), ("bind", "127.0.0.1"), ("basic_auth_user", "neu")):
        before = a.restarts
        _edit(**{field: value})
        a._reload_if_changed()
        check(a.restarts == before + 1, f"{field} ist listener-relevant -> genau ein Neustart")


def test_reload_ignores_touch_without_change():
    """Sonst würde jeder Speichervorgang einen überflüssigen Rebuild auslösen."""
    _reset_cache()
    src = _TMP / f"live{time.monotonic_ns()}"
    _write(src, "Familie", [_contact("A", "Ina", "+49 30 2312504")])
    a = _HeadlessApp({**cfgmod.DEFAULTS, "accounts": ["Familie"],
                      "source_base": str(src), "port": 18100})
    a.builder.refresh(a.cfg, force=True)

    time.sleep(0.01)
    cfgmod.save_settings(cfgmod.load_settings())  # angefasst, inhaltlich gleich
    a._reload_if_changed()
    check(a.restarts == 0, "gleicher Inhalt -> kein Neustart")


def test_reload_survives_broken_file():
    """settings.json ist handeditierbar — ein Tippfehler darf die App nicht kippen."""
    _reset_cache()
    src = _TMP / f"live{time.monotonic_ns()}"
    _write(src, "Familie", [_contact("A", "Ina", "+49 30 2312505")])
    a = _HeadlessApp({**cfgmod.DEFAULTS, "accounts": ["Familie"],
                      "source_base": str(src), "port": 18100})
    time.sleep(0.01)
    cfgmod.SETTINGS_PATH.write_text("{kein json", encoding="utf-8")
    try:
        a._reload_if_changed()
        check(True, "kaputte settings.json wirft nicht durch")
    except Exception:
        check(False, "kaputte settings.json wirft nicht durch")
    check(a.cfg["accounts"] == ["Familie"], "load_settings fällt auf die Defaults zurück")


# -------------------------------------------------------------- Einstellungen ---

def _raw(**over):
    base = {"accounts": "Familie", "source_base": "/tmp/x", "bind": "0.0.0.0",
            "port": "8081", "basic_auth_user": "wp826", "password": "",
            "notify_enabled": False, "notify_to": "", "notify_from": "",
            "smtp_host": "localhost", "smtp_port": "2525"}
    base.update(over)
    return base


def test_parse_settings_ok():
    values, errors = ps.parse_settings(_raw(accounts=" Familie , Timo "))
    check(errors == [], "gültige Eingaben ergeben keine Fehler")
    check(values["accounts"] == ["Familie", "Timo"], "Accounts werden getrennt und getrimmt")
    check(values["port"] == 8081 and values["smtp_port"] == 2525, "Ports werden zu Zahlen")
    check("password" not in values, "das Passwort geht NICHT in die Konfigurationsdatei")


def test_parse_settings_ports():
    for bad in ("0", "65536", "abc", "", "-1"):
        _, errors = ps.parse_settings(_raw(port=bad))
        check(errors != [], f"Port {bad!r} wird abgelehnt")
    values, errors = ps.parse_settings(_raw(port="65535"))
    check(errors == [] and values["port"] == 65535, "65535 ist gültig")


def test_parse_settings_required_fields():
    for field in ("accounts", "source_base", "bind", "basic_auth_user"):
        _, errors = ps.parse_settings(_raw(**{field: "  "}))
        check(errors != [], f"leeres Feld {field!r} wird abgelehnt")


def test_parse_settings_empty_user_is_rejected():
    """Ohne Benutzer gäbe es keine Basic Auth — das Telefonbuch läge offen im LAN."""
    _, errors = ps.parse_settings(_raw(basic_auth_user=""))
    check(any("Benutzer" in e for e in errors), "leerer Benutzer wird namentlich gerügt")


def test_parse_settings_notify_needs_recipient():
    _, errors = ps.parse_settings(_raw(notify_enabled=True, notify_to=""))
    check(errors != [], "Fehler-E-Mail aktiv ohne Empfänger -> Fehler")
    values, errors = ps.parse_settings(_raw(notify_enabled=False, notify_to=""))
    check(errors == [], "inaktiv ohne Empfänger ist in Ordnung")
    check(values["notify_enabled"] is False, "der Schalter wird zu bool")


def test_parse_settings_smtp_host_fallback():
    values, _ = ps.parse_settings(_raw(smtp_host="  "))
    check(values["smtp_host"] == "localhost",
          "leerer Relay-Host fällt auf localhost zurück (nicht 127.0.0.1)")


def test_parse_settings_collects_all_errors():
    _, errors = ps.parse_settings(_raw(port="x", smtp_port="y", accounts=""))
    check(len(errors) >= 3, "alle Fehler auf einmal, nicht einer nach dem anderen")


if __name__ == "__main__":
    try:
        test_parse_settings_ok()
        test_parse_settings_ports()
        test_parse_settings_required_fields()
        test_parse_settings_empty_user_is_rejected()
        test_parse_settings_notify_needs_recipient()
        test_parse_settings_smtp_host_fallback()
        test_parse_settings_collects_all_errors()
        test_reload_picks_up_source_change_without_restart()
        test_reload_restarts_only_for_listener_fields()
        test_reload_ignores_touch_without_change()
        test_reload_survives_broken_file()
        test_build_and_cache()
        test_no_rebuild_when_unchanged()
        test_deletion_is_detected()
        test_guard_missing_mount_keeps_cache()
        test_guard_empty_source_keeps_cache()
        test_no_cache_and_no_source()
        test_http_auth()
        test_http_routes()
        test_http_serves_stale_cache_on_broken_source()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\n{len(PASS)} ok, {len(FAIL)} fehlgeschlagen")
    sys.exit(1 if FAIL else 0)
