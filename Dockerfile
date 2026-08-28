FROM python:3.12-slim AS base

# Install ffmpeg (required by streamrip for audio conversion)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg git && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user with UID/GID 568 (TrueNAS SCALE apps dataset)
RUN groupadd -g 568 apps && \
    useradd -u 568 -g 568 -m -s /bin/bash apps

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create mount points with correct ownership
RUN mkdir -p /music /config && \
    chown -R 568:568 /music /config /app

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
    SEARCH_LIMIT=10

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
