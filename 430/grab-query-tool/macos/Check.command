#!/bin/sh
cd "$(dirname "$0")" || exit 1
./runtime/run-node.sh selfcheck.mjs
grab_exit=$?
if [ -t 0 ]; then printf '\n按回车关闭。\n'; read -r grab_reply; fi
exit "$grab_exit"
