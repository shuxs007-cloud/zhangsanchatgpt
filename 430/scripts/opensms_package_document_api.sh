#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_SOURCE="$WORKSPACE_ROOT/services/opensms_document_api"
CLIENT_SOURCE="$WORKSPACE_ROOT/scripts/opensms_document_api_client.py"
DISPATCH_SOURCE="$WORKSPACE_ROOT/scripts/opensms_process_document.py"
DISPATCH_SHELL_SOURCE="$WORKSPACE_ROOT/scripts/opensms_build_card_slot_report.sh"
DIST_DIR="${1:-$WORKSPACE_ROOT/dist}"
STAMP="$(date '+%Y%m%dT%H%M%S%z')"
PACKAGE_NAME="opensms_document_api_${STAMP}"
STAGING_ROOT="$(mktemp -d)"
PACKAGE_ROOT="$STAGING_ROOT/opensms_document_api"
trap 'rm -rf "$STAGING_ROOT"' EXIT

mkdir -p "$DIST_DIR" "$PACKAGE_ROOT"
cp -R "$SERVICE_SOURCE/." "$PACKAGE_ROOT/"
cp "$CLIENT_SOURCE" "$PACKAGE_ROOT/opensms_document_api_client.py"
cp "$DISPATCH_SOURCE" "$PACKAGE_ROOT/opensms_process_document.py"
cp "$DISPATCH_SHELL_SOURCE" "$PACKAGE_ROOT/opensms_build_card_slot_report.sh"
rm -rf \
  "$PACKAGE_ROOT/data" \
  "$PACKAGE_ROOT/.venv" \
  "$PACKAGE_ROOT/node_modules" \
  "$PACKAGE_ROOT/tests"
find "$PACKAGE_ROOT" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$PACKAGE_ROOT" -type f -name '*.pyc' -delete
chmod +x \
  "$PACKAGE_ROOT/install.sh" \
  "$PACKAGE_ROOT/run_server.sh" \
  "$PACKAGE_ROOT/opensms_document_api_client.py" \
  "$PACKAGE_ROOT/opensms_process_document.py" \
  "$PACKAGE_ROOT/opensms_build_card_slot_report.sh" \
  "$PACKAGE_ROOT/pipeline/build_card_slot_report.sh"

(
  cd "$PACKAGE_ROOT"
  find . -type f ! -name PACKAGE_SHA256SUMS.txt -print0 \
    | sort -z \
    | xargs -0 shasum -a 256 > PACKAGE_SHA256SUMS.txt
)

ARCHIVE_PATH="$DIST_DIR/${PACKAGE_NAME}.zip"
(
  cd "$STAGING_ROOT"
  COPYFILE_DISABLE=1 zip -qry "$ARCHIVE_PATH" opensms_document_api
)
unzip -t "$ARCHIVE_PATH" >/dev/null
shasum -a 256 "$ARCHIVE_PATH" > "${ARCHIVE_PATH}.sha256"
printf '%s\n' "$ARCHIVE_PATH"
