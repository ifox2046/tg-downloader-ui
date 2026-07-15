FROM python:3.12-slim

# BuildKit sets TARGETARCH (amd64, arm64, …). Default keeps plain
# `docker build` / compose on x86_64 hosts on the amd64 tdl pin.
ARG TARGETARCH=amd64
ARG TDL_VERSION=0.20.3
ARG TGDL_UID=1000
ARG TGDL_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/tgdl \
    TGDL_HOST=0.0.0.0 \
    TGDL_PORT=9910 \
    TGDL_STATE_DIR=/config \
    TGDL_DOWNLOAD_DIR=/downloads \
    TGDL_TDL_BIN=/usr/local/bin/tdl \
    TGDL_TDL_STORAGE=type=bolt,path=/tdl/data \
    TGDL_TDL_LOG=/config/tdl.log \
    TGDL_SESSION_FILE=/tdl/session.txt \
    TGDL_FORWARDER_ENABLED=1 \
    TGDL_FORWARDER_RESTART_CMD=/usr/local/bin/tg-downloader-forwarder-restart

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl passwd tar util-linux \
    && groupadd --gid "${TGDL_GID}" tgdl \
    && useradd --uid "${TGDL_UID}" --gid "${TGDL_GID}" --create-home --home-dir /home/tgdl tgdl \
    && install -d -o tgdl -g tgdl /config /downloads /tdl \
    && command -v setpriv \
    && rm -rf /var/lib/apt/lists/*

# Pinned unmodified upstream tdl 0.20.3 assets (aligned with OpenWrt full IPKs).
# amd64: tdl_Linux_64bit.tar.gz / f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718
# arm64: tdl_Linux_arm64.tar.gz / 8398784d5b9390d26450e3e3528e2ffd0e9fe75d374f63273d0247e7ab0378b7
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    case "${arch}" in \
      amd64|x86_64) \
        tdl_asset="tdl_Linux_64bit.tar.gz"; \
        tdl_sha256="f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718" \
        ;; \
      arm64|aarch64) \
        tdl_asset="tdl_Linux_arm64.tar.gz"; \
        tdl_sha256="8398784d5b9390d26450e3e3528e2ffd0e9fe75d374f63273d0247e7ab0378b7" \
        ;; \
      *) \
        echo "unsupported TARGETARCH for tdl: ${arch}" >&2; \
        exit 1 \
        ;; \
    esac; \
    curl -fsSL "https://github.com/iyear/tdl/releases/download/v${TDL_VERSION}/${tdl_asset}" \
      -o /tmp/tdl.tar.gz; \
    echo "${tdl_sha256}  /tmp/tdl.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/tdl.tar.gz -C /tmp; \
    find /tmp -type f -name tdl -exec install -m 0755 {} /usr/local/bin/tdl \;; \
    rm -rf /tmp/tdl.tar.gz /tmp/tdl*

COPY pyproject.toml README.md THIRD_PARTY.md LICENSE /app/
COPY tg_downloader_ui /app/tg_downloader_ui
COPY docker/entrypoint.sh /usr/local/bin/tg-downloader-ui-entrypoint
COPY docker/forwarder-supervisor.sh /usr/local/bin/tg-downloader-forwarder-supervisor
COPY docker/restart-forwarder.sh /usr/local/bin/tg-downloader-forwarder-restart

RUN chmod +x \
      /usr/local/bin/tg-downloader-ui-entrypoint \
      /usr/local/bin/tg-downloader-forwarder-supervisor \
      /usr/local/bin/tg-downloader-forwarder-restart \
    && pip install --no-cache-dir .

VOLUME ["/config", "/downloads", "/tdl"]
EXPOSE 9910

ENTRYPOINT ["tg-downloader-ui-entrypoint"]
CMD ["tg-downloader-ui"]
