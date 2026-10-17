#!/usr/bin/env python3
"""Build the one-time, evidence-bound restore candidate for 快过期续费C."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


PROTECTED_CARRIER = "快过期续费C"
TASK_B_CURRENT_SOURCE = "过期下架卡C"
TASK_C_CURRENT_SOURCE = "换号区C"
EXPECTED_TASK_B_COUNT = 29
EXPECTED_TASK_C_COUNT = 1
EXPECTED_TOTAL_COUNT = 30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成快过期续费C一次性恢复候选")
    parser.add_argument("--task-b-evidence", type=Path, required=True)
    parser.add_argument("--task-c-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def source_name(row: dict[str, Any]) -> str:
    return str(row.get("精确原运营商") or row.get("运营商") or "").strip()


def phone(row: dict[str, Any]) -> str:
    return str(row.get("电话") or row.get("phone") or "").strip().removesuffix(".0")


def selected_rows(document: dict[str, Any], expected_route: str) -> list[dict[str, Any]]:
    if str(document.get("route") or "") != expected_route:
        raise ValueError(f"恢复证据路由不符: {document.get('route')}")
    return [row for row in document.get("rows") or [] if source_name(row) == PROTECTED_CARRIER]


def main() -> None:
    args = parse_args()
    if os.environ.get("OPENSMS_FORCE_QUICK_RENEW_C_RESTORE") != "1":
        raise RuntimeError("OPENSMS_FORCE_QUICK_RENEW_C_RESTORE 未严格等于 1，拒绝生成恢复候选")

    task_b = load(args.task_b_evidence)
    task_c = load(args.task_c_evidence)
    task_b_rows = selected_rows(task_b, "task_b_expired_to_offline_c")
    task_c_rows = selected_rows(task_c, "task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap")
    if len(task_b_rows) != EXPECTED_TASK_B_COUNT or len(task_c_rows) != EXPECTED_TASK_C_COUNT:
        raise ValueError(
            "恢复证据数量不符: "
            f"taskB={len(task_b_rows)}/{EXPECTED_TASK_B_COUNT}, "
            f"taskC={len(task_c_rows)}/{EXPECTED_TASK_C_COUNT}"
        )

    rows: list[dict[str, Any]] = []
    for phase, current_source, source_rows in (
        ("task_b", TASK_B_CURRENT_SOURCE, task_b_rows),
        ("task_c", TASK_C_CURRENT_SOURCE, task_c_rows),
    ):
        for source_row in source_rows:
            card = phone(source_row)
            if not card:
                raise ValueError(f"恢复证据缺少号码: phase={phase}")
            rows.append(
                {
                    **source_row,
                    "电话": card,
                    "运营商": current_source,
                    "精确原运营商": current_source,
                    "恢复目标运营商": PROTECTED_CARRIER,
                    "恢复依据阶段": phase,
                    "历史受保护运营商": PROTECTED_CARRIER,
                }
            )

    phones = [row["电话"] for row in rows]
    if len(rows) != EXPECTED_TOTAL_COUNT or len(set(phones)) != EXPECTED_TOTAL_COUNT:
        raise ValueError(f"恢复号码总数或唯一数不符: total={len(rows)}, unique={len(set(phones))}")

    payload = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "task": "RESTORE_QUICK_RENEW_C",
        "forceGate": True,
        "restoreGate": True,
        "route": "restore_quick_renew_c_from_20260902_forced_tasks",
        "rule": "仅恢复2026-09-02强制任务B/C从快过期续费C转出的30个证据号码",
        "targetCarrierName": PROTECTED_CARRIER,
        "sourceCarrierNames": sorted({TASK_B_CURRENT_SOURCE, TASK_C_CURRENT_SOURCE}),
        "candidateCount": len(rows),
        "count": len(rows),
        "evidence": {
            "taskB": {"path": str(args.task_b_evidence.resolve()), "sha256": sha256(args.task_b_evidence), "count": len(task_b_rows)},
            "taskC": {"path": str(args.task_c_evidence.resolve()), "sha256": sha256(args.task_c_evidence), "count": len(task_c_rows)},
        },
        "rows": rows,
        "candidates": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({**payload, "path": str(args.output.resolve())}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
