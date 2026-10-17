#!/usr/bin/env python3
"""Validate a fresh export's Kooda identities against one complete inventory.

The core function is pure. The CLI consumes the already-built scan cache and
hash-bound inventory; it never queries the backend or opens a workbook.
"""
from __future__ import annotations

from collections import Counter
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/opensms_document_api/pipeline'))
from archived_system_identity import index_phones, match_archived


def text_value(value) -> str:
    return "" if value is None else str(value).strip()


def normalize_slot(value) -> str:
    """Keep parity with pipeline/process_card_slots.py normalize_slot."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", "").replace(" ", "").replace("-", "_")
    normalized = []
    for part in text.split("_"):
        numeric = re.fullmatch(r"(\d+)(?:\.\d+)?", part)
        normalized.append(str(int(numeric.group(1))) if numeric else part.lower())
    return "_".join(normalized)


def phone_key(value) -> str:
    digits = "".join(character for character in text_value(value) if character.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def is_kooda_opr(value) -> bool:
    return bool(re.search(r"kooda|koodo|telus1", text_value(value), re.I))


def _iccid_key(value) -> str | None:
    # Raw text only; a phone or a rounded spreadsheet number is never an ICCID.
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if not isinstance(value, str):
        return None
    key = re.sub(r"[Ff]$", "", value.strip())
    return key if re.fullmatch(r"[0-9]{18,22}", key) else None


def check_inventory_coverage(scan_cache: dict, inventory: dict) -> dict:
    errors, missing, iccid_conflicts, archived_matches = [], [], [], []
    counters = Counter()

    def structural(code, **details):
        errors.append({"code": code, **details})
        counters[code] += 1

    def result():
        retryable = bool(missing or iccid_conflicts) and not errors
        counts = {
            "sourceKoodaCount": counters["sourceKoodaCount"],
            "matchedCount": counters["matchedCount"],
            "missingCount": len(missing), "iccidConflictCount": len(iccid_conflicts),
            "structuralErrorCount": len(errors),
            "sourceDuplicateKeyCount": counters["source-duplicate-key"],
            "inventoryDuplicateKeyCount": counters["inventory-duplicate-key"],
            "inventoryUnkeyedCount": counters["inventoryUnkeyedCount"],
            "inventoryRowCount": counters["inventoryRowCount"],
            "iccidComparedCount": counters["iccidComparedCount"],
            "archivedMatchedCount": len(archived_matches),
        }
        return {
            "status": "failed" if errors or missing or iccid_conflicts else "passed",
            "retryable": retryable, **counts, "counts": counts,
            "missing": missing, "iccidConflicts": iccid_conflicts,
            "archivedMatches": archived_matches,
            "structuralErrors": errors,
            "matchRule": "normalized_slotIdentif_and_cardNo_last10_exact",
            "iccidRule": "raw_text_trim_strip_exactly_one_final_F_case_insensitive",
            "transactionalSnapshotGuaranteed": False,
        }

    if not isinstance(scan_cache, dict) or not isinstance(scan_cache.get("headers"), (tuple, list)) \
            or not isinstance(scan_cache.get("rows"), list):
        structural("invalid-scan-cache")
        return result()
    headers = [text_value(value) for value in scan_cache["headers"]]
    if any(count > 1 for name, count in Counter(headers).items() if name):
        structural("duplicate-source-headers")
    for name in ("电话", "运营商", "opr", "卡槽"):
        if headers.count(name) != 1:
            structural("missing-or-ambiguous-source-header", header=name)
    if not isinstance(inventory, dict) or not isinstance(inventory.get("systemRows"), list):
        structural("invalid-inventory-rows")
    if errors:
        return result()
    raw_inventory = inventory["systemRows"]
    if "totalRows" in inventory and (type(inventory["totalRows"]) is not int
            or inventory["totalRows"] != len(raw_inventory)):
        structural("inventory-total-mismatch")
    counters["inventoryRowCount"] = len(raw_inventory)
    index, backend_ids = {}, {}
    for inventory_row, row in enumerate(raw_inventory, 1):
        if not isinstance(row, dict):
            structural("invalid-inventory-row", inventoryRow=inventory_row)
            continue
        if not is_kooda_opr(row.get("opr")):
            structural("non-kooda-inventory-row", inventoryRow=inventory_row)
        backend_id = text_value(row.get("id"))
        if not backend_id:
            structural("missing-inventory-backend-id", inventoryRow=inventory_row)
        elif backend_id in backend_ids:
            structural("duplicate-inventory-backend-id", inventoryRow=inventory_row,
                       firstInventoryRow=backend_ids[backend_id], backendId=backend_id)
        else:
            backend_ids[backend_id] = inventory_row
        raw_iccid = row.get("cardStore")
        if raw_iccid is not None and not isinstance(raw_iccid, str):
            structural("non-text-inventory-iccid", inventoryRow=inventory_row, backendId=backend_id)
        slot, phone = normalize_slot(row.get("slotIdentif")), phone_key(row.get("cardNo"))
        if not slot or len(phone) != 10:
            # The generator permits unregistered inventory rows without a key.
            # They cannot satisfy a source identity and are counted explicitly.
            counters["inventoryUnkeyedCount"] += 1
            continue
        key = (slot, phone)
        entry = {"inventoryRow": inventory_row, "backendId": backend_id, "rawIccid": raw_iccid}
        if key in index:
            structural("inventory-duplicate-key", inventoryRow=inventory_row,
                       firstInventoryRow=index[key][0]["inventoryRow"], slotKey=slot, phoneKey=phone)
            index[key].append(entry)
        else:
            index[key] = [entry]

    columns = {name: headers.index(name) for name in ("电话", "opr", "卡槽")}
    archived_index = index_phones([row for row in raw_inventory if isinstance(row, dict)])
    iccid_column = headers.index("ICCID") if "ICCID" in headers else None
    source_index = {}
    archived_backend_ids = set()
    for source_row, values in enumerate(scan_cache["rows"], 2):
        if not isinstance(values, (list, tuple)) or len(values) <= max(columns.values()):
            structural("invalid-source-row", sourceRow=source_row)
            continue
        if not is_kooda_opr(values[columns["opr"]]):
            continue
        counters["sourceKoodaCount"] += 1
        raw_phone, raw_slot = values[columns["电话"]], values[columns["卡槽"]]
        slot, phone = normalize_slot(raw_slot), phone_key(raw_phone)
        raw_iccid = values[iccid_column] if iccid_column is not None and iccid_column < len(values) else None
        identity = {"sourceRow": source_row, "phone": text_value(raw_phone), "slot": text_value(raw_slot),
                    "phoneKey": phone, "slotKey": slot, "sourceIccid": raw_iccid}
        bad_identity = False
        if not slot:
            structural("invalid-source-slot", **identity)
            bad_identity = True
        if len(phone) != 10:
            structural("invalid-source-phone", **identity)
            bad_identity = True
        source_iccid = _iccid_key(raw_iccid)
        if source_iccid is None:
            structural("invalid-source-iccid", **identity)
            bad_identity = True
        if bad_identity:
            continue
        key = (slot, phone)
        if key in source_index:
            structural("source-duplicate-key", firstSourceRow=source_index[key], **identity)
            continue
        source_index[key] = source_row
        hits = index.get(key, [])
        if not hits:
            # Only a separately proven historical row may lack a current slot.
            archived = match_archived(archived_index, phone=raw_phone, slot=raw_slot,
                status=values[headers.index('状态')] if '状态' in headers else None,
                created=values[headers.index('创建时间')] if '创建时间' in headers else None,
                iccid=raw_iccid) if iccid_column is not None else None
            if archived is not None:
                backend_id = text_value(archived['id'])
                if backend_id in archived_backend_ids:
                    structural('archived-source-duplicate-backend-id', backendId=backend_id, **identity)
                    continue
                archived_backend_ids.add(backend_id)
                counters['matchedCount'] += 1
                archived_matches.append({**identity, 'backendId': text_value(archived['id']),
                    'sourceStatus': values[headers.index('状态')],
                    'inventoryStatus': archived.get('status'),
                    'lifecycleTransition': values[headers.index('状态')] == '上架',
                    'reason': 'archived-phone-machine-created-iccid-exact', 'liveSlotMatched': False})
                continue
            missing.append({**identity, "reason": "missing-slot-phone"})
            continue
        if len(hits) != 1:
            continue  # Already recorded as a structural duplicate, never deduplicate.
        hit = hits[0]
        if source_iccid:
            counters["iccidComparedCount"] += 1
            inventory_iccid = _iccid_key(hit["rawIccid"])
            if inventory_iccid is None:
                structural("invalid-inventory-iccid", **identity, **hit)
                continue
            if source_iccid != inventory_iccid:
                iccid_conflicts.append({**identity, **hit, "sourceIccidKey": source_iccid,
                                       "inventoryIccidKey": inventory_iccid, "reason": "same-key-iccid-changed"})
                continue
        counters["matchedCount"] += 1
    return result()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="核对本轮导出与完整Kooda库存身份覆盖，不联网")
    for name in ("scan-cache", "source", "inventory", "inventory-sha256", "run-id", "output"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Claim receipt before any input work, preserving evidence from prior runs.
    with output.open("x", encoding="utf-8") as handle:
        try:
            project_root = str(Path(__file__).resolve().parents[1])
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            from opensms_export_scan_cache import load_scan_cache
            scan = load_scan_cache(args.scan_cache, expected_source=args.source)
            raw = Path(args.inventory).read_bytes()
            actual_hash = hashlib.sha256(raw).hexdigest()
            if not re.fullmatch(r"[a-fA-F0-9]{64}", args.inventory_sha256) \
                    or actual_hash != args.inventory_sha256.lower():
                raise ValueError("inventory-sha256-mismatch")
            inventory = json.loads(raw)
            if not args.run_id.strip() or not isinstance(inventory, dict) \
                    or inventory.get("runId") != args.run_id or inventory.get("status") != "passed":
                raise ValueError("inventory-run-id-or-status-mismatch")
            payload = check_inventory_coverage(scan, inventory)
            payload["sourceSha256"] = scan.get("sourceSha256")
        except Exception as error:
            payload = {"status": "failed", "retryable": False, "sourceKoodaCount": 0, "matchedCount": 0,
                       "missingCount": 0, "missing": [], "iccidConflictCount": 0, "iccidConflicts": [],
                       "structuralErrorCount": 1,
                       "structuralErrors": [{"code": "invalid-coverage-input", "detail": str(error)}],
                       "counts": {"structuralErrorCount": 1}}
        payload.update({"runId": args.run_id, "receipt": str(output),
                        "scanCache": str(Path(args.scan_cache).resolve()), "source": str(Path(args.source).resolve()),
                        "inventory": str(Path(args.inventory).resolve()), "inventorySha256": args.inventory_sha256.lower()})
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0 if payload["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
