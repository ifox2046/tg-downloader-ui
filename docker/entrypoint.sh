#!/bin/sh
set -eu

forwarder_enabled() {
  forwarder_flag="$(printf '%s' "${TGDL_FORWARDER_ENABLED-1}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
  case "$forwarder_flag" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$(id -u)" = "0" ]; then
  mkdir -p /config /downloads /tdl
  chmod 700 /config /downloads /tdl
  chown -R tgdl:tgdl /config /tdl
  chown tgdl:tgdl /downloads
  exec setpriv --reuid=tgdl --regid=tgdl --init-groups "$0" "$@"
fi

supervisor_pid=""

if forwarder_enabled; then
  tg-downloader-forwarder-supervisor &
  supervisor_pid="$!"
  echo "$supervisor_pid" > "${TGDL_FORWARDER_SUPERVISOR_PID_FILE:-/tmp/tg-downloader-forwarder-supervisor.pid}"
else
  unset TGDL_FORWARDER_RESTART_CMD
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
