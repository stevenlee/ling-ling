# ling-ling — dev workflow + blog delivery.
#
# Dev quality gates (P0 of the refactor roadmap):
#   make check      lint + typecheck + fast tests (the pre-merge gate)
#   make lint       ruff check
#   make format     ruff format (run on files you touched; no repo-wide big bang)
#   make typecheck  mypy (legacy modules exempted in pyproject; list only shrinks)
#   make test       full suite
#   make test-fast  skip tests marked slow
#
# Mermaid render check (opt-in — needs Node; targets the private vault):
#   make validate-mermaid            check lings-desktop/pages
#   make validate-mermaid MMD=path   check a specific file/dir
#
# Blog delivery:
#   make blog   transform lings-desktop/Blog/ -> $(KAFU)/content/
#
# This is the "push" half: ling-ling owns producing + shaping the content and
# delivers finished Quartz markdown into kafu's content/. kafu never reaches
# back into ling-ling. Override the target repo with `make blog KAFU=/path`.

KAFU ?= $(HOME)/projects/kafu
PY := venv/bin

check: lint typecheck test-fast

lint:
	$(PY)/ruff check .

format:
	$(PY)/ruff format .

typecheck:
	$(PY)/mypy

test:
	$(PY)/pytest -q

test-fast:
	$(PY)/pytest -q -m "not slow"

install-dev:
	$(PY)/pip install -e ".[tui,reranker,dev]"
	$(PY)/pre-commit install

blog:
	$(PY)/python System_Engine/services/blog_transform.py --content $(KAFU)/content

# Parse every ```mermaid block with the real engine; exit non-zero on any
# failure. Installs the Node deps into scripts/node_modules on first run.
MMD ?= lings-desktop/pages
validate-mermaid:
	@[ -d scripts/node_modules/mermaid ] || (cd scripts && npm install --no-fund --no-audit)
	node scripts/validate_mermaid.mjs $(MMD)

.PHONY: check lint format typecheck test test-fast install-dev blog validate-mermaid
