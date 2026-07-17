#!/usr/bin/env python3
"""Benachrichtigungen: Desktop + Mail über das lokale MailRelay.

Zwei Teile:

* :func:`send_mail` — Transport, einfaches SMTP ohne Auth/TLS ans lokale Relay
  (Projekt **MailRelay**), das Upstream-Auth, STARTTLS und Retry übernimmt.
* :class:`NotifierState` — Entprellung. Portiert aus ``evcc/src/notifier_state.py``;
  die Swift-Geschwister (home-assistant/Notifier.swift) verweisen auf dasselbe Muster.

Gemailt wird **nur bei einem Zustandswechsel**: eine Mail gesund→Problem, eine
Mail Problem→gesund. Eine anhaltende Störung schweigt — sonst käme bei jedem
Telefon-Poll eine Mail.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

LOGGER = logging.getLogger(__name__)

# Bedingung -> (Problem-Betreff, Recovery-Betreff).
CONDITIONS: dict[str, tuple[str, str]] = {
    "source_broken": ("Telefonbuch-Quelle nicht lesbar",
                      "Telefonbuch-Quelle wieder lesbar"),
    "server_down": ("Telefonbuch-Server lauscht nicht",
                    "Telefonbuch-Server lauscht wieder"),
}

_MAX_SEND_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0


def notify(title: str, message: str) -> None:
    """macOS-Notification; best-effort, schluckt Fehler."""
    try:
        import rumps

        rumps.notification(title=title, subtitle="", message=message)
    except Exception as exc:  # noqa: BLE001 - Notifications sind best-effort
        LOGGER.debug("Desktop-Notification fehlgeschlagen: %s", exc)


def send_mail(host: str, port: int, sender: str, recipient: str, subject: str,
              body: str, timeout: float = 15.0) -> bool:
    """Liefert eine Mail per einfachem SMTP an das lokale Relay ein (kein Auth/TLS).

    Best-effort: Fehler werden geloggt, nicht geworfen — eine nicht zustellbare
    Benachrichtigung darf den Telefonbuch-Betrieb nie beeinflussen.
    """
    try:
        import smtplib
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="phonebook-server.local")
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Mail an %s über %s:%s fehlgeschlagen: %s", recipient, host, port, exc)
        return False


class NotifierState:
    """Hält je Bedingung den Zustand (gesund/Problem) und mailt nur bei Wechsel.

    :param settings_provider: Callable, das die aktuellen Settings als dict liefert —
        so wirken Änderungen sofort, ohne die Instanz neu zu bauen.
    :param mailer: Mail-Transport, für Tests injizierbar.
    :param desktop_notify: Notification-Callback, für Tests injizierbar.
    :param sleep: Verzögerung für den Sende-Retry, für Tests injizierbar.
    """

    def __init__(
        self,
        settings_provider: Callable[[], dict],
        mailer: Callable[..., bool] = send_mail,
        desktop_notify: Optional[Callable[[str, str], None]] = notify,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings_provider = settings_provider
        self._mailer = mailer
        self._desktop_notify = desktop_notify
        self._sleep = sleep
        self._problem: dict[str, bool] = {}

    def is_problem(self, condition: str) -> bool:
        return self._problem.get(condition, False)

    def report(self, condition: str, healthy: bool, detail: str = "") -> None:
        """Meldet den Zustand einer Bedingung; mailt nur bei echtem Wechsel."""
        if condition not in CONDITIONS:
            LOGGER.debug("Unbekannte Notifier-Bedingung ignoriert: %s", condition)
            return
        if healthy:
            self._on_healthy(condition)
        else:
            self._on_problem(condition, detail)

    def problem(self, condition: str, detail: str = "") -> None:
        self.report(condition, healthy=False, detail=detail)

    def healthy(self, condition: str) -> None:
        self.report(condition, healthy=True)

    def clear(self, condition: str) -> None:
        """Setzt eine Bedingung still auf gesund — OHNE Recovery-Mail.

        Für gewolltes Beenden: wer den Server selbst stoppt, will keine
        "wieder da"-Mail, wenn er ihn später startet.
        """
        self._problem[condition] = False

    def notify_event(self, subject: str, body: str = "") -> bool:
        """Einmalige Meldung ohne Zustandslogik (z. B. "letzter Lauf endete unsauber").

        Der Aufrufer ist selbst für Entprellung zuständig.
        """
        return self._emit(subject, body or subject)

    # -- intern --------------------------------------------------------------
    def _on_problem(self, condition: str, detail: str) -> None:
        if self._problem.get(condition):
            return  # schon im Problemzustand -> Debounce
        self._problem[condition] = True
        subject = CONDITIONS[condition][0]
        body = subject if not detail else f"{subject}\n\n{detail}"
        LOGGER.warning("Notifier: Problem '%s' (%s)", condition, detail or "ohne Detail")
        self._emit(subject, body)

    def _on_healthy(self, condition: str) -> None:
        if not self._problem.get(condition):
            return  # war nie im Problemzustand -> keine Recovery-Mail
        self._problem[condition] = False
        subject = CONDITIONS[condition][1]
        LOGGER.info("Notifier: Recovery '%s'", condition)
        self._emit(subject, subject)

    def _emit(self, subject: str, body: str) -> bool:
        """Verschickt Notification + Mail. **Wirft nie.**

        Die Kapselung ist nicht kosmetisch: `report()` wird aus `refresh()` heraus
        aufgerufen, und das läuft im HTTP-Thread. Eine Ausnahme von hier würde dem
        Telefon einen 500 bescheren, statt ihm den Cache auszuliefern — und damit
        genau die Invariante brechen, für die der ganze Cache existiert. Eine
        kaputte Benachrichtigung darf den Betrieb nie beeinflussen.

        Realistischer Auslöser: settings.json ist handeditierbar, ein
        ``"smtp_port": "abc"`` reicht für einen ValueError.
        """
        try:
            return self._emit_unsafe(subject, body)
        except Exception as exc:  # noqa: BLE001 - Benachrichtigung ist best-effort
            LOGGER.warning("Benachrichtigung fehlgeschlagen (%s): %s", subject, exc)
            return False

    def _emit_unsafe(self, subject: str, body: str) -> bool:
        cfg = self._settings_provider()
        if self._desktop_notify is not None:
            self._desktop_notify("Phonebook", subject)
        if not cfg.get("notify_enabled") or not cfg.get("notify_to"):
            LOGGER.debug("Mail nicht aktiv/kein Empfänger -> nur Desktop-Notification")
            return False
        sender = cfg.get("notify_from") or cfg["notify_to"]
        host, port = cfg.get("smtp_host", "localhost"), int(cfg.get("smtp_port", 2525))
        for attempt in range(1, _MAX_SEND_RETRIES + 1):
            if self._mailer(host, port, sender, cfg["notify_to"],
                            f"phonebook-server: {subject}", body):
                return True
            LOGGER.warning("Mail-Versuch %d/%d fehlgeschlagen", attempt, _MAX_SEND_RETRIES)
            if attempt < _MAX_SEND_RETRIES:
                self._sleep(_RETRY_BACKOFF_SECONDS)
        LOGGER.error("Mail endgültig nicht zustellbar: %s", subject)
        return False
