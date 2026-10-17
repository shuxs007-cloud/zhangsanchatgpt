#!/usr/bin/env python3
"""Create a source/target-restricted OpenSMS candidate snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def row_source(row: dict) -> str:
    # E recovery rows keep the historical origin in 原运营商; 运营商 is the
    # current backend source that the writer must validate.
    return str(
        row.get("运营商")
        or row.get("currentCarrierName")
        or row.get("原运营商")
        or ""
    ).strip()


def row_target(row: dict) -> str:
    return str(row.get("targetCarrierName") or row.get("目标运营商") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按精确来源/目标过滤候选并生成新的可审计快照。"
    )
    parser.add_argument("input", help="原候选 JSON")
    parser.add_argument("output", help="新候选 JSON，禁止覆盖原文件")
    parser.add_argument("--source", action="append", default=[], help="允许的精确来源，可重复")
    parser.add_argument("--target", help="允许的精确目标")
    args = parser.parse_args()

    source_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if source_path == output_path:
        raise ValueError("输出路径不得覆盖原候选文件")
    if output_path.exists():
        raise FileExistsError(f"输出候选已存在: {output_path}")
    if not args.source and not args.target:
        raise ValueError("至少提供一个 --source 或 --target")

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    rows_key = "rows" if isinstance(payload.get("rows"), list) else "candidates"
    rows = list(payload.get(rows_key) or [])
    allowed_sources = {str(value).strip() for value in args.source if str(value).strip()}
    target = str(args.target or "").strip()
    filtered = [
        row
        for row in rows
        if (not allowed_sources or row_source(row) in allowed_sources)
        and (not target or row_target(row) == target)
    ]

    payload[rows_key] = filtered
    payload["count"] = len(filtered)
    payload["filteredFrom"] = str(source_path)
    payload["filterRule"] = {
        "sourceCarrierNames": sorted(allowed_sources),
        "targetCarrierName": target or None,
    }
    if allowed_sources:
        payload["sourceCarrierNames"] = sorted(allowed_sources)
        payload["configuredSourceCarrierNames"] = sorted(allowed_sources)
    if target:
        payload["targetCarrierName"] = target

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(json.dumps({
        "path": str(output_path),
        "inputCount": len(rows),
        "count": len(filtered),
        "sourceCarrierNames": sorted(allowed_sources),
        "targetCarrierName": target or None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
