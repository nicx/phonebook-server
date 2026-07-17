#!/usr/bin/env python3
"""Tests für die Notifier-State-Machine (Debounce: Mail nur bei Zustandswechsel).

    .venv/bin/python tests/test_notify.py

Kein Netz, kein echter Mailversand — der Mailer wird injiziert.
Alle Adressen frei erfunden (Repo ist public).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    if not cond:
        print("FAIL: " + msg)


class FakeMailer:
    """Sammelt Sendeaufrufe; kann die ersten N Versuche scheitern lassen."""

    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_times = fail_times

    def __call__(self, host, port, sender, recipient, subject, body):
        self.calls.append({"host": host, "port": port, "sender": sender,
                           "recipient": recipient, "subject": subject, "body": body})
        if self._fail_times > 0:
            self._fail_times -= 1
            return False
        return True

    @property
    def subjects(self):
        return [c["subject"] for c in self.calls]


def _cfg(enabled=True, to="ops@example.org", sender="", host="localhost", port=2525):
    return {"notify_enabled": enabled, "notify_to": to, "notify_from": sender,
            "smtp_host": host, "smtp_port": port}


def _make(mailer, cfg=None, sleeps=None):
    cfg = cfg if cfg is not None else _cfg()
    return notify.NotifierState(
        settings_provider=lambda: cfg,
        mailer=mailer,
        desktop_notify=None,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


# ------------------------------------------------------------------ Debounce ---

def test_problem_mails_once():
    """Der Kern: eine anhaltende Störung darf nicht bei jedem Poll mailen."""
    m = FakeMailer()
    n = _make(m)
    for _ in range(5):
        n.problem("source_broken", "Mount weg")
    check(len(m.calls) == 1, "fünf Meldungen desselben Problems -> genau eine Mail")
    check(n.is_problem("source_broken"), "Zustand ist gemerkt")


def test_recovery_mails_once():
    m = FakeMailer()
    n = _make(m)
    n.problem("source_broken", "Mount weg")
    for _ in range(3):
        n.healthy("source_broken")
    check(len(m.calls) == 2, "Problem + Entwarnung = zwei Mails, nicht mehr")
    check("wieder lesbar" in m.subjects[1], "die zweite ist die Entwarnung")
    check(not n.is_problem("source_broken"), "Zustand ist zurückgesetzt")


def test_healthy_without_problem_stays_silent():
    """Der Normalfall: es läuft alles. Da darf nie eine Mail kommen."""
    m = FakeMailer()
    n = _make(m)
    for _ in range(10):
        n.healthy("source_broken")
    check(m.calls == [], "gesund ohne vorheriges Problem -> keine Mail")


def test_problem_recovery_problem_cycles():
    m = FakeMailer()
    n = _make(m)
    n.problem("source_broken", "1")
    n.healthy("source_broken")
    n.problem("source_broken", "2")
    check(len(m.calls) == 3, "jeder echte Wechsel mailt wieder")


def test_conditions_are_independent():
    m = FakeMailer()
    n = _make(m)
    n.problem("source_broken", "Mount weg")
    n.problem("server_down", "Port belegt")
    check(len(m.calls) == 2, "zwei verschiedene Bedingungen -> zwei Mails")
    n.healthy("source_broken")
    check(n.is_problem("server_down"), "die Entwarnung der einen lässt die andere in Ruhe")


def test_unknown_condition_is_ignored():
    m = FakeMailer()
    n = _make(m)
    n.problem("gibts_nicht", "…")
    check(m.calls == [], "unbekannte Bedingung mailt nicht (statt zu werfen)")


def test_clear_is_silent():
    """Gewolltes Beenden: kein 'wieder da' beim nächsten Start für ein Problem,
    das es nie gab."""
    m = FakeMailer()
    n = _make(m)
    n.problem("server_down", "Port belegt")
    m.calls.clear()
    n.clear("server_down")
    check(m.calls == [], "clear() schickt keine Entwarnung")
    check(not n.is_problem("server_down"), "setzt den Zustand trotzdem zurück")


def test_notify_event_has_no_state():
    m = FakeMailer()
    n = _make(m)
    n.notify_event("Letzter Lauf endete unsauber", "Details")
    n.notify_event("Letzter Lauf endete unsauber", "Details")
    check(len(m.calls) == 2, "One-Shots werden nicht entprellt — der Aufrufer entscheidet")


# ------------------------------------------------------------------- Versand ---

def test_mail_fields():
    m = FakeMailer()
    n = _make(m, _cfg(to="ops@example.org", host="localhost", port=2525))
    n.problem("source_broken", "Mount weg")
    c = m.calls[0]
    check(c["recipient"] == "ops@example.org", "Empfänger aus der Config")
    check(c["sender"] == "ops@example.org", "leerer Absender fällt auf den Empfänger zurück")
    check(c["host"] == "localhost" and c["port"] == 2525, "Relay aus der Config")
    check(c["subject"].startswith("phonebook-server: "), "Betreff ist als Absender erkennbar")
    check("Mount weg" in c["body"], "das Detail steht im Text")


def test_sender_can_differ():
    m = FakeMailer()
    n = _make(m, _cfg(to="ops@example.org", sender="mac@example.org"))
    n.problem("source_broken")
    check(m.calls[0]["sender"] == "mac@example.org", "expliziter Absender wird genutzt")


def test_disabled_and_missing_recipient():
    m = FakeMailer()
    _make(m, _cfg(enabled=False)).problem("source_broken")
    check(m.calls == [], "notify_enabled=false -> keine Mail")

    m2 = FakeMailer()
    _make(m2, _cfg(to="")).problem("source_broken")
    check(m2.calls == [], "kein Empfänger -> keine Mail")


def test_retry_then_success():
    m = FakeMailer(fail_times=2)
    sleeps = []
    n = _make(m, sleeps=sleeps)
    n.problem("source_broken")
    check(len(m.calls) == 3, "zwei Fehlschläge -> dritter Versuch")
    check(len(sleeps) == 2, "zwischen den Versuchen wird gewartet")


def test_retry_gives_up():
    m = FakeMailer(fail_times=99)
    n = _make(m, sleeps=[])
    n.problem("source_broken")
    check(len(m.calls) == notify._MAX_SEND_RETRIES, "nach N Versuchen ist Schluss")
    check(n.is_problem("source_broken"),
          "der Zustand bleibt gemerkt, auch wenn die Mail nicht rausging — sonst "
          "käme bei der nächsten Meldung erneut Dauerfeuer")


def test_mailer_exception_does_not_escape():
    """Eine kaputte Benachrichtigung darf den Betrieb nie kippen. report() läuft aus
    refresh() heraus im HTTP-Thread — eine Ausnahme von hier würde dem Telefon einen
    500 bescheren statt des Caches."""
    def boom(*a, **k):
        raise RuntimeError("Relay explodiert")
    n = notify.NotifierState(settings_provider=lambda: _cfg(), mailer=boom,
                             desktop_notify=None, sleep=lambda _s: None)
    try:
        n.problem("source_broken")
        check(True, "Ausnahme aus dem Mailer wird gefangen")
    except Exception:
        check(False, "Ausnahme aus dem Mailer wird gefangen")


def test_broken_settings_do_not_escape():
    """settings.json ist handeditierbar — ein Tippfehler im Port darf nicht das
    Telefonbuch abschießen."""
    m = FakeMailer()
    kaputt = {"notify_enabled": True, "notify_to": "ops@example.org",
              "smtp_host": "localhost", "smtp_port": "achtundzwanzig"}
    n = notify.NotifierState(settings_provider=lambda: kaputt, mailer=m,
                             desktop_notify=None, sleep=lambda _s: None)
    try:
        n.problem("source_broken")
        check(True, "unbrauchbarer smtp_port wirft nicht durch")
    except Exception:
        check(False, "unbrauchbarer smtp_port wirft nicht durch")
    check(m.calls == [], "und es wird nichts verschickt")


def test_settings_provider_exception_does_not_escape():
    def boom():
        raise RuntimeError("Config weg")
    n = notify.NotifierState(settings_provider=boom, mailer=FakeMailer(),
                             desktop_notify=None, sleep=lambda _s: None)
    try:
        n.problem("source_broken")
        check(True, "kaputter settings_provider wirft nicht durch")
    except Exception:
        check(False, "kaputter settings_provider wirft nicht durch")


if __name__ == "__main__":
    test_problem_mails_once()
    test_recovery_mails_once()
    test_healthy_without_problem_stays_silent()
    test_problem_recovery_problem_cycles()
    test_conditions_are_independent()
    test_unknown_condition_is_ignored()
    test_clear_is_silent()
    test_notify_event_has_no_state()
    test_mail_fields()
    test_sender_can_differ()
    test_disabled_and_missing_recipient()
    test_retry_then_success()
    test_retry_gives_up()
    test_mailer_exception_does_not_escape()
    test_broken_settings_do_not_escape()
    test_settings_provider_exception_does_not_escape()

    print(f"\n{len(PASS)} ok, {len(FAIL)} fehlgeschlagen")
    sys.exit(1 if FAIL else 0)
