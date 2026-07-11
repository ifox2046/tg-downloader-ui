#!/bin/sh
set -eu

forwarder_enabled() {
  forwarder_flag="$(printf '%s' "${TGDL_FORWARDER_ENABLED-1}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
  case "$forwarder_flag" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

forwarder_enabled || {
  echo "forwarder is disabled" >&2
  exit 1
}

pid_file="${TGDL_FORWARDER_PID_FILE:-/tmp/tg-downloader-forwarder.pid}"
supervisor_pid_file="${TGDL_FORWARDER_SUPERVISOR_PID_FILE:-/tmp/tg-downloader-forwarder-supervisor.pid}"

if [ -s "$supervisor_pid_file" ]; then
  supervisor_pid="$(cat "$supervisor_pid_file")"
else
  supervisor_pid=""
fi

if [ -z "$supervisor_pid" ] || ! kill -0 "$supervisor_pid" 2>/dev/null; then
  tg-downloader-forwarder-supervisor >/tmp/tg-downloader-forwarder-supervisor.log 2>&1 &
  supervisor_pid="$!"
  echo "$supervisor_pid" > "$supervisor_pid_file"
fi

if [ -s "$pid_file" ]; then
  forwarder_pid="$(cat "$pid_file")"
  if kill -0 "$forwarder_pid" 2>/dev/null; then
    kill "$forwarder_pid"
    echo "forwarder restart requested"
    exit 0
  fi
fi

echo "forwarder supervisor running; no active forwarder process to stop"
