#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec ./runtime/run-node.sh open-data.mjs
