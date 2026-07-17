"""
py2app-Build-Skript für phonebook-server.

Standalone-App bauen:
    python3 setup.py py2app

Schneller Alias-Build (nur lokal lauffähig, kein Verteilen):
    python3 setup.py py2app -A
"""
from setuptools import setup

APP = ["phonebook_server.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    # convert.py und settings.py sind Geschwister-Module, keine Pakete -> py2app
    # findet sie über die Imports in phonebook_server.py automatisch.
    "packages": ["rumps"],
    "plist": {
        # py2app leitet den Bundle-NAMEN aus CFBundleName ab -> dist/PhonebookServer.app.
        # Bewusst ohne Leerzeichen, damit Pfade (LaunchAgent, /Applications) simpel
        # bleiben; der angezeigte Name darf ruhig hübscher sein.
        "CFBundleName": "PhonebookServer",
        "CFBundleDisplayName": "Phonebook Server",
        "CFBundleIdentifier": "de.nicx.phonebook-server",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        # Menüleisten-App: kein Dock-Icon, kein App-Switcher-Eintrag
        "LSUIElement": True,
        "NSHumanReadableCopyright": "MIT License",
    },
}

setup(
    app=APP,
    name="PhonebookServer",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
    # Kein install_requires – py2app 0.28.x lehnt das ab.
    # Laufzeit-Abhängigkeiten stehen in requirements.txt.
)
