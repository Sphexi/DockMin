#!/bin/sh
# Stand-in for docker-buildx: records its invocation beside itself, then replays stderr.txt,
# stdout.txt, exit and hang from that directory.
set -eu

dir=${0%/*}
printf '%s\n' "$@" > "$dir/argv"
env > "$dir/env"
pwd > "$dir/cwd"
{
  printf 'docker_config=%s\n' "$(cat "$DOCKER_CONFIG/config.json")"
  if [ -d "$BUILDX_CONFIG" ]; then printf 'buildx_config=dir\n'; else printf 'buildx_config=missing\n'; fi
  if [ -n "${DOCKER_CERT_PATH:-}" ]; then
    printf 'tls='
    for file in "$DOCKER_CERT_PATH"/*; do printf '%s ' "${file##*/}"; done
    printf '\n'
  fi
  printf 'stdin_bytes=%s\n' "$(wc -c)"
} > "$dir/probe"

if [ -f "$dir/hang" ]; then exec sleep 60; fi
if [ -f "$dir/stderr.txt" ]; then cat "$dir/stderr.txt" >&2; fi
if [ -f "$dir/stdout.txt" ]; then cat "$dir/stdout.txt"; fi
if [ -f "$dir/exit" ]; then exit "$(cat "$dir/exit")"; fi
exit 0
