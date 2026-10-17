#!/usr/bin/env python3
"""Local, receipt-only per-phone Task D reporting. Never submits/retries business."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
from opensms_task_d_policy import population_statistics_removed

TASK_D_LABEL = "换号区下卡通知"
TASK_E_LABEL = "过期卡发送下卡通知"
PHASES = {"swap_zero": "换号区分支", "retention_verified": "卡保留分支"}
STATUS = {"success": "成功（同步事务确认）", "failed": "失败（后台明确未命中，未写入）",
          "uncertain": "提交结果不确定", "unprocessed": "未执行", "skipped": "筛选未命中，未提交",
          "unconfigured": "未配置运营商，按用户规则跳过", "retired": "已删除运营商，未提交通知",
          "data_anomaly": "异常数据跳过，待人工处理"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def cards(values):
    require(isinstance(values, list), "缺少逐号清单")
    result = []
    for value in values:
        require(isinstance(value, str) and bool(re.fullmatch(r"1?\d{10}", value)), "号码不是10位或带1前缀的11位文本")
        result.append(value[-10:])
    require(len(set(result)) == len(result), "同一清单存在重复号码")
    return result


def notification_rows(notification, evidence=None):
    """Keep transaction failures, uncertain submissions and unsubmitted rows distinct."""
    phase, expire = notification.get("phase"), notification.get("expireDate")
    require(notification.get("task") == "D" and phase in PHASES, "通知任务/分支不一致")
    require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", expire or "")), "通知日期缺失")
    expected = cards(notification.get("cardNos"))
    require(notification.get("candidateCount") == len(expected), "通知数量与号码清单不一致")
    result = {p: {"phone": p, "phase": phase, "expireDate": expire,
                  "status": "unprocessed", "reason": "未发现该号码的提交回执或提交断点"} for p in expected}
    if evidence is None:
        return list(result.values())
    require(evidence.get("task") == "D" and evidence.get("phase") == phase
            and evidence.get("expireDate") == expire
            and evidence.get("candidateCount") == len(expected), "事务回执与通知身份/日期/数量不一致")
    require(evidence.get("validateOnly") is not True, "仅验证候选，不是通知执行回执")
    seen = set()
    batches = evidence.get("batches") or []
    require(isinstance(batches, list), "批次回执格式错误")
    pending = evidence.get("pendingBatch")
    entries = [(batch, batch.get("serverResponse") or {}, True) for batch in batches]
    if pending:
        entries.append((pending, pending.get("response") or {}, False))
    for number, (batch, raw, finalized) in enumerate(entries, 1):
        batch_cards = cards(batch.get("cardNos"))
        require(0 < len(batch_cards) <= 20 and batch.get("batchNumber") == number
                and batch.get("expireDate") == expire, "批次日期、顺序或大小错误")
        require(set(batch_cards) <= set(expected) and not seen.intersection(batch_cards), "批次含集合外号码或重复提交号码")
        seen.update(batch_cards)
        size = len(batch_cards)
        data = raw.get("data") or {}
        matched, updates = data.get("matchedCardCount"), data.get("updatedChatCount")
        try:
            missing = set(cards(data.get("notFound")))
        except ValueError:
            missing = None
        complete = (raw.get("code") == 200 and type(data.get("inputCount")) is int
                    and data["inputCount"] == size and type(matched) is int
                    and type(updates) is int and updates >= 0 and missing is not None
                    and missing <= set(batch_cards) and matched >= 0 and matched + len(missing) == size)
        confirmed = complete and matched == size and not missing
        if finalized:
            confirmed = confirmed and batch.get("businessReceiptPassed") is True and batch.get("transactionalVerificationPassed") is True
            confirmed = confirmed and all(batch.get(k) == data.get(k) for k in ("inputCount", "matchedCardCount", "updatedChatCount", "notFound"))
        for phone in batch_cards:
            if confirmed:
                status, reason = "success", "原始同步事务的输入、命中、更新数量与本批号码逐项核对通过"
                if updates == 0:
                    reason = "backend_matched_no_change：同步事务全部命中，更新会话0；不等于未命中"
            elif complete and phone in missing:
                status, reason = "failed", "backend_not_found：后台事务明确返回该号码未命中"
            else:
                status, reason = "uncertain", "已进入提交阶段，但没有可完整核对的成功事务证明；禁止自动重试"
            result[phone].update(status=status, reason=reason, batchNumber=number)
    return list(result.values())


def build_details(config):
    run_id = config.get("runId")
    if (config.get("taskAOnly") is True or str(config.get("taskDStatus") or "").startswith("skipped")
            or not (config.get("taskDExecuted") is True or config.get("taskDCurrentUserAuthorized") is True)):
        return {"status": "skipped", "rows": [], "retentionRows": [], "warnings": []}
    result = {"status": "passed", "runId": run_id, "rows": [], "retentionRows": [],
              "retentionCarrierTransferEnabled": config.get("taskDRetentionCarrierTransferEnabled"),
              "warnings": [], "evidence": [], "liveBusinessExecuted": False}
    population_removed = population_statistics_removed(config, report=True)
    result["retentionPopulationRemoved"] = population_removed
    manifest_ref = config.get("taskDManifest")
    if not manifest_ref:
        result.update(status="incomplete", warnings=["本轮任务D准备清单未生成，无法逐号核验"])
        return result
    manifest_path = Path(manifest_ref).resolve()
    base = manifest_path.parent

    def read_bound(path):
        path = Path(path).resolve()
        require(bool(run_id) and path.parent == base and run_id in path.name, "回执路径不属于精确本轮目录/运行编号")
        blob = path.read_bytes()
        value = json.loads(blob)
        require(isinstance(value, dict) and value.get("runId", run_id) == run_id, "回执运行编号不符")
        return value, {"path": str(path), "sha256": hashlib.sha256(blob).hexdigest()}

    try:
        manifest, proof = read_bound(manifest_path)
        require(manifest.get("task") == "D" and manifest.get("runId") == run_id, "任务D清单身份错误")
        result["evidence"].append(proof)
    except (ValueError, OSError) as exc:
        result.update(status="incomplete", warnings=[str(exc)])
        return result

    def branch(phase, path_key, config_key):
        notification, source_proof = read_bound(manifest[path_key])
        require(notification.get("phase") == phase, "通知清单分支错误")
        receipt_path = config.get(config_key) or base / f"opensms_task_d_{phase}_{run_id}_receipt.json"
        if not Path(receipt_path).is_file() and not config.get(config_key):
            receipt_path = base / f"opensms_task_d_{phase}_{run_id}_progress.json"
        evidence, proofs = None, [source_proof]
        if Path(receipt_path).is_file():
            evidence, proof = read_bound(receipt_path)
            require(Path(evidence.get("manifestPath") or "").resolve() == Path(manifest[path_key]).resolve(), "事务回执未绑定该通知清单")
            proofs.append(proof)
        elif config.get(config_key):
            raise ValueError("已声明的任务D事务回执缺失")
        return notification_rows(notification, evidence), proofs

    def retention():
        summary_ref = config.get("taskDRetentionSummaryReceipt")
        require(bool(summary_ref), "保留区逐号统计未确认，不能用卡保留申请或候选代替")
        stored, summary_proof = read_bound(summary_ref)
        population_path = stored.get("populationReceipt")
        groups_path = stored.get("writeGroupsReceipt") or config.get("taskDRetentionGroupsReceipt")
        population, population_proof = read_bound(population_path)
        _, groups_proof = read_bound(groups_path)
        require(stored.get("populationSha256") == population_proof["sha256"]
                and stored.get("writeGroupsSha256") == groups_proof["sha256"], "保留区来源SHA-256不一致")
        from opensms_task_d_retention_summary import build_summary
        verified = build_summary(population_path, groups_path, run_id)
        for key in ("status", "newTransferredCount", "totalInRetentionCount", "newTransfers", "withinRetentionTransfers", "byCarrier", "asOf"):
            require(stored.get(key) == verified.get(key), "保留区逐号独立重算与原统计不一致")
        new = {row["phone10"]: row for row in verified["newTransfers"]}
        rows = [{"phone": row["phone10"], "carrier": group["carrierName"], "new": row["phone10"] in new,
                 "sourceCarrier": new.get(row["phone10"], {}).get("sourceCarrierName", ""), "asOf": verified["asOf"]}
                for group in population["groups"] for row in group["rows"]]
        rows = list({(row["phone"], row["carrier"]): row for row in rows}.values())
        require(len(rows) == verified["totalInRetentionCount"] and sum(row["new"] for row in rows) == verified["newTransferredCount"], "保留区明细与总数不一致")
        return sorted(rows, key=lambda row: (row["carrier"], row["phone"])), [summary_proof, population_proof, groups_proof]

    # Current contracts need only two notification branches. Inventory remains
    # readable exclusively for historical version-one receipts.
    with ThreadPoolExecutor(max_workers=2 if population_removed else 3) as pool:
        jobs = [("rows", pool.submit(branch, "swap_zero", "swapNotificationPath", "taskDSwapExpiryReceipt")),
                ("rows", pool.submit(branch, "retention_verified", "retentionNotificationPath", "taskDRetentionExpiryReceipt"))]
        if not population_removed:
            jobs.append(("retentionRows", pool.submit(retention)))
        for key, job in jobs:
            try:
                rows, proofs = job.result()
                result[key].extend(rows)
                result["evidence"].extend(proofs)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                result["warnings"].append(str(exc))
    try:
        skipped = cards(manifest.get("filteredUnmatchedCards") or [])
        require(manifest.get("filteredUnmatchedCount", 0) == len(skipped), "筛选未命中明细与数量不一致")
        result["rows"].extend({"phone": p, "phase": "retention_verified", "expireDate": manifest.get("retentionExpireDate", ""),
                               "status": "skipped", "reason": "未被chatr/koo/telus1/kini筛选返回；未提交本分支，不等于确认系统无号"} for p in skipped)
        unconfigured = cards(manifest.get("unconfiguredCarrierSkippedCards", []))
        require(manifest.get("unconfiguredCarrierSkippedCount", 0) == len(unconfigured), "未配置运营商明细与数量不一致")
        retired = cards(manifest.get("retiredCarrierSkippedCards", []))
        require(manifest.get("retiredCarrierSkippedCount", 0) == len(retired), "已删除运营商明细与数量不一致")
        anomalies = cards(manifest.get("dataAnomalySkippedCards", []))
        require(manifest.get("dataAnomalySkippedCount", 0) == len(anomalies), "异常数据明细与数量不一致")
        if unconfigured or retired or anomalies:
            from opensms_parallel_audit import validate_task_d_filtered_partition
            receipt, proof = read_bound(manifest["retentionReceiptPath"])
            notification, notification_proof = read_bound(manifest["retentionNotificationPath"])
            require(notification.get("retentionReceiptSha256") == proof["sha256"], "未配置运营商来源哈希不符")
            verified = validate_task_d_filtered_partition(receipt, run_id)
            require(set(unconfigured) == verified["unconfigured"], "未配置运营商排除与原始分页不符")
            require(set(retired) == verified["retired"], "已删除运营商排除与原始分页不符")
            require(set(anomalies) == verified["dataAnomalies"], "异常数据排除与原始分页不符")
            require(not set(unconfigured).intersection(row["phone"] for row in result["rows"]
                    if row["phase"] == "retention_verified"), "未配置运营商号码混入通知或未命中清单")
            result["evidence"].extend([proof, notification_proof])
            result["rows"].extend({"phone": p, "phase": "retention_verified", "expireDate": "",
                "status": "unconfigured", "reason": "后台明确未配置运营商；按用户规则排除，未提交通知、不修改运营商"} for p in unconfigured)
            require(not set(retired).intersection(row["phone"] for row in result["rows"]
                    if row["phase"] == "retention_verified"), "已删除运营商号码混入通知或其它排除清单")
            result["rows"].extend({"phone": p, "phase": "retention_verified", "expireDate": "",
                "status": "retired", "reason": f"{verified['matched'][p]['retiredCarrierName']}已删除，号码残留旧ID；未提交通知、不分配新运营商"}
                for p in retired)
            require(not set(anomalies).intersection(row["phone"] for row in result["rows"]
                    if row["phase"] == "retention_verified"), "异常数据混入通知或其它排除清单")
            result["rows"].extend({"phone": p, "phase": "retention_verified", "expireDate": "",
                "status": "data_anomaly", "reason": verified["matched"][p]["dataAnomaly"] + "；未提交，待人工处理"}
                for p in anomalies)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result["warnings"].append(str(exc))
    result["counts"] = dict(Counter(row["status"] for row in result["rows"]))
    result["successUniquePhoneCount"] = len({row["phone"] for row in result["rows"] if row["status"] == "success"})
    if result["warnings"]:
        result["status"] = "incomplete"
    return result


def cell(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_details(result):
    if result["status"] == "skipped":
        return []
    lines = ["", "#### 任务D下卡通知逐号结果", "",
             "成功只按原始同步事务回执确认；后台没有到期日独立查询接口。筛选未命中是未提交，不是提交失败。",
             "以下日期是本轮任务D时点；后续任务E或人工修改可能覆盖。两分支同号分别列出，数量单位为号次。", ""]
    if result.get("retentionCarrierTransferEnabled") is False:
        lines.extend([("卡保留通知保持原运营商；保留两个通知分支，不查询已删除运营商的库存。"
                       if result.get("retentionPopulationRemoved") else
                       "转入B14保留区工作已删除：本轮运营商候选0、修改0；保留两个通知分支，现有保留区仅做只读统计。"), ""])
    for state in ("success", "failed", "uncertain", "unprocessed", "skipped", "unconfigured", "retired", "data_anomaly"):
        rows = [row for row in result["rows"] if row["status"] == state]
        lines.extend([f"##### {STATUS[state]}：{len(rows)} 号次" + ("（仅已核验明细，覆盖不完整）" if result["warnings"] else ""), ""])
        if rows:
            lines.extend(["| 手机号 | 分支 | 下卡日期 | 结果依据 |", "| --- | --- | --- | --- |"])
            lines.extend(f"| {row['phone']} | {PHASES[row['phase']]} | {cell(row['expireDate'])} | {cell(row['reason'])} |" for row in rows)
        else:
            lines.append("无已核验的此类号码。")
        lines.append("")
    rows = result["retentionRows"]
    if not result.get("retentionPopulationRemoved"):
        lines.extend(["#### 任务D完成后保留区号码明细", ""])
    if rows and not result.get("retentionPopulationRemoved"):
        lines.extend([f"确认共 {len(rows)} 个号码，本轮新转入 {sum(row['new'] for row in rows)} 个；统计时点 {cell(rows[0]['asOf'])}。", "",
                      "| 手机号 | 保留区 | 本轮从区外新转入 | 写前来源（新增号码） |", "| --- | --- | --- | --- |"])
        lines.extend(f"| {row['phone']} | {cell(row['carrier'])} | {'是' if row['new'] else '否'} | {cell(row['sourceCarrier'])} |" for row in rows)
    elif not result.get("retentionPopulationRemoved"):
        lines.append("无可展示的保留区逐号记录；以统计回执是否完整通过区分空区与未知。")
    if result["warnings"]:
        lines.extend(["", "#### 明细证据未确认项", "", *[f"- {cell(warning)}" for warning in result["warnings"]]])
    lines.extend(["", "明细只读取本轮本地回执，不触发通知、重试或运营商修改。", ""])
    return lines


def report_lines(config):
    return render_details(build_details(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    result = build_details(config)
    lines = [f"# 任务D：{TASK_D_LABEL}（历史回执明细补充）", "", f"运行编号：{config.get('runId')}。不是新一轮导出或后台当前状态。",
             f"任务E备注：{TASK_E_LABEL}。名称修改不改变原有业务筛选规则。", *render_details(result)]
    lines.extend(["## 原始证据", "", *[f"- [{Path(p['path']).name}](<{p['path']}>)；SHA-256 `{p['sha256']}`。" for p in result.get("evidence", [])]])
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    with output.open("x", encoding="utf-8") as handle:
        handle.write(content)
    require(output.read_text(encoding="utf-8") == content, "MD独立回读不一致")
    population_counts = ({} if result.get("retentionPopulationRemoved") else
        {"retentionCount": len(result["retentionRows"]),
         "newRetentionCount": sum(row["new"] for row in result["retentionRows"])})
    print(json.dumps({"status": result["status"], "runId": result.get("runId"), "counts": result.get("counts"),
                      "successUniquePhoneCount": result.get("successUniquePhoneCount"),
                      **population_counts, "warnings": result["warnings"],
                      "retentionPopulationRemoved": result.get("retentionPopulationRemoved", False),
                      "output": str(output), "independentFileReadbackPassed": True, "liveBusinessExecuted": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
