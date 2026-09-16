ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# Match CI: mosquitto broker + openssl for integration/TLS tests
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         mosquitto \
         openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir -e ".[mcp,dev]"

COPY tests/ tests/
COPY scripts/ scripts/

CMD ["pytest", "-v", "--tb=short"]
