#!/bin/sh
set -eu

pid_file="${TGDL_FORWARDER_PID_FILE:-/tmp/tg-downloader-forwarder.pid}"
delay="${TGDL_FORWARDER_RESPAWN_DELAY:-5}"

trap 'rm -f "$pid_file"; exit 0' INT TERM

while true; do
  tg-downloader-forwarder &
  forwarder_pid="$!"
  echo "$forwarder_pid" > "$pid_file"
  wait "$forwarder_pid" || true
  rm -f "$pid_file"
  sleep "$delay"
done
