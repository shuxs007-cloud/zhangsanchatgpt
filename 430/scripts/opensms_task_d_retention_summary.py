#!/usr/bin/env python3
"""Local-only, receipt-bound Task D net new retention and final population counts.

No browser, database, or network access. A count is produced only from an exact
post-business two-carrier population and the current run's carrier receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROUTES = {
    "task_d_retain_chatr_to_b14_c": "B14-C快下保留卡",
    "task_d_retain_kooda_to_b14_t": "B14-T快下保留卡",
}
CARRIERS = set(ROUTES.values())


class RetentionProofError(ValueError):
    """Incomplete evidence must remain unknown rather than become a zero."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RetentionProofError(message)


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path: str | Path) -> dict[str, Any]:
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(result, dict), f"回执不是对象: {path}")
    return result


def instant(value: Any, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RetentionProofError(f"{label}缺少有效时间") from exc
    require(result.tzinfo is not None, f"{label}缺少时区")
    return result


def phone10(value: Any) -> str:
    value = re.sub(r"[\s()+-]", "", str(value or "").strip())
    require(bool(re.fullmatch(r"\d{10,11}", value)), "手机号含非数字或长度不正确")
    require(len(value) in {10, 11} and (len(value) == 10 or value.startswith("1")), "号码不是有效10位或北美11位字符串")
    return value[-10:]


def row_phone(row: dict[str, Any]) -> str:
    return phone10(row.get("phone10") or row.get("电话") or row.get("phone") or row.get("cardNo"))


def rows_by_phone(rows: Any, label: str) -> dict[str, dict[str, Any]]:
    require(isinstance(rows, list), f"{label}缺少逐号清单")
    result = {}
    for row in rows:
        require(isinstance(row, dict), f"{label}逐号记录无效")
        phone = row_phone(row)
        require(phone not in result, f"{label}存在重复手机号")
        result[phone] = row
    return result


def identity(row: dict[str, Any]) -> str:
    return str(row.get("后台ID") or row.get("backendId") or row.get("id") or "")


def carrier_id(row: dict[str, Any]) -> str:
    return str(row.get("carrierIdentif") or row.get("carrireIdentif") or row.get("原运营商ID") or "")


def bound_to_run(path: Path, data: dict[str, Any], run_id: str) -> None:
    if data.get("runId") is not None:
        require(data["runId"] == run_id, "回执runId不属于本轮")
    tags = {run_id, re.sub(r"[^0-9A-Za-z_.-]+", "_", run_id)}
    require(any(tag in str(path) for tag in tags), "回执路径不属于精确本轮运行")


def reference_matches(reference: Any, actual: Path, label: str) -> None:
    require(bool(reference), f"{label}缺少来源路径")
    ref = Path(str(reference))
    require(ref.name == actual.name, f"{label}来源文件不一致")
    if ref.is_file():
        require(sha256(ref) == sha256(actual), f"{label}来源文件内容不一致")


def validate_population(data: dict[str, Any], run_id: str, business_completed: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    require(data.get("schemaVersion") == 1 and data.get("task") == "D", "存量回执版本/任务无效")
    require(data.get("runId") == run_id, "存量回执runId不属于本轮")
    require(data.get("status") == "passed" and data.get("phase") == "post_task_d_retention_population", "任务D结束后存量读取未通过")
    require(data.get("scope") == "exact_b14_c_t_all_pages" and data.get("countingUnit") == "unique_phone10", "存量统计范围不是B14-C/T全页唯一手机号")
    require(data.get("sourceProvenance") == "phone_config_exact_carriers_all_pages" and data.get("endpoint") == "/phone/config/list" and data.get("readOnly") is True, "存量不是号码管理全页只读查询")
    require(data.get("silentBrowserMode") is True and data.get("browserForegroundActivationAttempted") is False, "存量缺少静默证明")
    require(all((data.get("authorization") or {}).get(key) is True for key in ("forceGate", "enableGate", "executeGate", "runIdMatched")), "存量缺少任务D本轮授权门禁")
    require((data.get("browserSession") or {}).get("profile") == "430" and bool((data.get("browserSession") or {}).get("dedicatedTargetId")), "存量不是430后台独立页面")
    concurrency = data.get("readOnlyConcurrency") or {}
    require(concurrency.get("failed") == concurrency.get("cancelled") == 0 and concurrency.get("submitted") == concurrency.get("completed") and isinstance(concurrency.get("submitted"), int), "存量存在未完成/失败查询")
    started = instant(data.get("createdAt"), "存量开始时间")
    completed = instant(data.get("completedAt"), "存量完成时间")
    require(business_completed <= started <= completed, "存量回执不是任务D业务全部完成后的查询")
    groups = data.get("groups")
    require(isinstance(groups, list) and len(groups) == 2, "存量缺少两个精确保留区")
    by_carrier, all_rows, backend_ids = {}, {}, set()
    for group in groups:
        name, identif = group.get("carrierName"), str(group.get("carrierIdentif") or "")
        require(name in CARRIERS and name not in by_carrier and bool(identif), "保留区重复、名称或精确ID无效")
        raw_rows = group.get("rows")
        require(isinstance(raw_rows, list), "保留区缺少逐号存量")
        rows = {}
        for row in raw_rows:
            require(isinstance(row, dict), "保留区逐号存量无效")
            phone = row_phone(row)
            require(carrier_id(row) == identif and identity(row), "存量逐号目标/后台身份无效")
            require(phone not in all_rows and identity(row) not in backend_ids, "保留区全量存在跨区重复手机号或后台ID")
            rows.setdefault(phone, []).append({**row, "carrierName": name})
            backend_ids.add(identity(row))
        all_rows.update(rows)
        require(group.get("rawRowCount") == group.get("firstPageTotal") == len(raw_rows) and group.get("uniquePhoneCount") == len(rows) and group.get("duplicatePhoneRowCount") == len(raw_rows) - len(rows), "保留区存量数量与逐号清单不一致")
        require(group.get("pageSize") == data.get("pageSize") == 500 and group.get("paginationTotalStable") is True and group.get("backendIdsUnique") is True and group.get("exactCarrierValidated") is True, "保留区分页或目标证明缺失")
        page_count = max(1, (len(raw_rows) + 499) // 500)
        pages = group.get("pageProofs")
        require(isinstance(pages, list) and len(pages) == group.get("pageCount") == page_count, "保留区分页未完整覆盖")
        for page_num, page in enumerate(pages, 1):
            expected_count = min(500, max(0, len(raw_rows) - (page_num - 1) * 500))
            require(page.get("pageNum") == page_num and page.get("pageSize") == 500 and page.get("reportedTotal") == len(raw_rows) and page.get("rowCount") == page.get("expectedRowCount") == expected_count, "保留区分页页码/总数/行数证明错误")
            require(started <= instant(page.get("verifiedAt"), "分页查询时间") <= completed, "分页不是本次存量快照内的查询")
        by_carrier[name] = {**group, "rowMap": rows}
    require(len({g["carrierIdentif"] for g in groups}) == 2, "两个保留区ID不是独立精确目标")
    require(data.get("totalUniquePhoneCount") == len(all_rows), "保留区总数与逐号并集不一致")
    require(data.get("totalRawRowCount") == len(backend_ids) and data.get("totalDuplicatePhoneRowCount") == len(backend_ids) - len(all_rows) and data.get("crossGroupPhoneOverlapCount") == 0 and data.get("backendIdsUnique") is True, "保留区原始/去重总数证明错误")
    return by_carrier, all_rows


def build_summary(population_path: str | Path, write_groups: str | Path | dict[str, Any], run_id: str,
                  output_path: str | Path | None = None, *,
                  path_resolver: Callable[[Any], Path] | None = None) -> dict[str, Any]:
    """Validate and count; output_path is exclusively created, omitted for audit."""
    resolve = path_resolver or (lambda path: Path(path).resolve())
    population_path = resolve(population_path)
    population = load(population_path)
    groups_path = None if isinstance(write_groups, dict) else resolve(write_groups)
    bundle = write_groups if groups_path is None else load(groups_path)
    require(bundle.get("runId") == run_id, "写入分组不属于本轮")
    business_completed = instant(bundle.get("businessCompletedAt"), "任务D业务完成时间")
    by_carrier, population_rows = validate_population(population, run_id, business_completed)
    group_list = bundle.get("groups")
    transfer_removed = bundle.get("retentionCarrierTransferEnabled") is False
    if transfer_removed:
        require(bundle.get("retentionCarrierTransferStatus") == "removed_by_user" and group_list == [],
                "任务D转入保留区工作已删除，不能包含运营商写入分组")
    else:
        # Read-only compatibility for immutable historical receipts.
        require(isinstance(group_list, list) and len(group_list) == 2, "任务D需要两个保留区写入分组证明")
    seen_routes, seen_candidates, new_transfers, existing_transfers, proofs = set(), set(), [], [], []
    for group in group_list:
        candidate_path = resolve(group.get("candidate"))
        candidate = load(candidate_path)
        bound_to_run(candidate_path, candidate, run_id)
        route = candidate.get("route")
        require(route in ROUTES and route not in seen_routes, "任务D候选路由不完整或重复")
        seen_routes.add(route)
        name = ROUTES[route]
        require(group.get("route", route) == route and group.get("target", name) == name, "分组路由或目标不一致")
        require(candidate.get("task") == "D" and candidate.get("forceGate") is True and candidate.get("automationGate") is True, "任务D候选授权证明缺失")
        require(candidate.get("phase") == "retention_carrier" and candidate.get("sourceProvenance") == "phone_card_retain_all_pages", "候选不是任务D卡保留全页来源")
        require(candidate.get("targetCarrierName") == name, "候选目标保留区错误")
        candidates = rows_by_phone(candidate.get("rows"), "原始候选")
        require(candidate.get("count") == len(candidates), "原始候选count错误")
        require(not seen_candidates.intersection(candidates), "两个保留区候选有重复号码")
        seen_candidates.update(candidates)
        require(instant(candidate.get("createdAt"), "候选创建时间") <= business_completed, "候选晚于业务完成")
        proof = {"route": route, "target": name, "candidate": str(candidate_path), "candidateSha256": sha256(candidate_path)}
        if not candidates:
            require(group.get("result") in {"no_candidate", "无候选"} and group.get("actualWrite", 0) == 0 and group.get("backendValid", 0) == 0, "空候选写入状态不是明确无候选")
            require(not any(group.get(key) for key in ("validated", "write", "verify")), "无候选却带有写入回执")
            proof["newTransferredCount"] = 0
            proofs.append(proof)
            continue
        receipt_data = {}
        paths = {}
        for key in ("validated", "write", "verify"):
            require(bool(group.get(key)), f"非空候选缺少{key}回执")
            paths[key] = resolve(group[key])
            receipt_data[key] = load(paths[key])
            bound_to_run(paths[key], receipt_data[key], run_id)
            proof[key], proof[f"{key}Sha256"] = str(paths[key]), sha256(paths[key])
        validated, write, verify = (receipt_data[k] for k in ("validated", "write", "verify"))
        require(validated.get("route") == route and validated.get("targetCarrierName") == name, "有效候选路由/目标错误")
        reference_matches(validated.get("sourceCandidate"), candidate_path, "有效候选")
        reference_matches(write.get("candidatePath"), candidate_path, "写入候选")
        reference_matches(write.get("validatedCandidatePath"), paths["validated"], "写入有效候选")
        reference_matches(verify.get("source"), paths["validated"], "独立验证")
        valid_rows = rows_by_phone(validated.get("rows"), "有效候选")
        require(set(valid_rows).issubset(candidates) and validated.get("count") == len(valid_rows) and validated.get("originalCount") == len(candidates), "有效候选数量/来源不一致")
        target = by_carrier[name]
        for receipt in (write, verify):
            require((receipt.get("target") or {}).get("name") == name, "写入或独立复核目标错误")
        require(str((write.get("target") or {}).get("identif") or "") == str(target["carrierIdentif"]), "写入目标ID与结束存量目标不一致")
        before = write.get("before") or {}
        eligible_rows = rows_by_phone(before.get("eligibleRows"), "写前可写号码")
        require(set(eligible_rows) == set(valid_rows), "写前可写清单与有效候选不一致")
        require(before.get("candidateCount") == len(candidates) and before.get("eligibleCount") == len(valid_rows) and write.get("actualWriteCount") == len(valid_rows), "写入数量证明不一致")
        require(group.get("actualWrite", len(valid_rows)) == len(valid_rows) and group.get("backendValid") == len(valid_rows), "运行状态写入数与回执不一致")
        for receipt in (before, verify):
            require(receipt.get("missingCount") == 0 and receipt.get("duplicateBackendCount") == 0, "写入或独立复核缺失/重复")
        excluded = {}
        for key in ("alreadyTarget", "nonSource"):
            found = rows_by_phone(before.get(key), f"写前{key}")
            require(before.get(f"{key}Count") == len(found), "写前排除计数错误")
            require(not set(excluded).intersection(found), "写前排除号码重复")
            excluded.update(found)
        require(not set(excluded).intersection(valid_rows) and set(excluded) | set(valid_rows) == set(candidates), "写前判定未完整覆盖候选")
        checked_at = instant(verify.get("checkedAt"), "独立验证时间")
        require(instant(validated.get("validatedAt"), "后台有效候选时间") <= instant(write.get("ts"), "写入完成时间") <= checked_at <= business_completed, "任务D写入/验证/业务完成时序不一致")
        require(verify.get("total") == verify.get("targetCount") == len(valid_rows) and verify.get("stillNotTargetCount") == 0, "独立验证未完整覆盖有效候选")
        if not valid_rows:
            require(write.get("noWrite") is True and write.get("updateResponses") == [] and write.get("after") is None, "空写入缺少零提交证明")
            require(verify.get("passed") is None and verify.get("status") == "no_candidate" and verify.get("verificationStatus") == "not_required" and verify.get("independentReadbackPerformed") is False, "空写入被误算独立验证成功")
        else:
            require(write.get("status") == "written" and write.get("noWrite") is False and bool(write.get("updateResponses")) and all(item.get("code") == 200 for item in write.get("updateResponses") or []), "真实写入业务回执缺失")
            require(verify.get("passed") is True and verify.get("independentReadbackPerformed") is True and verify.get("mode") == "protocol-api-readonly-verify", "真实写入未独立复核通过")
            require(str((verify.get("target") or {}).get("identif") or "") == str(target["carrierIdentif"]), "独立复核目标ID错误")
        source_ids = {}
        for source in write.get("sourceCarriers") or []:
            sid, sname = str(source.get("identif") or source.get("carrireIdentif") or ""), str(source.get("name") or "")
            require(sid and sname and sid not in source_ids, "精确来源运营商ID缺失或重复")
            source_ids[sid] = sname
        route_new = 0
        for phone, valid in valid_rows.items():
            observed = eligible_rows[phone]
            old_id, old_name = str(valid.get("原运营商ID") or ""), str(valid.get("精确原运营商") or "")
            require(old_id and old_name and source_ids.get(old_id) == old_name, "精确写前来源无法唯一证明")
            retention_name_by_id = {str(item["carrierIdentif"]): carrier for carrier, item in by_carrier.items()}
            require((old_name not in CARRIERS and old_id not in retention_name_by_id) or retention_name_by_id.get(old_id) == old_name, "写前保留区精确名称与ID矛盾，不能计为新增")
            require(carrier_id(observed) == old_id and identity(valid) == identity(observed) and bool(identity(valid)), "有效候选与写前后台身份不一致")
            if observed.get("carrierName"):
                require(observed["carrierName"] == old_name, "写前运营商名称与精确来源不一致")
            require(str(valid.get("目标运营商ID") or "") == str(target["carrierIdentif"]) and valid.get("目标运营商") == name, "有效候选目标ID/名称不一致")
            current = population_rows.get(phone) or []
            require(sum(item.get("carrierName") == name and identity(item) == identity(valid) for item in current) == 1, "独立结束存量未证实写入号码仍在目标保留区")
            detail = {"phone10": phone, "backendId": identity(valid), "sourceCarrierName": old_name, "sourceCarrierIdentif": old_id, "targetCarrierName": name, "targetCarrierIdentif": str(target["carrierIdentif"]), "route": route}
            if old_name in CARRIERS:
                existing_transfers.append(detail)
            else:
                new_transfers.append(detail)
                route_new += 1
        proof["newTransferredCount"] = route_new
        proofs.append(proof)
    require(seen_routes == (set() if transfer_removed else set(ROUTES)), "任务D两个保留区路由不完整")
    summary = {
        "schemaVersion": 1, "task": "D", "status": "passed", "runId": run_id,
        **({"retentionCarrierTransferEnabled": False, "retentionCarrierTransferStatus": "removed_by_user"}
           if transfer_removed else {}),
        "newTransferredCount": len(new_transfers), "totalInRetentionCount": len(population_rows),
        "byCarrier": [{"carrierName": name, "newTransferredCount": sum(row["targetCarrierName"] == name for row in new_transfers), "totalInRetentionCount": len(by_carrier[name]["rowMap"])} for name in ROUTES.values()],
        "asOf": population["completedAt"], "businessCompletedAt": bundle["businessCompletedAt"],
        "countingUnit": "unique_phone10", "populationReceipt": str(population_path), "populationSha256": sha256(population_path),
        "writeGroupsReceipt": str(groups_path) if groups_path else None,
        "writeGroupsSha256": sha256(groups_path) if groups_path else None,
        "newTransfers": sorted(new_transfers, key=lambda row: row["phone10"]),
        "withinRetentionTransfers": sorted(existing_transfers, key=lambda row: row["phone10"]),
        "proofGroups": proofs,
    }
    if output_path is not None:
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--population", required=True)
    parser.add_argument("--write-groups", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = build_summary(args.population, args.write_groups, args.run_id, args.output)
    compact = {key: result[key] for key in ("runId", "status", "newTransferredCount", "totalInRetentionCount", "byCarrier", "asOf", "countingUnit", "populationReceipt", "populationSha256")}
    print(json.dumps({**compact, "path": str(Path(args.output).resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
