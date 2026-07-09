#!/bin/sh
set -eu

supervisor_pid=""

if [ "${TGDL_FORWARDER_ENABLED:-1}" != "0" ]; then
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
