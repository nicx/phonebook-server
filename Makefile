.PHONY: venv test report app alias clean install-dev guard-not-running

BUNDLE := dist/PhonebookServer.app
BINARY := $(CURDIR)/$(BUNDLE)/Contents/MacOS/PhonebookServer

VENV := .venv
PYTHON ?= /opt/homebrew/bin/python3.13
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-dev: venv
	$(PIP) install py2app

# Konverter (Mapping), Server (Guards + HTTP) und Notifier (Debounce).
# Kein Netz, kein Mailversand, keine echten Daten.
test:
	$(PY) tests/test_convert.py
	$(PY) tests/test_server.py
	$(PY) tests/test_notify.py

# Was kosten die Slot-Grenzen bei den echten Daten? Enthält echte Namen und
# Rufnummern -> nur auf den Bildschirm, nie ins Repo.
report:
	$(PY) convert.py --report

# Läuft die App aus genau diesem dist/, würde der Build ihr das Bundle unter den
# Füßen weglöschen. Der Prozess läuft danach aus einem gelöschten Bundle mit ALTEM
# Code weiter, macOS graut ihn aus, das Menü reagiert nicht mehr — beenden geht dann
# nur noch per `kill`. Aus /Applications gestartete Instanzen sind unkritisch.
#
# Dieselbe Falle gibt es bei icloud-sync (build/build.sh hat denselben Guard) und
# bei MailRelay.
guard-not-running:
	@PIDS="$$(pgrep -f '$(BINARY)' || true)"; \
	if [ -n "$$PIDS" ]; then \
	  echo "ABBRUCH: PhonebookServer läuft aus $(CURDIR)/dist (PID: $$PIDS)." >&2; \
	  echo "         Der Build würde das laufende Bundle löschen." >&2; \
	  echo "         Erst beenden (Menüleiste -> Beenden), dann erneut bauen." >&2; \
	  exit 1; \
	fi

# Echte, doppelklickbare .app bauen -> dist/PhonebookServer.app
#
# ACHTUNG: die App danach NICHT aus dem Terminal starten (kein `open`). Eine
# headless gestartete Instanz belegt den Port und ist ohne Menüleiste kaum wieder
# loszuwerden — dieselbe Falle wie bei MailRelay. Bauen, dann selbst per Doppelklick
# oder aus /Applications starten.
app: guard-not-running install-dev
	rm -rf build dist
	$(PY) setup.py py2app
	@echo "Fertig: dist/PhonebookServer.app — bitte per Doppelklick starten, nicht aus dem Terminal."

alias: guard-not-running install-dev
	$(PY) setup.py py2app -A
	@echo "Fertig (Alias): dist/PhonebookServer.app"

clean:
	rm -rf build dist $(VENV)
