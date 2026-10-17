#!/usr/bin/env python3
"""Local-only Task D notification counts, derived from exact confirmed batches.

This module never starts a business task or contacts a server. Session-update
counts are not phone counts. The last date below is only within Task D; a later
Task E or a manual change can supersede it.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re

PHASES = ("swap_zero", "retention_verified")


def require(condition, message):
    if not condition:
        raise ValueError(f"任务D通知统计：{message}")


def phones(values):
    require(isinstance(values, list), "缺少逐号清单")
    require(all(isinstance(v, str) and re.fullmatch(r"[0-9]{10}", v) for v in values), "卡号格式错误")
    require(len(values) == len(set(values)), "单分支出现重复卡号")
    return set(values)


def build_summary(run_id, branches):
    """Each branch carries its notification, receipt and exact notification path."""
    require(len(branches) == 2 and bool(run_id), "缺少运行编号或两个分支")
    results, confirmed = [], []
    for phase, branch in zip(PHASES, branches):
        notification, receipt = branch["notification"], branch["receipt"]
        cards = phones(notification.get("cardNos"))
        n = len(cards)
        require(notification.get("task") == receipt.get("task") == "D", "任务标识错误")
        require(notification.get("phase") == receipt.get("phase") == phase, "分支顺序或标识错误")
        require(notification.get("forceGate") is True and notification.get("automationGate") is True, "缺少授权证明")
        require(notification.get("runId", run_id) == run_id and receipt.get("runId", run_id) == run_id, "运行编号不一致")
        require(Path(receipt.get("manifestPath") or "").resolve() == Path(branch["notificationPath"]).resolve(), "事务回执未绑定通知清单")
        server_date, expire_date = notification.get("serverDate"), notification.get("expireDate")
        require(receipt.get("serverDate") == server_date and receipt.get("expireDate") == expire_date, "回执日期不一致")
        require(type(notification.get("candidateCount")) is int and notification["candidateCount"] == n
                and type(receipt.get("candidateCount")) is int and receipt["candidateCount"] == n, "候选数不一致")
        require(receipt.get("status") in {"passed", "no_candidate", "recovered_transactional_receipt"}
                and receipt.get("validateOnly") is not True, "分支未实际确认完成")
        require(receipt.get("businessReceiptPassed") is True and receipt.get("transactionalVerificationPassed") is True
                and receipt.get("verificationMode") == "synchronous_transactional_business_receipt", "同步事务证明未通过")
        batches = receipt.get("batches")
        require(isinstance(batches, list), "缺少批次回执")
        actual, updates = [], 0
        for number, batch in enumerate(batches, 1):
            batch_cards = phones(batch.get("cardNos"))
            size = len(batch_cards)
            require(0 < size <= 20 and batch.get("batchNumber") == number, "批大小或顺序错误")
            require(batch.get("expireDate") == expire_date, "批次日期错误")
            raw = batch.get("serverResponse") or {}
            payload = raw.get("data") or {}
            require(raw.get("code") == 200, "原始后台回执未成功")
            for obj in (batch, payload):
                require(all(type(obj.get(key)) is int and obj[key] == size for key in ("inputCount", "matchedCardCount")), "输入/命中未精确覆盖本批")
                require(type(obj.get("updatedChatCount")) is int and obj["updatedChatCount"] >= 0
                        and obj.get("notFound") == [], "批次存在未确认写入")
            require(batch["updatedChatCount"] == payload["updatedChatCount"], "更新会话数与原始回执不一致")
            require(batch.get("businessReceiptPassed") is True and batch.get("transactionalVerificationPassed") is True, "批次未确认")
            actual.extend(batch["cardNos"])
            updates += batch["updatedChatCount"]
        require(phones(actual) == cards, "已确认批次与通知清单不是同一组号码")
        for key, value in (("inputCount", n), ("matchedCardCount", n), ("updatedChatCount", updates)):
            # Empty successful branches intentionally have no top-level totals.
            require((n == 0 and key not in receipt) or (type(receipt.get(key)) is int and receipt[key] == value), "分支总数与批次不一致")
        require(n == 0 or receipt.get("status") != "no_candidate", "有号码却标记无候选")
        results.append({"phase": phase, "expireDate": expire_date, "candidateCount": n,
                        "confirmedPhoneCount": n, "batchCount": len(batches), "updatedChatCount": updates})
        confirmed.append(cards)
    today = branches[0]["notification"]["serverDate"]
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", today or ""), "服务器日期无效")
    require(branches[1]["notification"]["serverDate"] == today
            and results[0]["expireDate"] == today
            and results[1]["expireDate"] == (date.fromisoformat(today) + timedelta(days=7)).isoformat(), "日期不是服务器当天及加7天")
    overlap = confirmed[0] & confirmed[1]
    return {"version": 1, "status": "passed", "runId": run_id, "countingUnit": "unique_phone10",
            "confirmedSubmissionCount": sum(map(len, confirmed)),
            "uniqueNotifiedPhoneCount": len(confirmed[0] | confirmed[1]), "overlapPhoneCount": len(overlap),
            "byPhase": results,
            "lastTaskDNotificationByDate": [
                {"expireDate": results[0]["expireDate"], "phoneCount": len(confirmed[0] - confirmed[1])},
                {"expireDate": results[1]["expireDate"], "phoneCount": len(confirmed[1])}],
            "verificationMode": "synchronous_transactional_business_receipt",
            "independentExpiryReadbackAvailable": False, "scope": "task_d_only_before_later_tasks"}


def build_from_files(run_id, manifest_path, swap_receipt, retention_receipt):
    manifest_path = Path(manifest_path).resolve()
    manifest_blob = manifest_path.read_bytes()
    manifest = json.loads(manifest_blob)
    require(manifest.get("runId") == run_id and manifest.get("task") == "D"
            and run_id in manifest_path.name, "准备清单身份不符")
    paths = [Path(manifest["swapNotificationPath"]), Path(swap_receipt),
             Path(manifest["retentionNotificationPath"]), Path(retention_receipt)]
    preparation_dir = manifest_path.parent
    receipt_dir = preparation_dir
    # Only the runner's exact same-run private preparation-attempt layout may
    # separate read-only notifications from existing transactional receipts.
    if preparation_dir.parent.name == "task_d_preparation_attempts":
        receipt_dir = preparation_dir.parent.parent
        require(re.fullmatch(r"[0-9]+", preparation_dir.name) is not None
                and receipt_dir.name == "receipts" and receipt_dir.parent.name == run_id,
                "准备尝试目录不属于本轮")
    def same_run_path(index, path):
        file = path.resolve()
        if run_id not in file.name:
            return False
        expected = preparation_dir if index in (0, 2) else receipt_dir
        if file.parent == expected:
            return True
        # Only the exact retention receipt in the same run's numeric private
        # continuation layout is allowed, not arbitrary descendant directories.
        return (index == 3 and file.parent.parent.name == 'task_d_continuation_attempts'
                and re.fullmatch(r'[0-9]+', file.parent.name) is not None
                and file.parent.parent.parent == receipt_dir
                and receipt_dir.name == 'receipts' and receipt_dir.parent.name == run_id)
    require(all(same_run_path(index, path) for index, path in enumerate(paths)),
            "通知或事务回执不属于本轮目录/运行编号")
    # Four immutable same-run local files can be read concurrently; no business
    # write, live query or workbook mutation is issued by this worker pool.
    with ThreadPoolExecutor(max_workers=4) as pool:
        blobs = list(pool.map(Path.read_bytes, paths))
    values = [json.loads(blob) for blob in blobs]
    require(values[2].get("runId") == run_id, "卡保留通知运行编号不符")
    source = Path(manifest.get("sourcePath") or "").resolve()
    require(source.is_file() and run_id in source.name, "本轮源工作簿缺失")
    require(Path(values[0].get("sourcePath") or "").resolve() == source
            and values[0].get("sourceSha256") == manifest.get("sourceSha256"), "换号区通知未绑定本轮源工作簿")
    with source.open("rb") as handle:
        source_sha = hashlib.file_digest(handle, "sha256").hexdigest()
    require(source_sha == manifest.get("sourceSha256"), "本轮源工作簿SHA-256不符")
    result = build_summary(run_id, [
        {"notificationPath": str(paths[i]), "notification": values[i], "receipt": values[i + 1]}
        for i in (0, 2)])
    result["evidence"] = [{"path": str(path.resolve()), "sha256": hashlib.sha256(blob).hexdigest()}
                          for path, blob in zip(paths, blobs)]
    result["evidence"].extend([
        {"path": str(manifest_path), "sha256": hashlib.sha256(manifest_blob).hexdigest()},
        {"path": str(source), "sha256": source_sha}])
    return result


def confirmed_summary(summary, run_id=None):
    """Presentation is fail-soft for incomplete/older checkpoints, not zero-filled."""
    if (not isinstance(summary, dict) or summary.get("status") != "passed"
            or not run_id or summary.get("runId") != run_id
            or summary.get("version") != 1 or summary.get("countingUnit") != "unique_phone10"
            or summary.get("scope") != "task_d_only_before_later_tasks"):
        return False
    if any(type(summary.get(key)) is not int or summary[key] < 0 for key in (
        "confirmedSubmissionCount", "uniqueNotifiedPhoneCount", "overlapPhoneCount")):
        return False
    rows = summary.get("byPhase")
    dates = summary.get("lastTaskDNotificationByDate")
    if not isinstance(rows, list) or len(rows) != 2 or not isinstance(dates, list) or len(dates) != 2:
        return False
    for phase, row, last in zip(PHASES, rows, dates):
        if (not isinstance(row, dict) or not isinstance(last, dict) or row.get("phase") != phase
                or not isinstance(row.get("expireDate"), str) or last.get("expireDate") != row["expireDate"]
                or any(type(row.get(k)) is not int or row[k] < 0 for k in ("confirmedPhoneCount", "batchCount"))
                or type(last.get("phoneCount")) is not int or last["phoneCount"] < 0):
            return False
    first, second = (row["confirmedPhoneCount"] for row in rows)
    overlap = summary["overlapPhoneCount"]
    return (overlap <= min(first, second) and summary["confirmedSubmissionCount"] == first + second
            and summary["uniqueNotifiedPhoneCount"] == first + second - overlap
            and dates[0]["phoneCount"] == first - overlap and dates[1]["phoneCount"] == second)


def summary_lines(summary, run_id=None):
    if not confirmed_summary(summary, run_id):
        return []
    lines = [f"- 任务D下卡通知：确认提交 {summary['confirmedSubmissionCount']} 号次；"
             f"去重 {summary['uniqueNotifiedPhoneCount']} 个号码；两分支重复 {summary['overlapPhoneCount']} 个（后执行的+7天标记覆盖当天标记）。"]
    for row in summary["byPhase"]:
        label = "换号区分支" if row["phase"] == "swap_zero" else "卡保留分支"
        lines.append(f"- {label}：{row['confirmedPhoneCount']} 个，{row['batchCount']} 批，到期时间 {row['expireDate']}。")
    lines.append("- 以上是任务D同步事务确认结果，不是到期日独立查询；后续任务E或人工修改可能覆盖该日期。")
    return lines


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run-id", "manifest", "swap-receipt", "retention-receipt"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    print(json.dumps(build_from_files(args.run_id, args.manifest, args.swap_receipt, args.retention_receipt), ensure_ascii=False, indent=2))
