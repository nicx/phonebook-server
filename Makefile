.PHONY: venv test report app alias clean install-dev

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

# Echte, doppelklickbare .app bauen -> dist/PhonebookServer.app
#
# ACHTUNG: die App danach NICHT aus dem Terminal starten (kein `open`). Eine
# headless gestartete Instanz belegt den Port und ist ohne Menüleiste kaum wieder
# loszuwerden — dieselbe Falle wie bei MailRelay. Bauen, dann selbst per Doppelklick
# oder aus /Applications starten.
app: install-dev
	$(PY) setup.py py2app
	@echo "Fertig: dist/PhonebookServer.app — bitte per Doppelklick starten, nicht aus dem Terminal."

alias: install-dev
	$(PY) setup.py py2app -A
	@echo "Fertig (Alias): dist/PhonebookServer.app"

clean:
	rm -rf build dist $(VENV)
