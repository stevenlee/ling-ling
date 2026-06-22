# ling-ling — deliver curated reviews into the kafu blog repo.
#
#   make blog   transform lings-desktop/Blog/ -> $(KAFU)/content/
#
# This is the "push" half: ling-ling owns producing + shaping the content and
# delivers finished Quartz markdown into kafu's content/. kafu never reaches
# back into ling-ling. Override the target repo with `make blog KAFU=/path`.

KAFU ?= $(HOME)/projects/kafu

blog:
	venv/bin/python System_Engine/services/blog_transform.py --content $(KAFU)/content

.PHONY: blog
