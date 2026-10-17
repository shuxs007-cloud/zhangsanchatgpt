#!/bin/sh
set -eu
grab_runtime_dir=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
grab_machine=$(/usr/bin/uname -m)
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)" = "1" ]; then
  grab_machine=arm64
fi
case "$grab_machine" in
  arm64) grab_node="$grab_runtime_dir/darwin-arm64/node" ;;
  x86_64) grab_node="$grab_runtime_dir/darwin-x64/node" ;;
  *) printf 'Unsupported Mac architecture: %s\n' "$grab_machine"; exit 1 ;;
esac
if [ ! -x "$grab_node" ]; then
  printf 'Runtime is missing or not executable. Please extract the entire ZIP.\n'
  exit 1
fi
exec "$grab_node" "$@"
