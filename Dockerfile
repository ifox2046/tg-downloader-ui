FROM python:3.12-slim

ARG TDL_VERSION=0.20.3
ARG TDL_ASSET=tdl_Linux_64bit.tar.gz
ARG TDL_SHA256=f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718
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
    TGDL_FORWARDER_ENABLED=0 \
    TGDL_FORWARDER_RESTART_CMD=/usr/local/bin/tg-downloader-forwarder-restart

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl passwd tar util-linux \
    && groupadd --gid "${TGDL_GID}" tgdl \
    && useradd --uid "${TGDL_UID}" --gid "${TGDL_GID}" --create-home --home-dir /home/tgdl tgdl \
    && install -d -o tgdl -g tgdl /config /downloads /tdl \
    && command -v setpriv \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/iyear/tdl/releases/download/v${TDL_VERSION}/${TDL_ASSET}" \
      -o /tmp/tdl.tar.gz \
    && echo "${TDL_SHA256}  /tmp/tdl.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/tdl.tar.gz -C /tmp \
    && find /tmp -type f -name tdl -exec install -m 0755 {} /usr/local/bin/tdl \; \
    && rm -rf /tmp/tdl.tar.gz /tmp/tdl*

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
