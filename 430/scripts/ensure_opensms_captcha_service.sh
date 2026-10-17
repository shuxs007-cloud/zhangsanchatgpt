#!/usr/bin/env bash
set -euo pipefail

health_url="http://127.0.0.1:2001/health"
service_label="com.local.captcha-ocr-2001"
service_plist="/Users/zhangsan/Library/LaunchAgents/com.local.captcha-ocr-2001.plist"
service_domain="gui/$(id -u)"

if curl -fsS --max-time 5 "$health_url" >/dev/null; then
  printf '{"success":true,"started":false,"health":"%s"}\n' "$health_url"
  exit 0
fi

launchctl kickstart -k "$service_domain/$service_label" >/dev/null 2>&1 || \
  launchctl load -w "$service_plist" >/dev/null 2>&1

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 5 "$health_url" >/dev/null; then
    printf '{"success":true,"started":true,"health":"%s"}\n' "$health_url"
    exit 0
  fi
  sleep 1
done

printf '{"success":false,"started":true,"health":"%s"}\n' "$health_url"
exit 1
