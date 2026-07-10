#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /config /downloads /tdl
  chown -R tgdl:tgdl /config /tdl
  chown tgdl:tgdl /downloads
  exec setpriv --reuid=tgdl --regid=tgdl --init-groups "$0" "$@"
fi

supervisor_pid=""

if [ "${TGDL_FORWARDER_ENABLED:-0}" != "0" ]; then
  tg-downloader-forwarder-supervisor &
  supervisor_pid="$!"
  echo "$supervisor_pid" > "${TGDL_FORWARDER_SUPERVISOR_PID_FILE:-/tmp/tg-downloader-forwarder-supervisor.pid}"
fi

"$@" &
app_pid="$!"

stop_all() {
  kill "$app_pid" 2>/dev/null || true
  if [ -n "$supervisor_pid" ]; then
    kill "$supervisor_pid" 2>/dev/null || true
  fi
  wait "$app_pid" 2>/dev/null || true
  if [ -n "$supervisor_pid" ]; then
    wait "$supervisor_pid" 2>/dev/null || true
  fi
}

trap 'stop_all; exit 143' INT TERM

wait "$app_pid"
status="$?"
stop_all
exit "$status"
