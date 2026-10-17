#!/usr/bin/env python3
import os
from pathlib import Path


BASE_DIR = Path(os.environ.get("OPENSMS_BASE_DIR", "/Users/zhangsan/Downloads/430"))
ARTIFACT_ROOT = Path(
    os.environ.get("OPENSMS_ARTIFACTS_DIR", str(BASE_DIR / "opensms_artifacts"))
)
SNAPSHOT_DIR = ARTIFACT_ROOT / "snapshots"
REPORT_DIR = ARTIFACT_ROOT / "reports"
EXPORT_ARCHIVE_DIR = ARTIFACT_ROOT / "exports"
LOG_DIR = ARTIFACT_ROOT / "logs"
REPLY_RATE_HISTORY_DIR = SNAPSHOT_DIR / "reply_rate_history"


def ensure_artifact_dirs():
    for directory in (
        ARTIFACT_ROOT,
        SNAPSHOT_DIR,
        REPORT_DIR,
        EXPORT_ARCHIVE_DIR,
        LOG_DIR,
        REPLY_RATE_HISTORY_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)

