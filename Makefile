.PHONY: test test-one test-dev test-matrix build clean plugin-skill

PYTHON_VERSION ?= 3.12

build:
	PYTHON_VERSION=$(PYTHON_VERSION) docker compose build

test: test-matrix

test-one: build
	PYTHON_VERSION=$(PYTHON_VERSION) docker compose run --rm test

test-dev:
	PYTHON_VERSION=$(PYTHON_VERSION) docker compose run --rm -v "$$(pwd):/app" test

test-matrix:
	@for v in 3.10 3.11 3.12 3.13; do \
		printf '\n=== Python %s ===\n\n' "$$v"; \
		PYTHON_VERSION=$$v docker compose build test && \
		PYTHON_VERSION=$$v docker compose run --rm test || exit 1; \
	done

clean:
	docker compose down --rmi local --volumes --remove-orphans

# The Claude Code plugin carries a copy of the packaged skill (the plugin directory refuses symlinks).
plugin-skill:
	cp src/swarmbus/skills/using-swarmbus/SKILL.md plugins/swarmbus/skills/using-swarmbus/SKILL.md
