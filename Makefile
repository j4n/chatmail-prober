.PHONY: install install-dev test clean

VENV   ?= .venv
PYTHON ?= python3

install: $(VENV)/bin/pip
	$(VENV)/bin/pip install ./cmping-src
	$(VENV)/bin/pip install .

install-dev: $(VENV)/bin/pip
	$(VENV)/bin/pip install ./cmping-src
	$(VENV)/bin/pip install -e '.[dev]'

$(VENV)/bin/pip:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip

test: install-dev
	$(VENV)/bin/pytest tests/ --ignore=tests/test_live.py

clean:
	rm -rf $(VENV) *.egg-info dist
