#!/usr/bin/env python3
"""Konfiguration + Keychain-Zugriff für phonebook-server.

Bewusst geteilt zwischen CLI (``convert.py``) und Server (``phonebook_server.py``),
damit ein ``--report``-Lauf garantiert dieselben Accounts sieht wie der laufende Dienst.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path

LOGGER = logging.getLogger(__name__)

APP_NAME = "phonebook-server"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
SETTINGS_PATH = SUPPORT / "settings.json"
CACHE_PATH = SUPPORT / "phonebook.xml"
LOGS_DIR = SUPPORT / "logs"
LOG_PATH = LOGS_DIR / f"{APP_NAME}.log"

DIR_MODE = 0o700
FILE_MODE = 0o600

DEFAULTS = {
    # Reihenfolge = Priorität beim Dedup (erster Treffer gewinnt).
    "accounts": ["Familie"],
    "source_base": "/Volumes/macmini_data/iCloudSync",
    # 0.0.0.0, damit das WLAN-Telefon drankommt; reines Loopback wäre nutzlos.
    "bind": "0.0.0.0",
    "port": 8081,
    "basic_auth_user": "wp826",
}


def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, DIR_MODE)


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            cfg.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            LOGGER.warning("settings.json unlesbar, nutze Defaults: %s", exc)
    return cfg


def save_settings(cfg: dict) -> None:
    secure_dir(SUPPORT)
    SETTINGS_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _chmod(SETTINGS_PATH, FILE_MODE)


# ---------------------------------------------------------------- Keychain ---
# Zugriff über das Apple-signierte /usr/bin/security statt in-process (keyring).
# Grund (wie in icloud-sync, dort Fallstrick #4): in-process bindet macOS das
# "Immer erlauben" an die Code-Identität der App. Die ist hier self-signed ohne
# Team-ID und ändert sich bei JEDEM Rebuild (neuer cdhash) -> nach jedem Update
# ein Keychain-Prompt. `security` hat eine stabile Identität; mit `-T` als Trust-
# Accessor angelegt hält "Immer erlauben" dauerhaft.
#
# Tradeoff: `add-generic-password -w <wert>` stellt das Passwort kurz in argv.
# Das trifft nur das (seltene, interaktive) Setzen, nicht das Lesen — und wiegt
# hier leichter als ein Prompt nach jedem Rebuild. Das Geheimnis ist ohnehin nur
# ein LAN-Basic-Auth-Passwort, das das Telefon im Klartext speichert.

SECURITY = "/usr/bin/security"
KEYCHAIN_SERVICE = APP_NAME
_B64_PREFIX = "b64:"


def _run(args: list[str]):
    try:
        return subprocess.run([SECURITY, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("security-Aufruf fehlgeschlagen: %s", exc)
        return None


def set_password(account: str, password: str) -> bool:
    """Legt das Basic-Auth-Passwort an. Erst löschen, dann neu: nur so wird die ACL
    frisch auf `-T /usr/bin/security` gesetzt (ein Update behielte die alte)."""
    if not account:
        return False
    payload = _B64_PREFIX + base64.b64encode(password.encode("utf-8")).decode("ascii")
    _run(["delete-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE])
    res = _run(["add-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE,
                "-w", payload, "-T", SECURITY, "-U"])
    ok = res is not None and res.returncode == 0
    if not ok:
        LOGGER.warning("Keychain-Eintrag für %s konnte nicht gespeichert werden.", account)
    return ok


def get_password(account: str) -> str | None:
    """Liest das Basic-Auth-Passwort. None = nicht gesetzt/nicht lesbar.

    Defensiv: nie werfen. Der Aufrufer entscheidet, was ein fehlendes Passwort
    bedeutet (der Server verweigert dann den Start, statt ungeschützt zu lauschen).
    """
    if not account:
        return None
    res = _run(["find-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w"])
    if res is None or res.returncode != 0:
        return None
    raw = res.stdout
    raw = raw[:-1] if raw.endswith("\n") else raw  # -w hängt ein \n an
    if raw.startswith(_B64_PREFIX):
        try:
            return base64.b64decode(raw[len(_B64_PREFIX):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return raw or None
