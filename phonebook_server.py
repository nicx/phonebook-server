#!/usr/bin/env python3
"""phonebook-server — liefert iCloud-Kontakte als XML-Telefonbuch für IP-Telefone.

macOS-Menüleisten-App. Kein Sync: das Telefon pollt selbst (Grandstream P332), der
Fluss ist strikt einseitig. Die Kontakte kommen aus den Roh-JSONs, die **icloud-sync**
auf die Platte spiegelt — diese App liest nur, sie spricht nie mit iCloud.

    http://<mac>:8081/phonebook.xml   (Basic Auth)

Start:
    python3 phonebook_server.py
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import rumps

import convert
import notify
import settings as cfgmod

__version__ = "1.0.0"

APP_TITLE = "Phonebook"
URL_PATH = "/phonebook.xml"

LOGGER = logging.getLogger("phonebook-server")


def setup_logging():
    cfgmod.secure_dir(cfgmod.SUPPORT)
    cfgmod.secure_dir(cfgmod.LOGS_DIR)
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.setLevel(logging.INFO)
        h = RotatingFileHandler(cfgmod.LOG_PATH, maxBytes=1_000_000, backupCount=5)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(h)
    return LOGGER


# ------------------------------------------------------------------ Builder ---

class PhonebookBuilder:
    """Baut das XML bei Bedarf neu und hält es im Cache.

    Kein Scheduler: das Telefon fragt ohnehin nur alle paar Stunden. Bei jedem
    Request wird die jüngste mtime der Quelldateien mit der des Caches verglichen
    (~200 stat()-Aufrufe, vernachlässigbar) und nur bei echter Änderung neu gebaut.
    """

    def __init__(self, notifier=None):
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.contact_count = 0
        self.entry_count = 0
        # Optional: meldet source_broken/Recovery. Ohne Notifier bleibt alles still —
        # so bleiben Builder-Tests frei von Mailversand.
        self._notifier = notifier

    def _report(self, healthy: bool, detail: str = "") -> None:
        if self._notifier is not None:
            self._notifier.report("source_broken", healthy=healthy, detail=detail)

    # -- Quellen -------------------------------------------------------------
    def _account_dirs(self, cfg) -> list[Path]:
        base = Path(cfg["source_base"])
        return [base / a / "Contacts" for a in cfg["accounts"]]

    def _scan_sources(self, cfg) -> tuple[bool, float, int]:
        """Prüft die Quellen. Rückgabe: (vollständig, jüngste mtime, Anzahl Dateien).

        "vollständig" = jedes konfigurierte Account-Verzeichnis war lesbar. False
        heißt ausdrücklich "unbekannt", nicht "nichts geändert".

        Die mtime des **Verzeichnisses** zählt mit, nicht nur die der Dateien. Das ist
        der einzige Weg, Löschungen zu bemerken: icloud-sync ist ein Spiegel und
        entfernt die JSON eines in iCloud gelöschten Kontakts. Dabei ändert sich keine
        der verbleibenden Dateien — nur der Verzeichniseintrag. Ohne die Verzeichnis-
        mtime bliebe ein gelöschter Kontakt für immer im Telefonbuch stehen.
        """
        newest = 0.0
        count = 0
        for d in self._account_dirs(cfg):
            if not d.is_dir():
                return False, 0.0, 0
            try:
                newest = max(newest, d.stat().st_mtime)
                for f in d.glob("*.json"):
                    newest = max(newest, f.stat().st_mtime)
                    count += 1
            except OSError:
                return False, 0.0, 0
        return True, newest, count

    # -- Bauen ---------------------------------------------------------------
    def refresh(self, cfg, force=False) -> bool:
        """Baut das XML neu, wenn nötig. True = Cache ist (weiterhin) brauchbar.

        Guards — beide nach demselben Prinzip wie der prune-Schutz in icloud-sync:
        ein unvollständiger Blick auf die Quelle darf gute Daten nicht zerstören.

        1. Fehlt ein Account-Verzeichnis (SMB-Mount weg, icloud-sync mittendrin),
           wird NICHT gebaut. Sonst entstünde ein kürzeres Telefonbuch, das den
           guten Cache überschreibt — und das Telefon würde beim nächsten Poll
           stillschweigend die halbe Verwandtschaft verlieren.
        2. Null Kontakte ist ebenfalls kein Ergebnis, sondern ein Symptom.
        """
        with self._lock:
            complete, newest, n_files = self._scan_sources(cfg)
            if not complete:
                self.last_error = "Quelle nicht vollständig lesbar (Mount weg?)"
                LOGGER.warning("Kein Rebuild: %s", self.last_error)
                self._report(False, f"{self.last_error}\nQuelle: {cfg['source_base']}\n"
                                    f"Accounts: {', '.join(cfg['accounts'])}\n\n"
                                    "Das Telefonbuch bleibt auf dem letzten guten Stand.")
                return cfgmod.CACHE_PATH.exists()
            if n_files == 0:
                # Alle Verzeichnisse da, aber leer: kein Ergebnis, sondern ein Symptom.
                self.last_error = "Quelle enthält 0 Kontaktdateien — Cache behalten"
                LOGGER.warning("Kein Rebuild: %s", self.last_error)
                self._report(False, f"{self.last_error}\nQuelle: {cfg['source_base']}\n"
                                    f"Accounts: {', '.join(cfg['accounts'])}\n\n"
                                    "Das Telefonbuch bleibt auf dem letzten guten Stand.")
                return cfgmod.CACHE_PATH.exists()

            # Ab hier ist die Quelle nachweislich lesbar — unabhängig davon, ob
            # gleich gebaut wird. Die Entwarnung gehört genau hierher und NICHT
            # hinter den Rebuild: kommt der Mount zurück, ohne dass sich ein Kontakt
            # geändert hat, greift unten die Frische-Prüfung und springt raus. Die
            # Recovery-Mail käme dann nie, und die Bedingung bliebe für immer auf
            # "Problem".
            self._report(True)

            if not force and cfgmod.CACHE_PATH.exists():
                try:
                    if cfgmod.CACHE_PATH.stat().st_mtime >= newest:
                        return True  # Cache aktuell
                except OSError:
                    pass

            try:
                contacts = convert.dedupe(
                    convert.load_contacts(cfg["source_base"], cfg["accounts"]))
            except Exception as exc:  # noqa: BLE001 - Cache retten, nie den Server kippen
                self.last_error = f"Lesen fehlgeschlagen: {exc}"
                LOGGER.exception("Rebuild fehlgeschlagen")
                self._report(False, f"{self.last_error}\n\n"
                                    "Das Telefonbuch bleibt auf dem letzten guten Stand.")
                return cfgmod.CACHE_PATH.exists()

            if not contacts:
                self.last_error = "Quelle lieferte 0 Kontakte — Cache behalten"
                LOGGER.warning("Kein Rebuild: %s", self.last_error)
                self._report(False, f"{self.last_error}\n\n"
                                    "Das Telefonbuch bleibt auf dem letzten guten Stand.")
                return cfgmod.CACHE_PATH.exists()

            try:
                xml = convert.to_grandstream_xml(contacts)
                entries = xml.count(b"<Contact>")
                # Atomar ersetzen: ein Telefon, das mitten im Schreiben liest,
                # bekäme sonst ein halbes Dokument.
                tmp = cfgmod.CACHE_PATH.with_suffix(".xml.tmp")
                tmp.write_bytes(xml)
                os.chmod(tmp, cfgmod.FILE_MODE)
                tmp.replace(cfgmod.CACHE_PATH)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Schreiben fehlgeschlagen: {exc}"
                LOGGER.exception("Rebuild fehlgeschlagen")
                self._report(False, f"{self.last_error}\nZiel: {cfgmod.CACHE_PATH}")
                return cfgmod.CACHE_PATH.exists()

            self.contact_count = len(contacts)
            self.entry_count = entries
            self.last_error = None
            LOGGER.info("Telefonbuch gebaut: %d Kontakte -> %d Einträge, %d Bytes",
                        len(contacts), entries, len(xml))
            self._report(True)
            return True

    def read_cache(self) -> bytes | None:
        try:
            return cfgmod.CACHE_PATH.read_bytes()
        except OSError:
            return None


# ------------------------------------------------------------------- HTTP ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"phonebook-server/{__version__}"
    sys_version = ""  # Python-Version nicht ausplaudern

    # Vom Server gesetzt (siehe serve()).
    builder: PhonebookBuilder = None       # type: ignore[assignment]
    cfg: dict = None                       # type: ignore[assignment]
    password: str = ""

    def log_message(self, fmt, *args):  # BaseHTTPRequestHandler -> stderr, das ist in der .app weg
        LOGGER.info("%s %s", self.address_string(), fmt % args)

    # -- Auth ----------------------------------------------------------------
    def _authorized(self) -> bool:
        """Basic Auth. Grandstream kann für das Telefonbuch NUR Basic, kein Digest.

        Über unverschlüsseltes HTTP ist das Base64, kein Schutz gegen einen
        Angreifer im LAN — aber es hält beiläufiges Stöbern ab, und das Telefon
        speichert das Passwort ohnehin im Klartext. TLS wäre hier keine Option:
        das Gerät müsste einer privaten CA vertrauen.
        """
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pw = raw.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        # compare_digest gegen Timing-Vergleiche; beide Felder prüfen.
        ok_user = hmac.compare_digest(user, self.cfg.get("basic_auth_user", ""))
        ok_pw = hmac.compare_digest(pw, self.password)
        return ok_user and ok_pw

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="phonebook"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- Routen --------------------------------------------------------------
    def do_GET(self):
        self._respond(body=True)

    def do_HEAD(self):
        self._respond(body=False)

    def _respond(self, body: bool):
        if self.path.split("?", 1)[0] != URL_PATH:
            self.send_error(404, "Not Found")
            return
        if not self._authorized():
            self._deny()
            return

        self.builder.refresh(self.cfg)
        xml = self.builder.read_cache()
        if xml is None:
            # Einziger Fall, in dem das Telefon einen Fehler sieht: es gab noch nie
            # einen erfolgreichen Build, es ist also schlicht nichts da.
            LOGGER.error("Kein Telefonbuch vorhanden: %s", self.builder.last_error)
            self.send_error(503, "Phonebook not available")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(xml)))
        self.end_headers()
        if body:
            self.wfile.write(xml)


def serve(builder, cfg, password) -> ThreadingHTTPServer:
    Handler.builder = builder
    Handler.cfg = cfg
    Handler.password = password
    httpd = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ---------------------------------------------------------------- Menü-App ---

class PhonebookApp(rumps.App):
    def __init__(self):
        super().__init__(APP_TITLE, icon=None, title="☎", quit_button=None)
        self.log = setup_logging()
        self.cfg = cfgmod.load_settings()
        cfgmod.save_settings(self.cfg)  # Defaults sichtbar machen
        # settings_provider statt fester Werte: Änderungen an settings.json wirken
        # beim nächsten Reload, ohne die Instanz neu zu bauen.
        self.notifier = notify.NotifierState(lambda: self.cfg)
        self.builder = PhonebookBuilder(notifier=self.notifier)
        self.httpd = None

        self._report_stale_crash()

        self.status_item = rumps.MenuItem("Status: startet…")
        self.count_item = rumps.MenuItem("Kontakte: –")
        self.menu = [
            self.status_item,
            self.count_item,
            None,
            rumps.MenuItem("Jetzt neu bauen", callback=self.rebuild),
            rumps.MenuItem("Passwort setzen…", callback=self.set_password),
            None,
            rumps.MenuItem("Test-E-Mail senden", callback=self.send_test_mail),
            rumps.MenuItem("Log öffnen…", callback=self.open_log),
            rumps.MenuItem("Einstellungen öffnen…", callback=self.open_settings),
            None,
            rumps.MenuItem("Beenden", callback=self.quit_app),
        ]
        self.start()
        rumps.Timer(self.tick, 10).start()

    def _report_stale_crash(self):
        """Meldet einen unsauber beendeten Vorlauf — einmalig, ohne Zustandslogik.

        Liegt der Marker beim Start noch da, wurde die App beim letzten Mal nicht
        über "Beenden" verlassen (Absturz, Force Quit, harter Reboot). Der Marker
        wird gleich neu gesetzt, die Meldung kommt also genau einmal pro Vorfall.
        """
        stale = cfgmod.stale_crash_marker()
        if not stale:
            return
        self.log.warning("Letzter Lauf endete unsauber: %s", stale)
        self.notifier.notify_event(
            "Letzter Lauf endete unsauber",
            "Beim Start lag noch der Marker des Vorlaufs — die App wurde nicht über "
            "„Beenden“ verlassen (Absturz, Force Quit oder harter Reboot).\n\n"
            f"Vorlauf: {stale}\n\n"
            "Das Telefonbuch selbst nimmt keinen Schaden: der Cache liegt auf der "
            "Platte und wird beim Start neu geprüft.")

    def start(self):
        pw = cfgmod.get_password(self.cfg["basic_auth_user"])
        if not pw:
            # Lieber gar nicht lauschen als ungeschützt: das Telefonbuch enthält
            # die Kontakte der ganzen Familie.
            self.status_item.title = "Status: kein Passwort gesetzt"
            self.log.warning("Kein Keychain-Passwort für %s — Server nicht gestartet.",
                             self.cfg["basic_auth_user"])
            self.notifier.problem(
                "server_down",
                f"Für den Benutzer „{self.cfg['basic_auth_user']}“ liegt kein Passwort "
                "im Schlüsselbund. Der Server lauscht deshalb nicht — das Telefon "
                "bekommt „Connection refused“.\n\n"
                "Beheben: Menü → „Passwort setzen…“.")
            return
        self.builder.refresh(self.cfg, force=True)
        try:
            self.httpd = serve(self.builder, self.cfg, pw)
        except OSError as exc:
            self.status_item.title = f"Status: Port belegt ({self.cfg['port']})"
            self.log.error("Bind auf %s:%s fehlgeschlagen: %s",
                           self.cfg["bind"], self.cfg["port"], exc)
            self.notifier.problem(
                "server_down",
                f"Der Server konnte {self.cfg['bind']}:{self.cfg['port']} nicht "
                f"belegen: {exc}\n\n"
                "Das Telefon bekommt „Connection refused“. Meist läuft schon eine "
                "zweite Instanz — prüfen mit:\n"
                f"  lsof -nP -iTCP:{self.cfg['port']} -sTCP:LISTEN")
            return
        # Marker erst setzen, wenn wirklich gelauscht wird: sonst meldete ein
        # sauberer Abbruch beim Start später fälschlich einen Absturz.
        cfgmod.arm_crash_marker(int(self.cfg["port"]))
        self.notifier.healthy("server_down")
        self.log.info("Lauscht auf %s:%s%s", self.cfg["bind"], self.cfg["port"], URL_PATH)

    def tick(self, _):
        if self.httpd is None:
            return
        if self.builder.last_error:
            self.status_item.title = f"Status: {self.builder.last_error}"
        else:
            self.status_item.title = f"Status: läuft ({self.cfg['bind']}:{self.cfg['port']})"
        self.count_item.title = (
            f"Kontakte: {self.builder.contact_count} ({self.builder.entry_count} Einträge)")

    # -- Aktionen ------------------------------------------------------------
    def rebuild(self, _):
        self.builder.refresh(self.cfg, force=True)
        if self.builder.last_error:
            rumps.notification(APP_TITLE, "Bauen fehlgeschlagen", self.builder.last_error, sound=False)
        else:
            rumps.notification(APP_TITLE, "Telefonbuch gebaut",
                               f"{self.builder.contact_count} Kontakte", sound=False)

    def set_password(self, _):
        user = self.cfg["basic_auth_user"]
        suggestion = secrets.token_urlsafe(12)
        w = rumps.Window(
            title=f"{APP_TITLE} – Basic-Auth-Passwort",
            message=(f"Passwort für Benutzer „{user}“.\n"
                     "Dasselbe im WP826 unter Phone Book → Phone Book Management eintragen.\n"
                     "Der Vorschlag ist bereits eingetragen."),
            default_text=suggestion,
            ok="Speichern", cancel="Abbrechen", dimensions=(320, 24))
        r = w.run()
        if not r.clicked or not r.text.strip():
            return
        if cfgmod.set_password(user, r.text.strip()):
            rumps.notification(APP_TITLE, "Passwort gespeichert", "Server wird neu gestartet.", sound=False)
            self.restart()
        else:
            rumps.alert(APP_TITLE, "Passwort konnte nicht im Schlüsselbund gespeichert werden.")

    def restart(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.start()

    def open_log(self, _):
        cfgmod.LOG_PATH.touch(exist_ok=True)
        subprocess.run(["open", str(cfgmod.LOG_PATH)])

    def open_settings(self, _):
        cfgmod.save_settings(self.cfg)
        subprocess.run(["open", str(cfgmod.SETTINGS_PATH)])

    def send_test_mail(self, _):
        """Prüft den kompletten Mailweg bis zum MailRelay — ohne auf einen echten
        Fehler zu warten."""
        if not self.cfg.get("notify_to"):
            rumps.alert(APP_TITLE,
                        "Kein Empfänger gesetzt.\n\nIn den Einstellungen "
                        "„notify_to“ eintragen und „notify_enabled“ auf true setzen.")
            return
        ok = notify.send_mail(
            self.cfg.get("smtp_host", "localhost"), int(self.cfg.get("smtp_port", 2525)),
            self.cfg.get("notify_from") or self.cfg["notify_to"], self.cfg["notify_to"],
            "phonebook-server: Test-E-Mail",
            f"Der Mailweg funktioniert.\n\nRelay: {self.cfg.get('smtp_host')}:"
            f"{self.cfg.get('smtp_port')}\nServer: {self.cfg['bind']}:{self.cfg['port']}")
        if ok:
            rumps.notification(APP_TITLE, "Test-E-Mail verschickt",
                               self.cfg["notify_to"], sound=False)
        else:
            rumps.alert(APP_TITLE,
                        f"Test-E-Mail fehlgeschlagen.\n\nRelay "
                        f"{self.cfg.get('smtp_host')}:{self.cfg.get('smtp_port')} "
                        "nicht erreichbar? Details im Log.")

    def quit_app(self, _):
        if self.httpd is not None:
            self.httpd.shutdown()
        # Gewolltes Beenden ist kein Absturz und kein Problem: Marker weg, damit der
        # nächste Start nichts meldet, und die Bedingung still auf gesund — sonst
        # käme beim nächsten Start eine "lauscht wieder"-Mail für ein Problem, das
        # es nie gab.
        cfgmod.disarm_crash_marker()
        self.notifier.clear("server_down")
        self.log.info("Beendet über das Menü")
        rumps.quit_application()


if __name__ == "__main__":
    PhonebookApp().run()
