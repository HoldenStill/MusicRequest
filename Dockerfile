# ── Stage 1: Standalone Static FFmpeg Binary ─────────────────────────────────
FROM mwader/static-ffmpeg:7.1 AS ffmpeg-source

# ── Stage 2: Build & Optimize Python Dependencies (Alpine) ───────────────────
FROM python:3.12-alpine AS builder

# Install build toolchain and C header libraries
RUN apk add --no-cache \
    git \
    gcc \
    musl-dev \
    libffi-dev \
    zlib-dev \
    jpeg-dev \
    binutils

WORKDIR /build

COPY requirements.txt .

# 1. Build and install Python packages in a single deterministic atomic step
RUN set -eux; \
    pip install \
        --no-cache-dir \
        --no-compile \
        --no-binary Pillow \
        --prefix=/install \
        -r requirements.txt; \
    # Remove all package test suites & Cryptodome test vectors (~15MB saved)
    find /install -type d \( -name "tests" -o -name "test" -o -name "testing" -o -name "SelfTest" \) -exec rm -rf {} + 2>/dev/null || true; \
    # Remove metadata and egg/dist-info (~5MB saved)
    find /install -type d \( -name "*.dist-info" -o -name "*.egg-info" \) -exec rm -rf {} + 2>/dev/null || true; \
    # Prune pygments lexers: keep only core base and python lexer (~15MB saved)
    if [ -d /install/lib/python3.12/site-packages/pygments/lexers ]; then \
        find /install/lib/python3.12/site-packages/pygments/lexers/ -type f ! -name "__init__.py" ! -name "python.py" ! -name "_*.py" -delete 2>/dev/null || true; \
    fi; \
    # Remove any stray bytecode / caches (~30MB saved)
    find /install -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true; \
    find /install -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null || true; \
    # Strip debug symbols from Rust (pydantic_core) and C extensions (.so files)
    find /install -type f -name "*.so*" -exec strip --strip-unneeded {} + 2>/dev/null || true; \
    # Normalize all file and directory timestamps to a fixed Unix epoch for 100% deterministic SHA256 layer hash
    find /install -exec touch -d "2025-01-01T00:00:00Z" {} + 2>/dev/null || true


# ── Stage 3: Final Ultra-Lean Runtime Image ──────────────────────────────────
FROM python:3.12-alpine

# Install minimal runtime libraries & setup non-root user (UID/GID 568)
RUN apk add --no-cache libjpeg-turbo libffi libgcc && \
    addgroup -g 568 -S apps && \
    adduser -u 568 -S apps -G apps -s /bin/sh && \
    mkdir -p /music /config /app && \
    chown -R 568:568 /music /config /app

# Copy standalone static ffmpeg / ffprobe binaries (~13MB total, cached permanently)
COPY --from=ffmpeg-source /ffmpeg /ffprobe /usr/local/bin/

# Copy ultra-lean Python environment (~18-20MB total, deterministic hash cached permanently)
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code with non-root ownership directly (ONLY layer that changes on code edits)
COPY --chown=568:568 app/ ./app/

# Switch to non-root user
USER 568:568

# Volume mount points:
#   /music  — server download destination (map to NAS dataset)
#   /config — streamrip config.toml location
VOLUME ["/music", "/config"]

# Environment variable defaults
ENV STREAMRIP_CONFIG_PATH=/config/config.toml \
    MUSIC_DIR=/music \
    APP_PASSCODE=1099 \
    SEARCH_LIMIT=10 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
