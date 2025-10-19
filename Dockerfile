# syntax=docker/dockerfile:1.6
#
# Multi-stage Dockerfile for iOS Network Backup "app"
# Includes:
#  - libimobiledevice + utilities (idevicepair, idevicebackup2, etc.)
#  - netmuxd (downloaded prebuilt release asset)
#  - FastAPI app runtime (Python)
#  - Non-root execution
#
# Build args:
#   NETMUXD_VERSION  (default v0.3.0)
#   BASE_IMAGE       (default debian:stable-slim)
#   TARGETARCH       (provided automatically when using buildx: amd64 / arm64)
#   LIBIMOBILEDEVICE_PKG (runtime package name; e.g. libimobiledevice-1.0-6)
#
# Usage (simple):
#   docker build -t ios-backup .
#
# Multi-arch (example):
#   docker buildx build --platform linux/amd64,linux/arm64 -t yourrepo/ios-backup:latest .
#
# Run (pairing enabled):
#   docker run -d \
#     --name ios-backup \
#     -p 8080:8080 \
#     -v ios_backups:/data/backups \
#     -v ios_lockdown:/data/lockdown \
#     -v ios_config:/data/config \
#     --device /dev/bus/usb \
#     --restart unless-stopped \
#     ios-backup:latest
#
# Environment variables you can override at runtime:
#   APP_MODE=full | backup-only
#   PORT=8080
#   BACKUP_DIR=/data/backups
#   LOCKDOWN_DIR=/var/lib/lockdown
#   OPENSSL_WEAK_CONFIG=/data/config/openssl-weak.conf
#   USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015
#   START_USBMUXD=true|false
#   USBMUXD_FLAGS (default "-f")
#   USBMUXD_SOCKET_PATH (/var/run/usbmuxd)
#
# If building for an unsupported architecture asset, it will fall back to source build.

ARG BASE_IMAGE=python:3.12-slim
ARG NETMUXD_VERSION=v0.3.0
ARG LIBIMOBILEDEVICE_PKG=libimobiledevice-1.0-6

############################
# Builder Stage
############################
FROM ${BASE_IMAGE} AS builder

ARG NETMUXD_VERSION
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive

# Install prerequisites for downloading / optional compiling netmuxd
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget jq curl file git openssl \
    libssl-dev build-essential pkg-config \
    libplist-dev libusbmuxd-dev libimobiledevice-dev \
    && rm -rf /var/lib/apt/lists/*

# Determine netmuxd asset name based on architecture
# Official release assets pattern:
#   - x86_64:   netmuxd-x86_64-linux-gnu
#   - arm64:    netmuxd-aarch64-linux-gnu
# Fallback: build from source if unknown arch or asset missing.
RUN set -eux; \
    if [ "$TARGETARCH" = "amd64" ]; then \
    export NETMUXD_ASSET="netmuxd-x86_64-linux-gnu"; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
    export NETMUXD_ASSET="netmuxd-aarch64-linux-gnu"; \
    else \
    export NETMUXD_ASSET=""; \
    fi; \
    if [ -n "$NETMUXD_ASSET" ]; then \
    echo "Attempting to download netmuxd asset: $NETMUXD_ASSET for $TARGETARCH"; \
    wget -q "https://api.github.com/repos/jkcoxson/netmuxd/releases/tags/${NETMUXD_VERSION}" -O /tmp/release.json; \
    ASSET_URL=$(jq -r '.assets[].browser_download_url' /tmp/release.json | grep "${NETMUXD_ASSET}" || true); \
    if [ -n "$ASSET_URL" ]; then \
    wget -q "$ASSET_URL" -O /usr/local/bin/netmuxd; \
    chmod +x /usr/local/bin/netmuxd; \
    echo "Downloaded prebuilt netmuxd: $ASSET_URL"; \
    else \
    echo "Prebuilt asset not found for $TARGETARCH; will build from source."; \
    export NETMUXD_ASSET=""; \
    fi; \
    fi; \
    if [ -z "$NETMUXD_ASSET" ]; then \
    echo "Building netmuxd from source..."; \
    apt-get update && apt-get install -y --no-install-recommends cargo rustc; \
    git clone --depth 1 --branch "${NETMUXD_VERSION}" https://github.com/jkcoxson/netmuxd.git /tmp/netmuxd-src; \
    cd /tmp/netmuxd-src; \
    cargo build --release --locked; \
    cp target/release/netmuxd /usr/local/bin/netmuxd; \
    chmod +x /usr/local/bin/netmuxd; \
    strip /usr/local/bin/netmuxd || true; \
    rm -rf /tmp/netmuxd-src; \
    fi;

############################
# Runtime Stage
############################
FROM ${BASE_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ${LIBIMOBILEDEVICE_PKG} libimobiledevice-utils usbmuxd \
    openssl tini jq wget curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --upgrade pip setuptools wheel

# Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Copy netmuxd from builder
COPY --from=builder /usr/local/bin/netmuxd /usr/local/bin/netmuxd

# Copy application source (expected to be placed under deviceBackup/app in repo)
# If requirements.txt exists, install dependencies; otherwise, create a placeholder.
COPY app/ /app/

RUN set -eux; \
    if [ -f requirements.txt ]; then \
    pip install --no-cache-dir -r requirements.txt; \
    else \
    echo "No requirements.txt found, skipping dependency installation."; \
    fi; \
    rm -rf /root/.cache/pip; \
    if [ ! -f main.py ]; then \
    echo 'from fastapi import FastAPI\napp=FastAPI(title="Placeholder")\n@app.get("/")\ndef root():\n    return {"status":"ok","message":"Add your FastAPI app in app/main.py"}\nif __name__=="__main__":\n    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=int(8080))' > main.py; \
    fi

# Prepare persistent directories (volumes override)
RUN mkdir -p /data/backups /data/lockdown /data/config /data/logs \
    && chown -R appuser:appuser /data

# Link lockdown directory (libimobiledevice expects /var/lib/lockdown)
RUN ln -s /data/lockdown /var/lib/lockdown

# Startup script to optionally launch usbmuxd before app
RUN printf '#!/bin/sh\nset -e\nif [ "${START_USBMUXD}" = "true" ]; then echo "Starting usbmuxd"; usbmuxd ${USBMUXD_FLAGS} & fi\nexec python main.py\n' > /usr/local/bin/container-start \
    && chmod +x /usr/local/bin/container-start

# Environment defaults
ENV BACKUP_DIR=/data/backups
ENV LOCKDOWN_DIR=/var/lib/lockdown
ENV OPENSSL_WEAK_CONFIG=/data/config/openssl-weak.conf
ENV USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015
ENV APP_MODE=full
ENV PORT=8080
ENV START_USBMUXD=true
ENV USBMUXD_SOCKET_PATH=/var/run/usbmuxd
ENV USBMUXD_FLAGS="-f"
# VIRTUAL_ENV removed (using base python:3.12-slim image)
# PATH modification for venv removed

# Expose web port
EXPOSE 8080

# Healthcheck (simple: list devices)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD idevice_id -l >/dev/null 2>&1 || exit 0

USER appuser

# Use tini as entrypoint for proper signal handling
ENTRYPOINT ["/usr/bin/tini","--"]

# Command starts FastAPI app via startup script (with optional usbmuxd)
CMD ["/usr/local/bin/container-start"]
# (Removed redundant second CMD; startup script invokes the application)
