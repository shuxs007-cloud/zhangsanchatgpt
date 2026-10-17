#!/bin/sh
cd "$(dirname "$0")" || exit 1
./runtime/run-node.sh launch.mjs "$@"
grab_exit=$?
if [ "$grab_exit" -ne 0 ] && [ -t 0 ]; then
  printf '\n启动失败，请查看使用说明和本地日志。按回车关闭。\n'
  read -r grab_reply
fi
exit "$grab_exit"
