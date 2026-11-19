ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# Match CI: mosquitto broker + openssl for integration/TLS tests
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         mosquitto \
         openssl \
         procps \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the uv in .github/workflows/test.yml. It is also the oldest that
# reads uv.lock as committed (lock revision 2); an older one rewrites it.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

# UV_PYTHON_DOWNLOADS=never keeps the resolve on THIS image's interpreter.
# Without it uv may fetch its own Python, and every row of the matrix then
# silently tests the same version instead of the one it is named for.
# The environment lives OUTSIDE /app because `make test-dev` bind-mounts the
# host tree over /app; a venv under it would be shadowed and pytest would
# vanish. uv installs the root project editable, so the bind mount still puts
# host source behind the import.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/swarmbus-venv \
    PATH="/opt/swarmbus-venv/bin:$PATH"

WORKDIR /app

# Deps first for layer caching. uv.lock is what makes this gate agree with
# CI: resolving from pyproject ranges instead lets each build pick its own
# dependency versions, so a local pass says nothing about CI.
COPY pyproject.toml uv.lock README.md CHANGELOG.md ./
COPY src/ src/
RUN uv sync --frozen --all-extras

COPY tests/ tests/
COPY scripts/ scripts/
COPY docs/ docs/
COPY examples/ examples/
COPY systemd/ systemd/
COPY .github/ .github/
COPY .claude-plugin/ .claude-plugin/
COPY plugins/ plugins/

CMD ["uv", "run", "--frozen", "pytest", "-v", "--tb=short"]
