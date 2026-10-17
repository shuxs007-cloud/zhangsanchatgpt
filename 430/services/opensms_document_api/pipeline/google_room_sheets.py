"""Exact live room tabs for current and legacy Google workbook layouts."""
from io import BytesIO

from openpyxl import load_workbook

LEGACY_ROOM_SHEETS = ("二號機房", "三號機房", "四號機房")
CURRENT_ROOM_SHEETS = ("二號機房", "三號機房", "五號機房")


def select_room_sheets(book):
    # Backups, similar names and hidden sheets are never substitute live inputs.
    visible = {ws.title for ws in book.worksheets if ws.sheet_state == "visible"}
    missing = [name for name in CURRENT_ROOM_SHEETS[:2] if name not in visible]
    if missing:
        raise ValueError("Google原表缺少必需可见页签: " + ", ".join(missing))
    alternatives = [name for name in ("四號機房", "五號機房") if name in visible]
    if len(alternatives) != 1:
        if alternatives:
            raise ValueError("Google原表四號機房、五號機房同时可见，来源范围不明确；需确认有效页签")
        raise ValueError("Google原表缺少可见机房页签: 五號機房（或旧版四號機房）；不使用隐藏备份补位")
    return CURRENT_ROOM_SHEETS if alternatives[0] == "五號機房" else LEGACY_ROOM_SHEETS


def bound_room_sheets(raw, claimed):
    """Require the snapshot's scope to match the hash-verified original bytes."""
    book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    try:
        sheets = select_room_sheets(book)
        if claimed != list(sheets):
            raise ValueError("Google快照sourceSheets与原表有效机房页签不一致")
        return sheets
    finally:
        book.close()
