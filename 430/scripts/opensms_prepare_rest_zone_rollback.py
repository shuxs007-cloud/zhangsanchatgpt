#!/usr/bin/env python3
"""Build exact per-origin rollback candidates from rest-zone entry backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_REST_SOURCES = {"E-T休息区", "E-C休息区"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def phone_of(row: dict[str, Any]) -> str:
    raw = str(row.get("phone") or row.get("电话") or "").strip()
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_.")
    return cleaned or "carrier"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backups", nargs="+")
    parser.add_argument("--rollback-run-id", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, str] = {}
    inputs: list[dict[str, Any]] = []

    for raw_path in args.backups:
        backup_path = Path(raw_path).expanduser().resolve()
        data = json.loads(backup_path.read_text(encoding="utf-8"))
        rows = data.get("rows") or data.get("candidates") or []
        source_rest = str(data.get("targetCarrierName") or "").strip()
        if source_rest not in ALLOWED_REST_SOURCES:
            raise SystemExit(f"备份目标不是允许的休息区: {source_rest} ({backup_path})")
        if int(data.get("count") or 0) != len(rows):
            raise SystemExit(f"备份数量不一致: {backup_path}")
        inputs.append({
            "path": str(backup_path),
            "sha256": sha256(backup_path),
            "sourceRestCarrier": source_rest,
            "count": len(rows),
        })
        for row in rows:
            phone = phone_of(row)
            origin = str(row.get("原运营商") or "").strip()
            if not phone or len(phone) != 10:
                raise SystemExit(f"备份中号码无效: {phone!r} ({backup_path})")
            if phone in seen:
                raise SystemExit(f"多个备份重复号码: {phone}; {seen[phone]}; {backup_path}")
            if not origin or origin in ALLOWED_REST_SOURCES or re.match(r"^B13(?:-|\s|$)", origin, re.I):
                raise SystemExit(f"备份中原运营商无效: phone={phone}, origin={origin!r}")
            seen[phone] = str(backup_path)
            grouped[(source_rest, origin)].append({
                "电话": phone,
                "phone": phone,
                "运营商": source_rest,
                "原运营商": source_rest,
                "回滚目标运营商": origin,
                "转入休息区前运营商": origin,
                "转入休息区前运营商ID": str(row.get("原运营商ID") or ""),
                "原始转入备份": str(backup_path),
                "原始后台ID": row.get("后台ID"),
            })

    created_at = datetime.now(timezone.utc).isoformat()
    outputs = []
    for index, ((source_rest, origin), rows) in enumerate(sorted(grouped.items()), start=1):
        rows.sort(key=lambda item: item["phone"])
        label = f"rollback_{args.rollback_run_id}_{index:02d}_{safe_name(origin)}"
        candidate_path = out_dir / f"{label}_candidates.json"
        payload = {
            "createdAt": created_at,
            "task": "rest_zone_entry_rollback",
            "route": "rest_zone_entry_rollback",
            "rollbackAuthorization": "current_user_explicit",
            "rollbackRunId": args.rollback_run_id,
            "sourceCarrierNames": [source_rest],
            "configuredSourceCarrierNames": [source_rest],
            "targetCarrierName": origin,
            "count": len(rows),
            "rows": rows,
            "candidates": rows,
        }
        candidate_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        outputs.append({
            "label": label,
            "candidate": str(candidate_path),
            "sourceRestCarrier": source_rest,
            "targetCarrier": origin,
            "count": len(rows),
        })

    manifest = {
        "createdAt": created_at,
        "rollbackRunId": args.rollback_run_id,
        "authorization": "current_user_explicit",
        "scope": "only_numbers_entered_rest_zone_by_named_run",
        "inputBackups": inputs,
        "backupCount": len(inputs),
        "uniquePhoneCount": len(seen),
        "groupCount": len(outputs),
        "outputs": outputs,
    }
    manifest_path = out_dir / f"opensms_rest_zone_rollback_{args.rollback_run_id}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), **manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
