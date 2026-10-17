"""Read a run-private atomic submission receipt; never access any service."""
from datetime import date, datetime
import json
from pathlib import Path
import re


def read_submission_ready(path, run_id, token):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("导出调度回执不能是符号链接")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("导出调度回执不是有效的小型本轮文件")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or type(value.get("schemaVersion")) is not int
            or value["schemaVersion"] != 1 or value.get("status") != "submitted"
            or value.get("runId") != run_id or value.get("token") != token
            or not re.fullmatch(r"[a-fA-F0-9]{32}", str(value.get("taskId") or ""))
            or value.get("exportPageClosed") is not True
            or value.get("requestLaneReleased") is not True):
        raise ValueError("导出调度回执身份或提交确认不符，禁止启动其它系统采集")
    raw_date = value.get("serverDate")
    if not isinstance(raw_date, str) or date.fromisoformat(raw_date).isoformat() != raw_date:
        raise ValueError("导出调度回执日期无效")
    published = datetime.fromisoformat(value["publishedAt"].replace("Z", "+00:00"))
    if published.tzinfo is None:
        raise ValueError("导出调度回执时间缺少时区")
    return value
