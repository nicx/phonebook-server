#!/usr/bin/env python3
"""Login-Autostart über einen LaunchAgent (In-App-Toggle).

Schreibt/entfernt ein ``~/Library/LaunchAgents``-plist, das die App beim Login startet.
Portiert aus ``icloud-sync/src/autostart.py``.

Bewusst ein LaunchAgent statt ``SMAppService``: kommt ohne registrierten Helfer aus und
funktioniert auch für ein ad-hoc-signiertes Eigengebrauch-Bundle zuverlässig.

Bewusst **ohne** ``KeepAlive``: das ist das Muster der Menüleisten-Apps hier (evcc,
icloud-sync). KeepAlive würde gegen „Beenden" im Menü ankämpfen — launchd startete die
App sofort wieder. Nur Hintergrunddienste (Caddy, evcc-Agent) haben es.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

LOGGER = logging.getLogger(__name__)

LABEL = "de.nicx.phonebook-server"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_enabled() -> bool:
    return plist_path().exists()


def program_args() -> Optional[list[str]]:
    """Programmargumente für den LaunchAgent. ``None`` außerhalb des Bundles.

    py2app setzt ``sys.frozen``. **ACHTUNG:** ``sys.executable`` zeigt im Bundle auf
    ``…/Contents/MacOS/python`` — den eingebetteten Interpreter, NICHT den
    App-Loader-Stub (``CFBundleExecutable``). Würde der LaunchAgent ``python`` direkt
    starten, käme nur ein nackter Interpreter hoch und die Menüleisten-App erschiene
    nie. Also den echten Bundle-Executable auflösen; der Stub triggert ``__boot__``
    und damit denselben Start wie ein Doppelklick.

    (Fallstrick 1:1 aus icloud-sync übernommen — dort steht er kommentiert, weil er
    schon einmal zugeschlagen hat.)
    """
    if not getattr(sys, "frozen", False):
        return None
    macos_dir = os.path.dirname(sys.executable)              # …/Contents/MacOS
    bundle = os.path.dirname(os.path.dirname(macos_dir))     # …/PhonebookServer.app
    exe_name = os.path.splitext(os.path.basename(bundle))[0]  # Default: App-Name
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
            exe_name = plistlib.load(fh).get("CFBundleExecutable") or exe_name
    except (OSError, plistlib.InvalidFileException):
        pass
    return [os.path.join(macos_dir, exe_name)]


def enable(args: Sequence[str]) -> None:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": list(args),
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    with open(path, "wb") as fh:
        plistlib.dump(payload, fh)
    _launchctl("load", str(path))
    LOGGER.info("Autostart aktiviert: %s", list(args))


def disable() -> None:
    """Idempotent — auch wenn die plist gar nicht da ist."""
    path = plist_path()
    if path.exists():
        _launchctl("unload", str(path))
        try:
            path.unlink()
        except OSError as exc:
            LOGGER.warning("LaunchAgent-plist nicht löschbar: %s", exc)
    LOGGER.info("Autostart deaktiviert")


def _launchctl(action: str, path: str) -> None:
    """Best-effort ``launchctl load/unload`` — Fehler werden nur geloggt.

    Die plist allein genügt fürs nächste Login; ``launchctl`` macht es nur sofort
    wirksam. Ein Fehlschlag darf die Einstellung deshalb nicht kippen.
    """
    try:
        subprocess.run(["launchctl", action, "-w", path],
                       check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("launchctl %s fehlgeschlagen: %s", action, exc)
