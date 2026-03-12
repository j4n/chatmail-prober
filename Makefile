.PHONY: install install-dev test clean

# uv manages the venv and lockfile; scripts in .venv/bin/ get shebangs
# pointing to the local Python, so the venv must be created on the machine
# that will run the prober.  Never copy .venv between hosts.
#
# If uv is not yet installed, the install targets will fetch and run
# the installer from https://astral.sh/uv, then invoke the binary
# directly from its install location (~/.local/bin/uv).

UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

$(HOME)/.local/bin/uv:
	curl -LsSf https://astral.sh/uv/install.sh | sh

install: $(UV)
	$(UV) sync

install-dev: $(UV)
	$(UV) sync --dev

test:
	$(UV) run python -m pytest tests/ --ignore=tests/test_live.py

clean:
	rm -rf .venv *.egg-info dist
