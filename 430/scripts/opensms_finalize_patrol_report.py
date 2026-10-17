#!/usr/bin/env python3
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from opensms_task_d_report_details import TASK_D_LABEL, TASK_E_LABEL, report_lines as task_d_phone_detail_lines
from opensms_review_expiry_carriers import review as review_expiry_carriers, report_lines as expiry_carrier_lines
from opensms_unexpired_a11_restore import report_lines as unexpired_restore_lines
from opensms_report_summary import compact_phone_tables
from opensms_carrier_reply_comparison import report_lines as carrier_reply_lines
from opensms_scope_comparison import report_lines as scope_comparison_lines
from opensms_device_reply_periods import report_lines as device_reply_period_lines
from opensms_v_swap_daily import report_lines as v_swap_lines
from opensms_task_d_policy import population_statistics_removed
from services.opensms_document_api.pipeline.room_mapping import CONFIG as ROOM_CONFIG, range_text


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def count_candidates(data):
    value = data.get("count")
    if value is not None:
        return int(value)
    return len(data.get("rows") or data.get("candidates") or [])


def group_metrics(group):
    candidate = load(group["candidate"])
    candidate_count = count_candidates(candidate)
    metrics = {
        "candidate": candidate_count,
        "valid": int(group.get("backendValid", 0)),
        "already": 0,
        "left": 0,
        "missing": 0,
        "duplicate": 0,
        "actual": 0,
        "success": 0,
        "failure": 0,
        "verify_target": 0,
        "verify_non_target": 0,
        "verify_missing": 0,
        "verify_duplicate": 0,
        "passed": group.get("result", "未执行"),
    }
    if group.get("write"):
        receipt = load(group["write"])
        before = receipt.get("before") or receipt.get("prep") or {}
        after = receipt.get("after") or {}
        metrics["already"] = int(before.get("alreadyTargetCount") or 0)
        metrics["left"] = int(before.get("nonSourceCount") or len(before.get("nonSource") or []))
        metrics["missing"] = int(before.get("missingCount") or len(before.get("missingPhones") or []))
        metrics["duplicate"] = int(before.get("duplicateBackendCount") or len(before.get("duplicateBackend") or []))
        metrics["valid"] = max(
            candidate_count - metrics["left"] - metrics["missing"] - metrics["duplicate"],
            0,
        )
        metrics["actual"] = int(before.get("eligibleCount") or 0)
        target_count = int(after.get("targetCount") or 0)
        metrics["success"] = max(target_count - metrics["already"], 0)
        metrics["failure"] = max(metrics["actual"] - metrics["success"], 0)
    if group.get("verify"):
        verify = load(group["verify"])
        metrics["verify_target"] = int(verify.get("targetCount") or 0)
        metrics["verify_non_target"] = int(verify.get("stillNotTargetCount") or 0)
        metrics["verify_missing"] = int(verify.get("missingCount") or 0)
        metrics["verify_duplicate"] = int(verify.get("duplicateBackendCount") or 0)
        metrics["passed"] = "通过" if verify.get("passed") else "未通过"
        if verify.get("verificationStatus") == "not_required" and group.get("noWrite"):
            # Final audit validates this immutable stage proof; do not claim an
            # empty per-group verification actually read any backend records.
            metrics["passed"] = "无写入；阶段全量回读" if group.get("stageReadbackReceipt") else "无写入；待阶段回读"
    return candidate, metrics


def pct(value, digits=3):
    return "未知" if value is None else f"{float(value):.{digits}f}%"


def signed_change(value, digits=2, suffix="%"):
    """A missing/zero-denominator comparison is unknown, not a zero change."""
    return "未知" if value is None else f"{float(value):+.{digits}f}{suffix}"


def path_line(label, path):
    absolute = str(Path(path).resolve())
    return [f"[{label}]({absolute})", absolute]


def device_daily_report_lines(card_report):
    proof = (card_report.get("process") or {}).get("device_daily_statistics")
    if not proof:  # Historical report receipts remain readable, never invent it.
        return []
    review = (card_report.get("verification") or {}).get("device_daily_statistics_review") or {}
    return [
        f"- 设备每日统计：{proof.get('device_count', '未知')}台设备，{proof.get('date_count', '未知')}个日期，{proof.get('source_rows', '未知')}条主表记录；详见“设备每日统计”工作表。",
        f"- 设备每日统计独立重算：{'通过' if review.get('matches') is True else '未通过'}；日期并发{proof.get('actual_workers', '未知')}线程。",
        "- 设备每日回复率：同设备同日回复窗口数合计 ÷ 发送窗口数合计，不平均号码回复率；总发送量、总回复量分别求和。",
        f"- 设备统计缺失项：未识别设备记录{proof.get('unrecognized_device_rows', '未知')}条；空白或非法源计数单元格{proof.get('missing_or_invalid_count_cells', '未知')}个；对应合计留空，零发送分母的回复率留空。",
    ]


def final_expiry_carrier_lines(config):
    path = config.get('cardResult')
    if not path or not Path(path).is_file() or not config.get('serverDate'):
        return ["- 截止日与现有分区：缺少可读取的最终工作簿或服务器日期，未核验；不能宣称存量分区正确。"]
    return expiry_carrier_lines(review_expiry_carriers(path, config['serverDate']))


def card_retention_report_lines(card_report):
    proof = (card_report.get('process') or {}).get('card_retention_report')
    if not proof:
        return []
    review = (card_report.get('verification') or {}).get('card_retention_report_review') or {}
    return [
        f"- 任务A卡保留记录：{proof['record_count']}条、{proof['unique_phone_count']}个去重号码；200条/页，共{proof['page_count']}页，两遍完整读取核对。",
        f"- 需要保留时间：位于使用天数右侧，按号码末10位匹配申请时间createTime，同号取最新一次；已填{proof['filled_rows']}行，匹配{proof['matched_phone_count']}个号码。不是新到期日，不参与运营商或下卡通知判断。",
        f"- 卡保留未关联主表：{proof['unmatched_phone_count']}个号码；申请时间空白/无效：{proof['unknown_request_time_phone_count']}个号码，列留空，完整记录仍在卡保留明细sheet中。",
        f"- 卡保留独立核验：{'通过' if review.get('matches') is True else '未通过'}；实际最大只读并发{proof['read_concurrency']}；全量记录、去重号码和原始字段已并入本工作簿。独立任务G已移除，历史文件未删除。",
    ]


def chatr_google_date_report_lines(config, card_report):
    """Describe scoped date evidence without presenting legacy Google tabs."""
    if config.get("googleChatrDateFallbackEnabled") is not True:
        return []
    proof = (card_report.get("process") or {}).get("google_chatr_date_fallback") or {}
    review = (card_report.get("verification") or {}).get("google_chatr_date_fallback_review") or {}
    path = config.get("chatrGoogleDates")
    expected = config.get("chatrGoogleDatesSha256")
    if (proof.get("enabled") is not True or proof.get("status") != "passed"
            or proof.get("run_id") != config.get("runId") or review.get("matches") is not True
            or proof.get("scope") != "chatr_blank_activation_and_cutoff_only"
            or proof.get("source") != "current_run_system_then_fresh_google"
            or any(review.get(key) != value for key, value in proof.items())
            or proof.get("snapshot_sha256") != expected or not path or not expected):
        return ["- Chatr Google日期补缺：已配置，但本轮来源、快照或独立回读未确认；不将补值计为核验成功。"]
    snapshot_path = Path(path)
    if not snapshot_path.is_file() or hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != expected:
        return ["- Chatr Google日期补缺：日期快照文件或SHA-256未确认。"]
    snapshot = load(snapshot_path)
    if (snapshot.get("status") != "passed" or snapshot.get("runId") != config.get("runId")
            or snapshot.get("sourceSha256") != config.get("chatrGoogleSourceSha256")):
        return ["- Chatr Google日期补缺：日期快照未绑定本轮来源与运行编号。"]
    fields = proof.get("field_counts") or {}
    lines = [
        f"- Chatr Google日期来源：本轮下载源表 {snapshot.get('totalSourceRows', '未知')} 行；Chatr来源 {snapshot.get('chatrSourceRows', '未知')} 行；排除非Chatr {snapshot.get('ignoredNonChatrRows', '未知')} 行。",
        f"- Chatr日期冻结快照：{snapshot.get('recordCount', '未知')} 个唯一卡槽；本轮主表Chatr {proof.get('chatr_rows', '未知')} 行；同卡槽匹配 {proof.get('matched_rows', '未知')} 行（ICCID冲突不补Google日期）；未匹配 {proof.get('unmatched_rows', '未知')} 行。",
        f"- Chatr实际补值：{proof.get('filled_rows', '未知')} 行（激活日期 {fields.get('激活日期', '未知')}，截止日 {fields.get('截止日', '未知')}）；补值后仍缺激活日期 {proof.get('missing_activation_rows', '未知')} 行，仍缺截止日 {proof.get('missing_cutoff_rows', '未知')} 行。",
        f"- Chatr Google源表SHA-256：`{config.get('chatrGoogleSourceSha256')}`。",
        f"- Chatr日期快照SHA-256：`{expected}`。",
        f"- [本轮Chatr Google日期源表](<{Path(config['chatrGoogleSource']).resolve()}>)",
        f"- [本轮Chatr日期冻结快照](<{snapshot_path.resolve()}>)",
    ]
    if "iccid_conflict_count" in proof:
        lines += [
            f"- 系统/Google ICCID冲突：{proof['iccid_conflict_count']}行；详见最终工作簿“{proof['iccid_conflict_sheet']}”。独立冲突清单核验：{'通过' if review.get('conflict_sheet_matches') is True else '未通过'}。",
            "- 冲突行以系统ICCID为准，系统已有日期不覆盖，也不借用冲突Google日期。两日期仍空白时仅按已授权的创建时间加29天规则兜底，并明确来源；不修改Google、后台ICCID或后台日期。",
        ]
        records = proof.get("iccid_conflicts") or []
        if records:
            columns = (("row", "主表行号"), ("phone", "电话"), ("slot", "卡槽"),
                       ("system_iccid", "系统ICCID"), ("google_iccid", "Google ICCID（归一化）"),
                       ("system_activation", "系统激活日期"), ("system_cutoff", "系统截止日"),
                       ("google_activation", "Google激活日期（不采用）"), ("google_cutoff", "Google截止日（不采用）"),
                       ("final_activation", "最终激活日期"), ("final_cutoff", "最终截止日"),
                       ("activation_source", "激活来源"), ("cutoff_source", "截止日来源"))
            def escape(value):
                return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
            lines += ["", "### ICCID冲突逐行清单（系统为准）", "",
                      "| " + " | ".join(label for _, label in columns) + " |",
                      "| " + " | ".join("---" for _ in columns) + " |"]
            lines += ["| " + " | ".join(escape(record.get(key)) for key, _ in columns) + " |" for record in records]
            lines.append("")
    return lines


def confirmed_task_d_retention_summary(summary):
    """Require complete C/T post-task counts before showing a population."""
    if not isinstance(summary, dict) or summary.get("status") != "passed":
        return None
    if summary.get("countingUnit") != "unique_phone10" or not summary.get("asOf") or not summary.get("populationReceipt"):
        return None
    digest = summary.get("populationSha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
        return None
    rows = summary.get("byCarrier")
    names = {"B14-C快下保留卡", "B14-T快下保留卡"}
    if (not isinstance(rows, list) or len(rows) != 2 or any(not isinstance(row, dict) for row in rows)
            or any(not isinstance(row.get("carrierName"), str) for row in rows)
            or {row.get("carrierName") for row in rows} != names):
        return None
    for row in [summary, *rows]:
        if any(type(row.get(key)) is not int or row[key] < 0
               for key in ("newTransferredCount", "totalInRetentionCount")):
            return None
        if row["newTransferredCount"] > row["totalInRetentionCount"]:
            return None
    if any(summary[key] != sum(row[key] for row in rows)
           for key in ("newTransferredCount", "totalInRetentionCount")):
        return None
    return summary


def task_d_filtered_retention_lines(config):
    """Keep skipped query misses separate from carrier writes and notifications."""
    if "taskDFilteredUnmatchedCount" not in config:
        return []
    lines = [
        f"- 卡保留OPR筛选实际命中（含保护）：{config.get('taskDRetentionMatchedCardCount', '未知')}。",
        f"- 卡保留续费C/T保护排除：{config.get('taskDNeverProcessSourceExcludedCount', '未知')}。",
        f"- 卡保留未配置运营商跳过：{config.get('taskDUnconfiguredCarrierSkippedCount', 0)}；按用户规则排除，不计入通知成功，不修改运营商。",
        f"- 卡保留已删除运营商跳过：{config.get('taskDRetiredCarrierSkippedCount', 0)}；仅核实的B14旧ID，不提交通知、不分配新运营商，不计成功。",
        f"- 卡保留非保护可处理：{config.get('taskDRetentionActionableCardCount', '未知')}。",
        f"- Chatr / Kooda/T系运营商候选：{config.get('taskDChatrCandidateCount', '未知')} / {config.get('taskDKoodaCandidateCount', '未知')}。",
        f"- 卡保留筛选未命中跳过：{config['taskDFilteredUnmatchedCount']}；不计入候选、实际写入或通知成功。",
    ]
    if config.get("taskDRetentionCarrierTransferEnabled") is False:
        lines.insert(0, "- 卡保留通知保持原运营商：运营商候选0、修改0。")
    if config.get("taskDRetentionReceipt"):
        lines.append(f"- [卡保留全部分页与OPR筛选原始证明](<{config['taskDRetentionReceipt']}>)")
    if config.get("taskDFilteredUnmatchedCards"):
        lines.extend(["", "| 筛选未命中号码 | 处理结果 |", "| --- | --- |"])
        lines.extend(f"| {phone} | 支持的OPR筛选未返回，未提交；无法据此判定系统无号 |" for phone in config["taskDFilteredUnmatchedCards"])
        lines.append("")
    return lines


def task_d_retention_lines(config, *, task_a_only=False):
    """Render Task D's exact retained population, not its request-log size."""
    if population_statistics_removed(config, report=True):
        return []
    status = str(config.get("taskDStatus") or "")
    if (task_a_only or str(status).startswith("skipped")
            or (config.get("taskDExecuted") is not True and config.get("taskDCurrentUserAuthorized") is not True)):
        return ["- 保留区统计：任务D未执行；本轮新转入数量、执行后保留区总量均未查询，不显示历史数量。"]
    summary = (confirmed_task_d_retention_summary(config.get("taskDRetentionSummary"))
               if config.get("taskDExecuted") is True else None)
    if summary is None:
        return ["- 保留区统计：本轮新转入数量、执行后保留区总量均未知；任务或全量统计未核验通过，不以申请记录、候选或本轮报表数量代替。"]
    lines = ["", "#### 任务D完成后的保留卡区统计", "",
             f"本轮新转入保留卡区 {summary['newTransferredCount']} 个；执行后保留区共 {summary['totalInRetentionCount']} 个。", "",
             "| 保留卡区 | 本轮新转入号码 | 执行后区内全部号码 |",
             "|---|---:|---:|"]
    rows = {row["carrierName"]: row for row in summary["byCarrier"]}
    for name in ("B14-C快下保留卡", "B14-T快下保留卡"):
        row = rows[name]
        lines.append(f"| {name} | {row['newTransferredCount']} | {row['totalInRetentionCount']} |")
    lines.extend([f"| 合计 | {summary['newTransferredCount']} | {summary['totalInRetentionCount']} |", "",
                  f"- 统计时间：{summary['asOf']}；统计单位：去重手机号（unique_phone10）。",
                  "- 执行后总量来源：号码管理中两个B14保留卡运营商的后台全量分页查询及独立核验；不是卡保留申请记录（cardRetain）数量、任务D候选数或本轮报表行数。",
                  ("- 转入保留区工作已删除：本轮新转入0、运营商写入组为空；现有号码只读统计，不移动。"
                   if config.get("taskDRetentionCarrierTransferEnabled") is False else
                   "- 历史新增口径：任务D本轮从保留区以外转入、且执行后独立回读确认在上述保留卡区的去重号码；原已在任一B14保留卡区的号码和两个B14保留卡区之间互转的号码均不计新增。"),
                  "- 这是一份任务D完成时的确认快照；后续其它任务或人工改动不倒推为本轮新增。"])
    evidence = (("任务D保留区统计回执", config.get("taskDRetentionSummaryReceipt")),
                ("任务D保留区后台全量分页回执", config.get("taskDRetentionPopulationReceipt") or summary["populationReceipt"]))
    for label, path in evidence:
        if path:
            lines.extend(path_line(label, path))
    lines.append(f"- 全量分页回执SHA-256：`{summary['populationSha256']}`。")
    return lines


def task_d_notification_lines(config):
    summary = config.get("taskDNotificationSummary")
    if config.get("taskDExecuted") is not True:
        return []
    from opensms_task_d_notification_summary import summary_lines
    lines = summary_lines(summary, config.get("runId"))
    if not lines:
        return ["- 下卡通知去重统计：未确认，不把候选或更新会话数作为成功号码数。"]
    lines.extend(["", "| 任务D内最后一次确认的到期时间 | 去重号码数 |", "| --- | ---: |"])
    lines.extend(f"| {row['expireDate']} | {row['phoneCount']} |" for row in summary["lastTaskDNotificationByDate"])
    lines.append("")
    if summary.get("receipt"):
        lines.extend(path_line("任务D通知逐批校验及去重统计回执", summary["receipt"]))
    return lines


def task_f_background_lines(config):
    """Describe the immutable dispatch snapshot, never a guessed final result.

    The worker owns its independent state/report after main delivery. Reading
    that mutable state here would make the main manifest race with Task F.
    """
    status = config.get("taskFStageStatus") or "pending"
    if config.get("taskFBackgroundLaunchReceipt"):
        return ["- 任务F后台启动失败，本轮已独立回读的最终主表仍可交付；没有确认启动、查询或入库成功。",
                f"- 失败原因：{config.get('taskFBackgroundLaunchError') or '详见启动失败回执'}。",
                *path_line("任务F不可变启动失败回执", config["taskFBackgroundLaunchReceipt"]),
                "- 未生成任务F增量文件，不提供历史文件作为本次结果；本轮主表及日期冻结快照不变。"]
    lines = [
        f"- 后台派发时状态快照：{status}（非主报告生成时或当前运行状态）；本轮最终主表不等待任务F联网，实时进度和终态以独立F状态为准。",
        "- 本轮主表使用服务器优先、生成前冻结的本地日期补缺；后台新查结果不改本轮文件及其审计清单，从下次生成时补入。",
        ("- 补查本轮主表日期缺口及ko系统有-未激活表；按ICCID合并去重、隔离查询，库内有效日期不重复联网。"
         if config.get('taskFKoRefreshEnabled') else
         "- 只补查本轮Kooda日期空白或仅有已核验创建日期兜底的ICCID；每个ICCID仅去除末尾一个F/f，本地有效日期去重，隔离并发且不省略身份核验。"),
        *([f"- ko系统有-未激活异步刷新：{config.get('taskFDocumentKoRefreshRows', '未知')} 行；本轮不改表，已确认激活者下轮移出未激活表，仍无日期者显示最新记录时间。"]
          if config.get('taskFKoRefreshEnabled') else []),
        f"- 文档实际缺日期/仅创建日期兜底/合计需核对行数：{config.get('taskFDocumentMissingDateRows', '未知')}/{config.get('taskFDocumentCreationFallbackRows', '未知')}/{config.get('taskFDocumentLookupNeededRows', '未知')}；需核对行数不等于去重后的联网条数。",
        f"- 并发请求工作器：{config.get('taskFRequestedWorkers', 8)}；实际工作器、完成和新入库计数由独立任务F状态报告，未完成不得记成0缺口或已通过。",
        f"- 冻结日期快照：{config.get('reportDateCachePath') or '未配置'}；SHA-256：`{config.get('reportDateCacheSha256') or '未配置'}`。",
        "- 仅在本次确有新入库的有效激活日期时生成任务F增量Excel（三列：ICCID、截止日、激活日期）；无新增时不生成、不展示历史增量文件。",
        "- 云端上传：未执行；后台只写本地库和独立任务F交付目录，不修改本轮已交付文档。",
    ]
    for key, label in (("taskFBackgroundState", "任务F后台最新状态"),
                       ("taskFBackgroundReport", "任务F独立报告（完成后含本次增量文件）"),
                       ("taskFBackgroundJob", "任务F不可变启动回执")):
        if config.get(key):
            lines.extend(path_line(label, config[key]))
    updated = config.get("taskFNewActivationImportedCount")
    if (status in {"success", "completed", "passed"} and type(updated) is int
            and updated > 0 and config.get("taskFIncrementalWorkbook")):
        lines.append(f"- 本次新增有效激活日期：{updated} 条。")
        lines.extend(path_line("本次任务F新增日期Excel", config["taskFIncrementalWorkbook"]))
    return lines


def balance_group_names(items):
    names = []
    for item in items or []:
        name = item.get("group") if isinstance(item, dict) else item
        if name and str(name) not in names:
            names.append(str(name))
    return names


def balance_display_values(record, reply):
    total = record.get("dailyTotal")
    percent = record.get("percentRemaining")
    reply_value = reply.get("replyRatePct") if reply.get("status") == "ok" else None
    reply_date = reply.get("statDate") if reply.get("status") == "ok" else None
    if not total or percent is None:
        return reply_value, reply_date, "未知"
    status = "紧急提醒" if percent < 10 else "提醒" if percent < 20 else "正常"
    return reply_value, reply_date, status


def offline_final_metrics(first_write, first_verify, final_verify):
    pre_active = int(
        first_write.get("beforeActiveCount")
        if first_write.get("beforeActiveCount") is not None
        else first_write.get("alreadyActiveCount") or 0
    )
    first_count = int(first_verify.get("candidateCount") or 0)
    retry_count = int(final_verify.get("candidateCount") or 0)
    first_active = int(
        first_verify.get("afterActiveCount")
        if first_verify.get("afterActiveCount") is not None
        else first_verify.get("alreadyActiveCount") or 0
    )
    retry_active = int(
        final_verify.get("afterActiveCount")
        if final_verify.get("afterActiveCount") is not None
        else final_verify.get("alreadyActiveCount") or 0
    )
    final_inactive = int(
        final_verify.get("afterInactiveCount")
        if final_verify.get("afterInactiveCount") is not None
        else final_verify.get("inactiveCount") or 0
    )

    # The final receipt normally covers only the retry subset. If it covers the
    # full first-pass set, use it directly to avoid double counting.
    final_active = retry_active if retry_count >= first_count else first_active + retry_active
    return {
        "pre_active": pre_active,
        "final_active": final_active,
        "final_inactive": final_inactive,
        "recovered_after_submit": max(final_active - pre_active, 0),
    }


def offline_queue_metrics(receipt):
    responses = receipt.get("updateResponses")
    if responses is None:
        legacy = receipt.get("updateResponse")
        responses = [legacy] if legacy else []
    accepted = 0
    skipped = 0
    for response in responses:
        data = (response or {}).get("data") or {}
        accepted += int(data.get("acceptedCount") or 0)
        skipped += int(data.get("skippedCount") or 0)
    if not responses and receipt.get("actualWriteCount") is not None:
        accepted = int(receipt.get("successCount") or 0)
        skipped = max(
            int(receipt.get("actualWriteCount") or 0) - accepted,
            0,
        )
    return {"accepted": accepted, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description="从本轮回执生成固定格式的OpenSMS巡检报告。")
    parser.add_argument("config")
    parser.add_argument("output")
    parser.add_argument("--receipt")
    args = parser.parse_args()

    config = load(args.config)
    task_a_only = config.get("taskAOnly") is True and config.get("taskSelection") == "A"
    analysis = load(config["analysis"])
    balance = load(config["balance"])
    routing_manifest_path = config.get("carrierRoutingManifest")
    if not routing_manifest_path:
        routing_manifest_path = next(
            (
                item.get("path")
                for item in config.get("extraReceipts") or []
                if item.get("label") == "运营商候选清单"
            ),
            None,
        )
    routing_manifest = load(routing_manifest_path) if routing_manifest_path else {}
    routing_outputs = routing_manifest.get("outputs") or {}
    chatr_zero_output = routing_outputs.get("chatr_to_e") or {}
    a_zone_chatr_zero_candidates = config.get(
        "aZoneChatrZeroToECCandidates",
        int(chatr_zero_output.get("count") or 0),
    )
    protected_source_groups = routing_manifest.get("protectedSourceCarrierGroups") or ["A21", "A2"]
    protected_source_counts = routing_manifest.get("protectedSourceExcludedCounts") or {
        "A21": int(routing_manifest.get("protectedA21ExcludedCount") or 0),
        "A2": 0,
    }
    protected_source_summary = "、".join(
        f"{group} {int(protected_source_counts.get(group) or 0)}"
        for group in protected_source_groups
    )
    b1_low_threshold = int(
        routing_manifest.get("b1B11Use2ToB11TenDaySendThreshold")
        or config.get("b1B11Use2ToB11TenDaySendThreshold")
        or routing_manifest.get("b1B11TenDaySendThreshold")
        or config.get("b1B11TenDaySendThreshold")
        or 240
    )
    b1_high_threshold = int(
        routing_manifest.get("b1B11ToB12TenDaySendThreshold")
        or config.get("b1B11ToB12TenDaySendThreshold")
        or routing_manifest.get("b1B11TenDaySendThreshold")
        or config.get("b1B11TenDaySendThreshold")
        or 240
    )
    b1v_threshold = int(
        routing_manifest.get("b1vB11vTenDaySendThreshold")
        or config.get("b1vB11vTenDaySendThreshold")
        or routing_manifest.get("b1B11TenDaySendThreshold")
        or config.get("b1B11TenDaySendThreshold")
        or 240
    )
    rest_zone_entry_enabled = routing_manifest.get("restZoneEntryEnabled") is True
    rest_zone_b_held = int(
        routing_manifest.get("restZoneEntryBQualifiedHeldCount")
        or config.get("restZoneEntryBQualifiedHeldCount")
        or 0
    )
    rest_zone_chatr_held = int(
        routing_manifest.get("restZoneEntryChatrQualifiedHeldCount")
        or config.get("restZoneEntryChatrQualifiedHeldCount")
        or 0
    )
    b1_low_candidates = config.get(
        f"b1B11Use2Lt{b1_low_threshold}Candidates",
        config.get("b1B11Use2Lt400Candidates", 0),
    )
    b1_low_writes = config.get(
        f"b1B11Use2Lt{b1_low_threshold}Writes",
        config.get("b1B11Use2Lt400Writes", 0),
    )
    b1_high_candidates = config.get(
        f"b1B11Gte{b1_high_threshold}Candidates",
        config.get("b1B11Gte400Candidates", 0),
    )
    b1_high_writes = config.get(
        f"b1B11Gte{b1_high_threshold}Writes",
        config.get("b1B11Gte400Writes", 0),
    )
    b1v_low_candidates = config.get(f"b1vB11vUse2Lt{b1v_threshold}Candidates", 0)
    b1v_low_writes = config.get(f"b1vB11vUse2Lt{b1v_threshold}Writes", 0)
    b1v_high_candidates = config.get(f"b1vB11vGte{b1v_threshold}Candidates", 0)
    b1v_high_writes = config.get(f"b1vB11vGte{b1v_threshold}Writes", 0)
    b12v_held = int(routing_manifest.get("b12vHeldWhenPriority1NotMatched") or 0)
    card = load(config["cardReceipt"])
    offline_config = config.get("offline") or {}
    offline_disabled = config.get("offlineRecoveryEnabled") is False or offline_config.get("status") == "disabled"
    if offline_disabled:
        offline_candidate = {"status": "disabled", "count": 0, "rows": []}
        offline_first = {}
        offline_first_verify = {}
        offline_retry = {}
        offline_final = {}
    else:
        offline_candidate = load(offline_config["candidate"])
        offline_first = load(offline_config["firstWrite"])
        offline_first_verify = load(offline_config["firstVerify"])
        offline_retry = load(offline_config["retryWrite"])
        offline_final = load(offline_config["finalVerify"])
    wps = load(config["wpsReceipt"]) if config.get("wpsReceipt") else None
    room_driver = load(config["roomDriverReceipt"]) if config.get("roomDriverReceipt") else None
    overall = analysis["overall"]
    same = overall.get("sameTimeComparison") or {}
    current_send = int(overall.get("send") or 0)
    previous_send = int(same.get("send") or 0) if same.get("status") == "matched" else None
    delta = current_send - previous_send if previous_send is not None else None
    delta_pct = delta / previous_send * 100 if previous_send else None
    current_rate = overall.get("rate_pct")
    previous_rate = same.get("rate_pct") if same.get("status") == "matched" else None
    rate_delta_pp = (
        float(current_rate) - float(previous_rate)
        if current_rate is not None and previous_rate is not None
        else None
    )
    rate_delta_pct = (
        rate_delta_pp / float(previous_rate) * 100
        if rate_delta_pp is not None and previous_rate
        else None
    )
    card_report = card.get("cardSlotReport") or card
    process = card_report["process"]
    enrichment = card_report["enrichment"]
    verification = card_report["verification"]
    google_disabled = config.get("googleAssociationEnabled") is False
    chatr_google_enabled = config.get("googleChatrDateFallbackEnabled") is True
    google_dependency_line = (
        "- Google全面关联：已关闭（user_disabled）；为Chatr空白激活日期与截止日下载本轮Google来源并冻结日期快照；独立差集只复用该原始文件，不扩大主表补值范围。"
        if google_disabled and chatr_google_enabled else
        "- Google依赖：已关闭（user_disabled）；未下载、未读取Google关联表，关联检查未执行。"
        if google_disabled else f"- Google关联表SHA-256：`{config['googleSha256']}`。"
    )
    difference = process.get("google_system_missing_sheet") or {}
    ko_unactivated = process.get('ko_unactivated_sheet') or {}
    if config.get('koUnactivatedSheetEnabled') is True or ko_unactivated:
        google_dependency_line += (
            f"\n- ko系统有-未激活：任务F未激活且本轮系统库存存在的记录 {ko_unactivated.get('row_count', '未知')} 条，"
            f"F冻结未激活总数 {ko_unactivated.get('f_snapshot_row_count', '未知')} 条，"
            f"排除系统不存在 {ko_unactivated.get('excluded_not_in_system_count', '未知')} 条；"
            f"快照时间 {ko_unactivated.get('frozen_at', '未知')}；按规范ICCID取交集并去重，不限10日主表或上架状态。"
            f"卡槽来自本轮系统库存：已填 {ko_unactivated.get('current_slot_filled_count', '未知')} 条，"
            f"空白 {ko_unactivated.get('current_slot_empty_count', '未知')} 条，"
            f"同ICCID多卡槽 {ko_unactivated.get('multiple_current_slot_count', '未知')} 条（去重换行列全）。"
            "系统卡槽为空不回填历史；号码等带记录时标签的字段仍为F历史资料。"
            "未激活仅表示已查到卡但未取得激活日期，非实时上架或激活确认。"
            "排除未找到/查询失败；本轮F后续结果下次导出体现，不改主表日期或运营商。")
    if config.get("googleSystemMissingSheetEnabled") is True:
        difference_review = verification.get("google_system_missing_sheet_review") or {}
        google_dependency_line += (
            f"\n- 谷歌有-系统无：{difference.get('missing_rows', '未知')} 条Google原记录，"
            f"{difference.get('missing_unique_slots', '未知')} 个唯一卡槽；"
            f"其中有效激活日期标黄 {difference.get('activation_highlighted_rows', '未知')} 行；"
            f"独立回读{'通过' if difference_review.get('matches') is True else '未确认'}。"
            "按归一化卡槽与本轮系统全部状态记录比较，不代表后台完全无卡；不参与日期补值或号码操作。"
        )
    chatr_date_line = (
        "- Chatr：激活日期与截止日逐字段使用本轮系统有效值；对应字段空白时从本轮新鲜Google日期快照补值，系统已有值不覆盖。"
        if google_disabled and chatr_google_enabled else
        "- Chatr：激活日期与截止日均使用本轮系统值；系统激活日期空白允许保留，不从Google补值。"
        if google_disabled else
        "- Chatr：服务器已有激活日期或截止日的字段直接使用，仅对应空白字段回退到本轮Google关联表。Google缺失工作表继续保留Google原始字段。"
    )

    group_results = []
    total_actual = total_success = total_failure = 0
    for group in config["groups"]:
        candidate, metrics = group_metrics(group)
        group_results.append((group, candidate, metrics))
        total_actual += metrics["actual"]
        total_success += metrics["success"]
        total_failure += metrics["failure"]

    b_zone_zero_candidates = config.get("bZoneTodayGte50ZeroCandidates")
    if b_zone_zero_candidates is None:
        b_zone_zero_candidates = (
            config.get("b1B11TodayGt50ZeroCandidates", 0)
            + config.get("b12TodayGt50ZeroCandidates", 0)
        )
    b_zone_zero_writes = config.get("bZoneTodayGte50ZeroWrites")
    if b_zone_zero_writes is None:
        b_zone_zero_writes = (
            config.get("b1B11TodayGt50ZeroWrites", 0)
            + config.get("b12TodayGt50ZeroWrites", 0)
        )
    c2_route = (routing_manifest.get("outputs") or {}).get("c2_previous_day_reply_rate_lt5_to_a11") or {}
    c2_candidates = config.get("c2PreviousDayLt5ToA11Candidates")
    if c2_candidates is None:
        c2_candidates = c2_route.get("count", 0)
    c2_writes = config.get("c2PreviousDayLt5ToA11Writes", 0)
    c2_send_total = config.get("c2DailyStatisticsSendTotal")
    if c2_send_total is None:
        c2_send_total = routing_manifest.get("c2DailyStatisticsSendTotal")
    c2_send_alert = config.get("c2DailyStatisticsSendAlertTriggered")
    if c2_send_alert is None:
        c2_send_alert = routing_manifest.get("c2DailyStatisticsSendAlertTriggered")
    c2_balance_reply = next(
        (item for item in balance.get("replyRates") or [] if item.get("group") == "C2"),
        {},
    )
    c2_balance_row = c2_balance_reply.get("dailyRow") or {}
    if c2_send_total is None:
        c2_send_total = c2_balance_row.get("sendSize")
    c2_previous_day = routing_manifest.get("c2PreviousDay") or "未知"
    c2_statistics_date = (
        routing_manifest.get("c2DailyStatisticsDate")
        or c2_balance_reply.get("statDate")
        or c2_balance_row.get("days")
        or "未知"
    )
    c2_daily_execution = config.get("c2DailyExecution") or routing_manifest.get("c2DailyExecution") or {}
    c2_daily_status = c2_daily_execution.get("status")
    c2_daily_status_text = {
        "evaluation_completed": "本轮已执行",
        "write_completed": "本轮已执行",
        "skipped_already_executed": "今日已执行，本轮跳过",
        "skipped_after_cutoff": "已到北京时间21:00，本轮跳过",
    }.get(c2_daily_status, "未知")
    if c2_daily_execution and c2_daily_execution.get("eligible") is not True:
        c2_send_total = None
        c2_statistics_date = "未查询"
        c2_send_total_text = f"未查询（{c2_daily_status_text}）"
        c2_send_alert_status = f"未执行（{c2_daily_status_text}）"
        c2_send_alert_detail = (
            f"按日统计查询未执行（{c2_daily_status_text}）；"
            "严格低于4000提醒未执行，等于4000不提醒。"
        )
    else:
        c2_send_total_text = c2_send_total if c2_send_total is not None else "未知"
        c2_send_alert_status = "未知" if c2_send_alert is None else "已触发" if c2_send_alert else "未触发"
        c2_send_alert_detail = (
            f"按日统计→C2→查询的 {c2_statistics_date} 发送量 {c2_send_total_text}；"
            f"严格低于4000提醒 {c2_send_alert_status}，等于4000不提醒。"
        )
    task_b_executed = config.get("taskBExecuted") is True
    task_b_status = config.get("taskBStatus") or (
        "已按当前交互授权强制执行" if task_b_executed else "关闭，未执行"
    )
    task_b_manifest = load(config["taskBManifest"]) if task_b_executed and config.get("taskBManifest") else {}
    task_c_executed = config.get("taskCExecuted") is True
    task_c_status = config.get("taskCStatus") or (
        "已按当前交互授权强制执行" if task_c_executed else "关闭，未执行"
    )
    task_c_manifest = load(config["taskCManifest"]) if config.get("taskCManifest") else {}
    task_d_executed = config.get("taskDExecuted") is True
    task_d_status = config.get("taskDStatus") or (
        "已按当前交互授权执行完成" if task_d_executed else "默认关闭，未执行"
    )
    if task_d_executed and not confirmed_task_d_retention_summary(config.get("taskDRetentionSummary")):
        task_d_status = "业务回执已通过，保留区全量统计尚未确认"
    task_d_swap_receipt = load(config["taskDSwapExpiryReceipt"]) if config.get("taskDSwapExpiryReceipt") else {}
    task_d_retain_receipt = load(config["taskDRetentionExpiryReceipt"]) if config.get("taskDRetentionExpiryReceipt") else {}
    task_e_executed = config.get("taskEExecuted") is True
    task_e_status = config.get("taskEStatus") or (
        "已按当前交互授权执行完成" if task_e_executed else "默认关闭，未执行"
    )
    task_e_receipt = load(config["taskEReceipt"]) if config.get("taskEReceipt") else {}
    task_f_executed = config.get("taskFExecuted") is True
    task_f_status = config.get("taskFStatus") or (
        "默认自动目录同步完成" if task_f_executed else "默认自动目录同步未完成"
    )
    if config.get("taskFBackground"):
        task_f_status = ("后台启动失败；不阻塞本轮最终报表，非查询或入库成功回执"
                         if config.get("taskFBackgroundLaunchReceipt") else
                         f"后台派发时快照{config.get('taskFStageStatus') or 'pending'}（非当前状态）；不阻塞本轮最终报表，实时终态见独立F状态")
    if task_a_only:
        task_b_status = task_c_status = task_d_status = task_e_status = "仅任务A模式，完整跳过（task_a_only）"
        task_f_status = "未执行；仅任务A模式，完整跳过（task_a_only）"
    # Keep timings in machine receipts for diagnostics, not the user-facing report.
    timing_rows = []
    for key, workers in (config.get("programConcurrency") or {}).items():
        timing_rows.append(f"| 程序并发上限：{key} | {workers} |")

    lines = [
        "## 本轮概况",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| 执行时间 | {config['runTime']} |",
        f"| 本轮状态 | {config.get('runStatus', '完成')} |",
        f"| 执行范围 | {'本轮明确授权的未到期恢复A11；普通流转和B/C/D/E跳过' if config.get('unexpiredRestore') else '仅任务A；任务B/C/D/E/F完整跳过' if task_a_only else '普通巡检；任务F默认执行，任务B/C/D/E按本轮授权'} |",
        f"| 后台任务ID | {config['taskId']} |",
        f"| 后台生成耗时 | {config['taskDuration']} |",
        f"| 报表最新日期 | {analysis['latestDate']} |",
        f"| 号码行数 | {analysis['rows']} |",
        f"| 本轮实际写入 | {total_actual} |",
        f"| 本轮写入成功 | {total_success} |",
        f"| 本轮写入失败 | {total_failure} |",
        "| 登录恢复 | 未触发 |",
        f"| 任务B | {task_b_status} |",
        f"| 任务C | {task_c_status} |",
        f"| 任务D | {task_d_status} |",
        f"| 任务E | {task_e_status} |",
        f"| 任务F | {task_f_status} |",
        *timing_rows,
        "",
        f"- 导出流程：{config['exportWorkflow']}。",
        f"- 源文件SHA-256：`{config['sourceSha256']}`。",
        google_dependency_line,
        "",
        f"### 机房设备配置（{ROOM_CONFIG['effectiveDate']}）",
        "",
        "| 机房 | 设备范围 | 配置台数 |",
        "| --- | --- | ---: |",
        *(f"| {r['label']} | {range_text(r)} | {r['deviceCount']} |" for r in ROOM_CONFIG['rooms']),
        f"| 合计 | 互不重复的配置设备 | {ROOM_CONFIG['totalDeviceCount']} |",
        "",
        "- 配置台数不代表本轮在线或有数据设备数；未列设备为未配置，字母前缀设备不并入数字设备。",
        f"- Kooda系统来源：{config.get('systemKoodaSource') or '本轮来源未确认'}；SHA-256：`{config.get('systemKoodaSha256') or '未确认'}`。",
        "",
        "## 0回复与回复率",
        "",
        f"- 通用0回复口径：最新日期 {analysis['latestDate']}，发送窗口数 >70 且回复窗口数=0。",
        f"- 当前0回复数：{analysis['currentZero']}。",
        f"- 本轮新增0回复数：{analysis['newSincePrev']}。",
        f"- 本轮恢复0回复数：{analysis['recoveredSincePrev']}。",
        f"- 整体回复率：{overall['reply']}/{overall['send']}={pct(overall.get('rate_pct'))}。",
        f"- 今日同时间发送量：{current_send}。",
        f"- 今日同时间回复率：{pct(current_rate)}。",
    ]
    if analysis.get("scopeComparison"):
        del lines[-2:]
        lines.extend(scope_comparison_lines(analysis["scopeComparison"]))
    elif previous_send is None:
        lines.extend([
            "- 昨日同时间回复率无法取得。",
            "- 回复率百分点变化：未知。",
            "- 回复率变化比例：未知。",
            "- 昨日同时间发送量无法取得。",
            "- 增减量：未知。",
            "- 变化比例：未知。",
        ])
        nearest = config.get("nearestSameTime") or {}
        if nearest:
            lines.extend([
                f"- 最近可用昨日快照：{nearest.get('snapshotTime', '未知')}，时间偏差 {nearest.get('diffMinutes', '未知')} 分钟；超出匹配窗口，不参与增减计算。",
                f"- 最近可用快照发送量：{nearest.get('send', '未知')}；回复率：{pct(nearest.get('ratePct'))}。",
                f"- 最近可用快照回执：{nearest.get('path', '未知')}。",
            ])
    else:
        lines.extend([
            f"- 昨日同时间回复率：{pct(previous_rate)}。",
            f"- 回复率百分点变化：{signed_change(rate_delta_pp, 3, '个百分点')}。",
            f"- 回复率变化比例：{signed_change(rate_delta_pct)}。",
            f"- 昨日同时间发送量：{previous_send}。",
            f"- 增减量：{delta:+d}。",
            f"- 变化比例：{signed_change(delta_pct)}。",
            f"- 昨日快照时间：{same.get('mtime_bj')}。",
            f"- 时间偏差：{same.get('diff_minutes')}分钟。",
        ])
    lines.extend(carrier_reply_lines(balance.get("carrierReplyComparison")))
    lines.extend(["", "| 机房 | 回复 | 发送 | 回复率 | 0回复数 | 0回复率 |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for item in overall.get("room") or []:
        lines.append(f"| {item['room']} | {item['reply']} | {item['send']} | {pct(item.get('rate_pct'))} | {item['zero']} | {pct(item.get('zero_rate_pct'))} |")
    if room_driver:
        rooms = room_driver.get("rooms") or {}
        room2 = rooms.get("2号机房") or {}
        room3 = rooms.get("3号机房") or {}
        room5 = rooms.get("5号机房") or {}
        room6 = rooms.get("6号机房") or {}
        room2_overall = room2.get("overall") or {}
        room3_overall = room3.get("overall") or {}
        room5_overall = room5.get("overall") or {}
        room6_overall = room6.get("overall") or {}
        room2_rate = room2_overall.get("replyRatePct")
        room3_rate = room3_overall.get("replyRatePct")
        room5_rate = room5_overall.get("replyRatePct")
        room6_rate = room6_overall.get("replyRatePct")
        activation = room_driver.get("room2ActivationSummary") or {}
        zero_three = next(
            (item for item in room2.get("byActivationAge") or [] if item.get("bucket") == "0-3天"),
            {},
        )
        remaining_send = float(room2_overall.get("send") or 0) - float(zero_three.get("send") or 0)
        remaining_reply = float(room2_overall.get("reply") or 0) - float(zero_three.get("reply") or 0)
        remaining_rate = remaining_reply / remaining_send * 100 if remaining_send else None

        other_by_family = defaultdict(lambda: {"send": 0.0, "reply": 0.0})
        for room in (room3, room5, room6):
            for item in room.get("byOpr") or []:
                family = item.get("family")
                other_by_family[family]["send"] += float(item.get("send") or 0)
                other_by_family[family]["reply"] += float(item.get("reply") or 0)
        expected_reply = 0.0
        expected_send = 0.0
        for item in room2.get("byOpr") or []:
            family = item.get("family")
            send = float(item.get("send") or 0)
            benchmark = other_by_family.get(family) or {}
            benchmark_send = benchmark.get("send") or 0
            if benchmark_send:
                rate = benchmark.get("reply", 0) / benchmark_send
            elif item.get("replyRatePct") is not None:
                rate = float(item["replyRatePct"]) / 100
            else:
                continue
            expected_send += send
            expected_reply += send * rate
        expected_rate = expected_reply / expected_send * 100 if expected_send else None
        other_send = sum(float(r.get('send') or 0) for r in (room3_overall, room5_overall, room6_overall))
        other_reply = sum(float(r.get('reply') or 0) for r in (room3_overall, room5_overall, room6_overall))
        other_rate = other_reply / other_send * 100 if other_send else None

        lines.extend(["", "### 2号机房回复率驱动分析", ""])
        if all(value is not None for value in (room2_rate, room3_rate, room5_rate)):
            delta_vs_room3 = room2_rate - room3_rate
            delta_vs_room5 = room2_rate - room5_rate
            lines.append(
                f"- 2号机房回复率：{pct(room2_rate)}；较3号 {delta_vs_room3:+.3f} 个百分点；较5号 {delta_vs_room5:+.3f} 个百分点。"
            )
            lines.append(f"- 较6号 {room2_rate - room6_rate:+.3f} 个百分点。" if room6_rate is not None
                         else "- 6号机房缺少有效回复率，差异未知。")
        else:
            lines.append("- 部分机房缺少有效发送窗口或回复率，不做机房高低比较。")
        lines.extend([
            f"- 激活日期可识别：{activation.get('knownCards', 0)}张；近7天新卡：{activation.get('newWithin7DaysCards', 0)}张，占 {pct(activation.get('newWithin7DaysCardSharePct'), 2)}。",
            f"- 近7天新卡发送占比：{pct(activation.get('newWithin7DaysSendSharePct'), 2)}。",
            f"- 0-3天卡：发送 {zero_three.get('send', 0)}，回复率 {pct(zero_three.get('replyRatePct'))}；剔除该批后2号回复率约 {pct(remaining_rate)}。",
        ])
        for item in (room2.get("byOpr") or [])[:3]:
            lines.append(
                f"- OPR {item.get('family')}：发送占比 {pct(item.get('sendSharePct'), 2)}，回复率 {pct(item.get('replyRatePct'))}。"
            )
        if expected_rate is not None and other_rate is not None:
            lines.extend([
                f"- OPR结构反事实：按3号、5号和6号已有同OPR回复率套用2号发送结构，预计回复率约 {pct(expected_rate)}；这些机房合计为 {pct(other_rate)}，结构约解释 {expected_rate - other_rate:+.3f} 个百分点。",
            ])
            if all(value is not None for value in (room2_rate, room3_rate, room5_rate, room6_rate)) and room2_rate > max(room3_rate, room5_rate, room6_rate):
                lines.append("- 判断：2号较高主要来自同OPR内部表现更高及0-3天新卡的高回复率，OPR结构占比较高是次要正向因素。")
            else:
                lines.append("- 新卡占比及OPR构成仅作为结构说明；没有完整证据证明2号回复率高于其余所有机房。")
        else:
            lines.append("- 判断：当前只能确认2号机房新卡占比较高及OPR构成；因最新日发送量为0，无法验证它们对回复率差异的贡献。")
        lines.append(f"- 数据限制：{room_driver.get('activationCoverageNote')}")
    lines.extend(device_reply_period_lines((analysis.get("scopeComparison") or {}).get("deviceReplyPeriods")))
    zero_devices = [item for item in overall.get("device") or [] if int(item.get("zero") or 0) > 0]
    lines.extend(["", "### 设备0回复", ""])
    if zero_devices:
        for item in zero_devices:
            lines.append(f"- 设备 {item['device']}：{item['zero']}/{item['denom_gt50']}={pct(item.get('zero_rate_pct'))}。")
    else:
        lines.append("- 无设备命中0回复口径。")

    lines.extend(["", "## 运营商调整"])
    for group, candidate, metrics in group_results:
        lines.extend([
            "",
            f"### {group['title']}",
            f"- 候选数：{metrics['candidate']}。",
            f"- 后台有效数：{metrics['valid']}。",
            f"- 提交前已在目标：{metrics['already']}。",
            f"- 已离开允许来源：{metrics['left']}。",
            f"- 后台缺失：{metrics['missing']}。",
            f"- 重复记录：{metrics['duplicate']}。",
            f"- 实际写入：{metrics['actual']}。",
            f"- 成功数：{metrics['success']}。",
            f"- 失败数：{metrics['failure']}。",
            f"- 独立复核已在目标：{metrics['verify_target']}。",
            f"- 独立复核仍非目标：{metrics['verify_non_target']}。",
            f"- 复核缺失：{metrics['verify_missing']}。",
            f"- 复核重复：{metrics['verify_duplicate']}。",
            f"- 独立复核结果：{metrics['passed']}。",
            f"- 候选快照：{Path(group['candidate']).resolve()}",
        ])
        if group.get("write"):
            lines.append(f"- 写入回执：{Path(group['write']).resolve()}")
        if group.get("validated"):
            lines.append(f"- 验证候选快照：{Path(group['validated']).resolve()}")
        if group.get("verify"):
            lines.append(f"- 复核回执：{Path(group['verify']).resolve()}")
        for note in group.get("notes") or []:
            lines.append(f"- 说明：{note}。")

    lines.extend([
        "",
        "### 主报表日期：服务器优先、逐字段补缺",
        "",
        f"- 数据来源：{config.get('systemKoodaSource') or '本轮来源未确认'}；系统库存回执：{config.get('systemKoodaPath') or '未提供'}。",
        f"- 来源SHA-256：`{config.get('systemKoodaSha256') or '未确认'}`；系统库存行数/主表匹配行数/缺激活日期行数：{config.get('systemKoodaRows', '未确认')}/{config.get('systemKoodaMatchedRows', '未确认')}/{config.get('systemKoodaMissingActivationRows', '未确认')}。",
        "- 主表只保留激活日期、截止日，不再输出系统导出激活日期、系统导出截止日两列。每个字段独立优先使用本轮服务器下载的有效值，系统截止日不重算、不被备用来源覆盖。",
        "- Kooda：ICCID保留系统原始文本（包括末尾F）；服务器空白的日期字段从文档生成前冻结的任务F本地库补齐。",
        "- 最终日期兜底：系统及适用Google/冻结本地库均无激活日期和截止日时，使用源表创建时间、创建时间加29天；已有任一日期不覆盖，非空无效值不当空白。此为creation_date_fallback推算来源，不写后台或任务F权威库，保护区不变。",
        f"- 本轮创建日期兜底行数：{((card_report.get('process') or {}).get('date_resolution', {}).get('field_counts', {}).get('激活日期', {}).get('creation_date_fallback', 0))}；逐行来源由日期独立回读核验。",
        chatr_date_line,
        "- Kooda开卡日期留空；原始创建时间保留但不替代日期字段。原有新鲜源表创建时间换号判断保持不变；解析后激活日期与截止日双空的行按未换号新卡处理。",
        "",
        "### 任务F：报表空激活日期补查到本地库",
        "",
        f"- 执行状态：{task_f_status}。",
        *([
            "- 跳过原因：task_a_only；本轮只执行任务A。",
            "- 仅任务A可只读生成前冻结的本地日期补缺；联网补查、数据库导入和截止日模板导出均未执行。" if config.get("reportDateCachePath") else "- F目录、数据库、ICCID库及台账均未检查或读取；联网补查、数据库导入和截止日模板导出均未执行。",
            "- F联网缺口未查询，不能据此判断缺口为0。" if config.get("reportDateCachePath") else "- F本地库存、有效日期复用及待补查缺口：未检查；不能据此判断缺口为0。",
            "- 本轮F库及截止日模板：未生成，此模式不要求这些产物。Kooda主报表继续使用本轮系统库存，并完成缺激活日期整行标红与独立复核。",
        ] if task_a_only else [
            "- 主表系统与冻结缓存逐字段补缺后，激活日期、截止日均完整；无需任务F联网，不生成或提示任务F新增文件。",
            "- 此结论来自本轮独立回读，不使用历史增量文件代替本次更新。",
        ] if config.get("taskFSkippedReason") in {"no_missing_activation_after_fallback", "no_missing_authoritative_dates"} else task_f_background_lines(config) if config.get("taskFBackground") else [
        f"- 阶段顺序：{'本轮初始文档已先完成，随后才启动后台静默增量补查' if config.get('taskFDocumentGeneratedBeforeLookup') else '诊断模式或阶段顺序未确认'}；新查询只更新本地库，不修改本轮冻结主表，从下次生成时按字段补缺。",
        f"- 本轮文档Kooda/T系缺失激活日期整行标红：应标{config.get('taskFDocumentMissingActivationRedRows', 0)}，实际整行标红{config.get('taskFDocumentFullRedRows', 0)}；独立复核：{'通过' if config.get('taskFDocumentRedReviewPassed') else '未通过'}；颜色RGB：{config.get('taskFMissingActivationFillRgb') or 'FFC7CE'}。",
        "- 主表日期及标红按系统优先、生成前冻结缓存补缺后的最终值判断；本轮联网返回不改变本轮主表。",
        f"- 默认执行：{'是' if config.get('taskFAutomaticCatalogSync') else '本轮为显式诊断模式'}；权威附件：{config.get('taskFCatalogSource') or '未读取'}；SHA-256：`{config.get('taskFCatalogSourceSha256') or '未知'}`。",
        f"- 权威附件总数/有激活日期/尚未激活：{config.get('taskFCatalogSourceRowCount', 0)}/{config.get('taskFCatalogFoundCount', 0)}/{config.get('taskFCatalogActivationMissingCount', 0)}；附件旧空日期不再阻止本轮报表空激活行补查。",
        f"- 补查依据：{config.get('taskFDocumentPath') or '独立诊断模式'}；文档SHA-256：`{config.get('taskFDocumentSha256') or '不适用'}`；运行编号：{config.get('taskFDocumentRunId') or '不适用'}。",
        f"- 报表空激活唯一ICCID/本地已有有效日期可导出/旧空日期重新待查/缺或非法ICCID排除行：{config.get('taskFDocumentMissingActivationIccidCount', 0)}/{config.get('taskFReadyForUploadCount', 0)}/{config.get('taskFRefreshExistingMissingCount', 0)}/{config.get('taskFDocumentExcludedCount', 0)}。",
        "- 只针对当前报表激活日期或截止日仍为空、或仅有已核验创建日期兜底的Kooda号码；本地有有效日期直接复用，本地仍空、未找到或无记录时查询。仅本次新入库有效激活日期生成新增Excel，由用户人工提交云端；本程序不上传云端、不改本轮主表。",
        f"- Grab&Go联网补查：{'已触发' if config.get('taskFOnlineLookupTriggered') else '未触发（本轮空激活范围无需补查）'}；登录要求：{'需要登录' if config.get('taskFLoginRequired') else '否'}；入口：{config.get('taskFLoginUrl') or '不适用'}。",
        f"- 浏览器与传输：{config.get('taskFBrowser') or '本轮未执行'}；同源HTTP优先，后台页面JavaScript回退；HTTP请求{config.get('taskFSameOriginHttpRequests', 0)}次，JS回退{config.get('taskFBackgroundJavascriptFallbackRequests', 0)}次。",
        f"- 全部系统库存唯一ICCID：{config.get('taskFInventoryCount', 0)}；本轮范围本地复用/待补查：{config.get('taskFAlreadyQueriedCount', 0)}/{config.get('taskFPendingCount', 0)}。",
        f"- Kooda原始记录/卡存储缺失/卡存储无效：{config.get('taskFInventoryTotalRows', 0)}/{config.get('taskFMissingCardStoreCount', 0)}/{config.get('taskFInvalidCardStoreCount', 0)}；缺失或无效项单列为无法查询，不计入完成终态。",
        f"- 本轮成功导入激活日期/瞬时失败：{config.get('taskFFoundImportedCount', 0)}/{config.get('taskFTransientErrorCount', 0)}。",
        f"- 其中旧空日期或未找到记录成功补成有效日期：{config.get('taskFRefreshedMissingActivationCount', 0)}；云端自动上传：未执行。",
        f"- 重新查询仍为空、已更新查询时间及源数据的本地记录：{config.get('taskFRefreshedBlankRecordCount', 0)}。",
        f"- 旧版记录待补全/本轮已升级：{config.get('taskFSchemaUpgradePendingCount', 0)}/{config.get('taskFSchemaUpgradedCount', 0)}；当前完整结构记录：{config.get('taskFDatabaseCurrentSchemaCount', 0)}。",
        f"- 固定网页字段：{', '.join(config.get('taskFRecordedWebFields') or ['Purchase Date', 'Act. Request Date', 'Activation Date', 'SIM Number', 'SKU'])}；记录结构版本：{config.get('taskFRecordSchemaVersion', 0)}。",
        f"- 完整OpenSMS源数据：{'已随每条ICCID保存到库存回执、查询回执和SQLite' if config.get('taskFFullSourceDataIncluded') else '本轮未执行或未确认'}。",
        f"- 并发工作线程：请求{config.get('taskFRequestedWorkers', 0)}，实际{config.get('taskFEffectiveWorkers', 0)}；ASP.NET独立会话{config.get('taskFUniqueAspNetSessionCount', 0)}个；会话模式：{config.get('taskFWorkerSessionMode') or '未执行'}；自适应并发：{'是' if config.get('taskFAdaptiveConcurrency') else '否'}；共享会话强制单线程：{'是' if config.get('taskFSharedSessionForcesSerial') else '否'}；每个工作线程每条查询后至少等待{config.get('taskFPerWorkerDelayMs', 0)}毫秒。",
        f"- 独立表单登录：{'已尝试' if config.get('taskFIndependentFormLoginAttempted') else '未尝试'}；{'成功建立独立会话' if config.get('taskFIndependentFormLoginSucceeded') else '未成功或不需要'}；凭据仅从本机钥匙串读取且不写入回执。",
        f"- 并发身份保护：{config.get('taskFStrictIdentityGuard') or '未执行'}；身份不一致写终态：{'是（异常）' if config.get('taskFIdentityMismatchStoredAsTerminal') else '否'}。",
        f"- 共享会话未找到串行确认：成功{config.get('taskFSharedSessionSerialConfirmationCount', 0)}，失败{config.get('taskFSharedSessionSerialConfirmationFailureCount', 0)}；未确认的not_found不得写终态。",
        f"- 批量进度：{'仅查询本轮报表激活日期为空且本地无有效日期的ICCID' if config.get('taskFReportBackfillMode') else '独立诊断模式，沿用其待查清单'}；每{config.get('taskFProgressIntervalSeconds', 0)}秒刷新一次；进度回执：{config.get('taskFProgress') or '未生成'}。",
        f"- 数据库分批断点：{'已启用' if config.get('taskFDatabaseCheckpointAfterEveryBatch') else '未启用或本轮未执行'}；批大小{config.get('taskFBatchSize', 0)}，计划/完成批次{config.get('taskFBatchCount', 0)}/{config.get('taskFCompletedBatchCount', 0)}。",
        f"- 持久数据库：{config.get('taskFDatabase') or '未建立'}。",
        f"- 本轮数据库快照：{config.get('taskFDatabaseSnapshot') or '未生成'}。",
        f"- 本轮可保存ICCID库：{config.get('taskFIccidLibrary') or '未生成'}。",
        f"- 固定最新ICCID库：{config.get('taskFLatestIccidLibrary') or '未生成'}；总记录/有激活日期/尚未激活/未找到：{config.get('taskFIccidLibraryRecordCount', 0)}/{config.get('taskFIccidLibraryFoundCount', 0)}/{config.get('taskFIccidLibraryActivationMissingCount', 0)}/{config.get('taskFIccidLibraryNotFoundCount', 0)}。",
        "- 独立库日期口径：激活日计第1天，截止日=激活日+29个自然日；主报表已有系统截止日则直接使用系统值，仅系统空白时使用冻结缓存截止日。",
        ]),
        "",
        f"### 任务D：{TASK_D_LABEL}",
        "- 备注名称已更新，仍包含换号区和卡保留两个通知分支；卡保留申请日志和通知规则保持。",
        f"- 执行状态：{task_d_status}。",
        f"- 换号区连续两日0回复指标候选：{config.get('taskDSwapMetricCandidateCount', 0)}。",
        f"- 任务D指标空运营商跳过：{config.get('taskDSwapMetricUnconfiguredCarrierSkippedCount', 0)} 行；不分配、不通知、不计成功。",
        f"- 换号区后台实时有效并写入当天标记：{config.get('taskDSwapNotificationCount', 0)}。",
        f"- 换号区目标到期日：{config.get('taskDSwapExpireDate') or '未知'}。",
        f"- 换号区标记业务输入/命中/更新会话：{task_d_swap_receipt.get('inputCount', 0)}/{task_d_swap_receipt.get('matchedCardCount', 0)}/{task_d_swap_receipt.get('updatedChatCount', 0)}。",
        f"- 换号区标记事务回执校验：{'通过' if task_d_swap_receipt.get('transactionalVerificationPassed') else '未通过或未执行'}。",
        f"- 卡保留申请来源全部分页去重卡号（非保留区存量）：{config.get('taskDRetentionUniqueCardCount', 0)}。",
        *task_d_filtered_retention_lines(config),
        f"- 卡保留运营商候选/实际写入：{config.get('taskDCarrierCandidateCount', 0)}/{config.get('taskDCarrierActualWrite', 0)}。",
        f"- 卡保留目标到期日：{config.get('taskDRetentionExpireDate') or '未知'}。",
        f"- 卡保留标记业务输入/命中/更新会话：{task_d_retain_receipt.get('inputCount', 0)}/{task_d_retain_receipt.get('matchedCardCount', 0)}/{task_d_retain_receipt.get('updatedChatCount', 0)}。",
        f"- 卡保留标记事务回执校验：{'通过' if task_d_retain_receipt.get('transactionalVerificationPassed') else '未通过或未执行'}。",
        "- 到期日独立查询：后台未提供；卡保留记录仅作为保留申请来源，不作为到期标记写入回读。",
        *task_d_notification_lines(config),
        *task_d_retention_lines(config, task_a_only=task_a_only),
        *task_d_phone_detail_lines({**config, "taskAOnly": task_a_only}),
        "",
        f"### 任务E：{TASK_E_LABEL}",
        "- 仅更新备注；原有6天/快过期/已过期筛选、文档截止日分组和永久去重规则不变。",
        f"- 执行状态：{task_e_status}。",
        f"- 文档状态命中原始行：{config.get('taskEEligibleWorkbookRowCount', 0)}。",
        f"- 快过期续费C/T保护排除：{config.get('taskENeverProcessSourceExcludedCount', 0)}。",
        f"- 未配置运营商跳过：{config.get('taskEUnconfiguredCarrierSkippedCount', 0)}；不通知、不计成功。",
        f"- 永久台账已提交排除：{config.get('taskEAlreadySubmittedExcludedCount', 0)}。",
        f"- 同一号码同截止日重复折叠：{config.get('taskEDuplicateSameDeadlineCollapsedCount', 0)}。",
        f"- 去重后候选/已处理/成功写入/未写入：{config.get('taskECandidateCount', 0)}/{config.get('taskEProcessedCount', 0)}/{config.get('taskESubmittedCount', 0)}/{config.get('taskENotWrittenCount', 0)}。",
        f"- 后台已匹配但无需变化：{config.get('taskEMatchedNoChangeCount', 0)}。",
        f"- 截止日分组数：{config.get('taskEDateGroupCount', 0)}；日期：{', '.join(config.get('taskEExpireDates') or []) or '无'}。",
        f"- 事务输入/命中/更新会话：{task_e_receipt.get('inputCount', 0)}/{task_e_receipt.get('matchedCardCount', 0)}/{task_e_receipt.get('updatedChatCount', 0)}。",
        f"- 事务回执校验：{'通过' if task_e_receipt.get('transactionalVerificationPassed') else '未通过或未执行'}；永久去重台账校验：{'通过' if task_e_receipt.get('ledgerVerificationPassed') else '未通过或未执行'}。",
        f"- 不确定批次恢复候选/重试：{config.get('taskERecoveryCandidateCount', 0)}/{config.get('taskERecoveryRetriedCount', 0)}；其中接口幂等无变化：{config.get('taskEIdempotentRecoveryNoChangeCount', 0)}。",
        f"- 不确定批次恢复证明：{config.get('taskERecoveryProof') or '未使用'}。",
        f"- 永久去重键：{config.get('taskELedgerDedupeKey') or '未执行'}；台账：{config.get('taskELedger') or '未读取'}。",
        f"- 本轮台账快照：{config.get('taskELedgerSnapshot') or '未生成'}。",
        f"- 卡保留记录分页/总记录/去重号码：{config.get('taskERetentionPageCount', 0)}/{config.get('taskERetentionTotalRecordCount', 0)}/{config.get('taskERetentionUniqueCardCount', 0)}。",
        f"- 卡保留号码在文档找到/截止日有效：{config.get('taskERetentionWorkbookPhoneFoundCount', 0)}/{config.get('taskERetentionWorkbookDeadlineKnownCount', 0)}；文档缺号/缺截止日：{config.get('taskERetentionWorkbookMissingPhoneCount', 0)}/{config.get('taskERetentionWorkbookMissingDeadlineCount', 0)}。",
        f"- 卡保留记录新到期日无效：{config.get('taskERetentionInvalidNotificationDateRecordCount', 0)}；新到期日与文档截止日完全相同的提醒号码/记录：{config.get('taskERetentionDeadlineMatchPhoneCount', 0)}/{config.get('taskERetentionDeadlineMatchRecordCount', 0)}。",
        f"- 卡保留分页复用：{'复用本轮任务D只读回执' if config.get('taskERetentionReceiptReusedFromTaskD') else '任务E独立只读获取' if task_e_executed else '未执行'}。",
        "- 到期日独立查询：后台未提供；任务E以同步事务回执和持久化手机号台账作为完成证据。",
    ])
    task_e_not_written = config.get("taskENotWrittenCards") or []
    if task_e_not_written:
        if config.get("taskENotWrittenSheetRequired") is True:
            lines.append("- 最终合并表已单列“任务E未写入”sheet，列出本轮明确后台未命中的手机号、截止日和原因。")
        lines.extend([
            "",
            "#### 任务E未写入号码",
            "",
            "请人工排查以下后台未命中号码；未写入，不计为成功。复制区每行一个号码：",
            "",
            "```text",
            *[str(row.get('phone') or '') for row in task_e_not_written],
            "```",
            "",
            "| 电话 | 截止日 | 原因 |",
            "| --- | --- | --- |",
        ])
        for row in task_e_not_written:
            lines.append(
                f"| {row.get('phone') or ''} | {row.get('expireDate') or ''} | {row.get('reason') or 'backend_not_found'} |"
            )
    task_e_no_change = config.get("taskEMatchedNoChangeCards") or []
    if task_e_no_change:
        lines.extend([
            "",
            "#### 任务E已匹配但无需变化号码",
            "",
            "| 电话 | 截止日 | 状态 |",
            "| --- | --- | --- |",
        ])
        for row in task_e_no_change:
            lines.append(
                f"| {row.get('phone') or ''} | {row.get('expireDate') or ''} | {row.get('reason') or 'backend_matched_no_change'} |"
            )
    retention_deadline_matches = config.get("taskERetentionDeadlineMatches") or []
    if retention_deadline_matches:
        lines.extend([
            "",
            "#### 任务E卡保留日期一致提醒",
            "",
            "| 电话 | 下卡通知时间（新到期日） | 文档截止日 | 原到期日 | 申请时间 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for row in retention_deadline_matches:
            lines.append(
                f"| {row.get('电话') or ''} | {row.get('下卡通知时间') or ''} | "
                f"{row.get('截止日') or ''} | {row.get('原到期日') or ''} | {row.get('申请时间') or ''} |"
            )

    from opensms_task_g import report_lines as task_g_lines
    if config.get("taskG"):
        lines.extend(["", "### 任务G：周末休息与次日恢复", "", *task_g_lines(config["taskG"])])
    first_queue = offline_queue_metrics(offline_first)
    retry_queue = offline_queue_metrics(offline_retry)
    offline_metrics = offline_final_metrics(offline_first, offline_first_verify, offline_final)
    pre_active = offline_metrics["pre_active"]
    final_active = offline_metrics["final_active"]
    final_inactive = offline_metrics["final_inactive"]
    recovered_after_submit = offline_metrics["recovered_after_submit"]
    lines.extend(["", "## 下架恢复", ""])
    probe = config.get('expiredCardProbe')
    if probe and probe.get('background') is True:
        lines.extend([
            '- 当前规则：仅过期下架卡T/C全区，不按截止日筛选。已上架每轮A一次；同一卡身份未上架只尝试一次，未恢复不重复提交。',
            '- 后台独立作业在本轮最终报表与交付清单完成后取得网站锁再运行；不阻塞本轮输出、不改写已交付文档。',
            f"- 不可变派发快照：{probe.get('status', '未知')}，不是当前测试状态或成功证明。",
            f"- 独立状态：{probe.get('backgroundState', '未生成')}。",
            f"- 独立结果回执：{probe.get('receipt', '未生成')}；结果需要独立回读及审计，不以后台接收代替成功。",
            f"- 派发错误：{probe.get('error') or '无'}。",
        ])
    elif probe:
        lines.extend([
            "- 新授权范围：当前过期下架卡C/T，有效截止日严格早于平台今天（昨天包含）。旧泛化恢复仍关闭。",
            f"- 状态：{probe.get('status', '未确认')}；平台日期：{probe.get('serverDate', '未知')}。",
            f"- 全范围：{probe.get('populationCount', '未知')}；候选：{probe.get('candidateCount', '未知')}；缺有效日期：{probe.get('unknownDateCount', '未知')}；异常/其他状态：{probe.get('ineligibleStatusCount', '未知')}；号码/身份格式异常：{probe.get('invalidIdentityCount', '未知')}。",
            f"- 下架提交：{probe.get('offlineSubmittedCount', '未知')}；恢复提交：{probe.get('recoverySubmittedCount', '未知')}；后台接受：{probe.get('acceptedCount', '未知')}。",
            f"- 独立回读已上架：{probe.get('onlineVerifiedCount', '未知')}；异步待确认：{probe.get('pendingCount', '未知')}；已测跳过：{probe.get('alreadyTestedSkippedCount', '未知')}；不明意图阻止重放：{probe.get('blockedCount', '未知')}。",
            "- 未上架只提交一次，已上架才允许下轮继续测试；异步接收不等于存活确认，未确认不重试。",
            f"- 独立回执：{probe.get('receipt', '未生成')}；错误：{probe.get('error') or '无'}。",
        ])
    elif offline_disabled:
        lines.extend([
            "- 功能状态：已取消。",
            "- 候选数：0。",
            "- 后台有效数：0。",
            "- 实际写入：0。",
            "- 成功数：0。",
            "- 失败数：0。",
            "- 重试数：0。",
            "- 独立复核结果：未执行。",
        ])
    else:
        lines.extend([
            f"- 候选数：{count_candidates(offline_candidate)}。",
            f"- 提交前已上架：{pre_active}。",
            f"- 首次实际提交：{offline_first.get('actualWriteCount', offline_first.get('inactiveCount', 0))}。",
            f"- 首次队列接受数：{first_queue['accepted']}。",
            f"- 首次独立复核已上架：{offline_first_verify.get('afterActiveCount', offline_first_verify.get('alreadyActiveCount', 0))}。",
            f"- 首次独立复核仍下架：{offline_first_verify.get('afterInactiveCount', offline_first_verify.get('inactiveCount', 0))}。",
            f"- 重试实际提交：{offline_retry.get('actualWriteCount', offline_retry.get('inactiveCount', 0))}。",
            f"- 重试队列接受数：{retry_queue['accepted']}。",
            f"- 重试跳过数：{retry_queue['skipped']}。",
            f"- 最终成功数：{recovered_after_submit}。",
            f"- 最终失败数：{final_inactive}。",
            f"- 最终独立复核已上架：{final_active}。",
            f"- 最终独立复核仍下架：{final_inactive}。",
            f"- 首次提交回执：{Path(offline_config['firstWrite']).resolve()}",
            f"- 首次复核回执：{Path(offline_config['firstVerify']).resolve()}",
            f"- 重试回执：{Path(offline_config['retryWrite']).resolve()}",
            f"- 最终复核回执：{Path(offline_config['finalVerify']).resolve()}",
        ])
    lines.extend([
        "",
        "## 余量与提醒",
        "",
        "| 分组 | 当日剩余量 | 当日总量 | 剩余比例 | 回复率 | 回复率统计日期 | 状态 |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ])
    records = {item.get("group"): item for item in balance.get("records") or []}
    reply_rates = {item.get("group"): item for item in balance.get("replyRates") or []}
    for group_name in ["A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V"]:
        record = records.get(group_name)
        reply = reply_rates.get(group_name) or {}
        if not record:
            lines.append(f"| {group_name} | 未知 | 未知 | 未知 | {pct(reply.get('replyRatePct'), 2)} | {reply.get('statDate') or '未知'} | 未知：运营商缺失 |")
            continue
        remaining = record.get("dailyRemaining")
        total = record.get("dailyTotal")
        percent = record.get("percentRemaining")
        reply_value, reply_date, status = balance_display_values(record, reply)
        lines.append(f"| {group_name} | {remaining} | {total} | {pct(percent, 2)} | {pct(reply_value, 2)} | {reply_date or '未知'} | {status} |")
    auto_binding = balance.get("autoBinding") or {}
    binding_readback = auto_binding.get("independentReadback") or {}
    lines.extend([
        f"- 低于20%提醒：{'、'.join(balance_group_names(balance.get('low20'))) or '无'}。",
        f"- 低于10%紧急提醒：{'、'.join(balance_group_names(balance.get('low10'))) or '无'}。",
        f"- C2最新日发送窗口数合计：{c2_send_total_text}。",
        f"- C2最新日发送窗口数低于4000提醒：{c2_send_alert_status}。",
        f"- A3客户绑定触发：{'是' if auto_binding.get('attempted') else '否'}。",
        f"- A3来源客户数：{auto_binding.get('sourceBindingCount', 0)}。",
        f"- A1/A11新增绑定数：{len(auto_binding.get('created') or [])}。",
        f"- A1/A11提交前已绑定数：{len(auto_binding.get('alreadyBound') or [])}。",
        f"- A1/A11绑定失败数：{len(auto_binding.get('failures') or [])}。",
        f"- A1/A11独立复核已绑定：{binding_readback.get('confirmedCount', 0)}。",
        f"- A1/A11独立复核缺失：{binding_readback.get('missingCount', 0)}。",
        "- 自动加量：关闭，本轮未修改余量或配额。",
        (
            f"- C2与A3/A1的1.2倍比较：触发 {'、'.join((balance.get('c2ReplyRateRule') or {}).get('triggeredTargets') or [])}。"
            if (balance.get('c2ReplyRateRule') or {}).get('triggeredTargets')
            else "- C2与A3/A1的1.2倍比较：本轮同日首次回复率未触发。"
        ),
        "",
        "## 文件与回执",
        "",
        f"- 卡槽结果表：{card_report['result']}；完整复核失败项 {len(card_report.get('failed_checks') or [])}。",
        (f"- 卡槽主表行数：{process['main_rows']}；Google卡槽匹配未执行。" if google_disabled else
         f"- 卡槽主表行数：{process['main_rows']}；匹配 {process['matched_rows']}；未匹配卡槽 {process['unmatched_slot_count']}。"),
        f"- 系统无-谷歌表有：{process.get('unused_port_count', 0)}；设备 {process.get('unused_port_device_count', 0)} 台；系统中无 {process.get('system_missing_port_count', 0)}；注册失败 {process.get('registration_failed_port_count', 0)}；其中“谷歌表存在” {process.get('system_missing_google_present_count', 0)}；保留原端口检查范围，新增标记仅用于系统无记录且Google同卡槽存在的行。",
        *device_daily_report_lines(card_report),
        *card_retention_report_lines(card_report),
        *([f"- 使用天数：{process['creation_usage_days']['rule']}；可计算{process['creation_usage_days']['known_rows']}行，缺失/非法{process['creation_usage_days']['unknown_rows']}行，未来创建{process['creation_usage_days']['future_rows']}行；独立重算{'通过' if card_report.get('checks', {}).get('使用天数独立重算') else '未通过'}。"] if process.get('creation_usage_days') else []),
        *(["- 系统无-谷歌表有设备范围：仅本轮系统源主表出现的数字设备，每台1至64端口（system_source_only）；未覆盖仅在Google登记、系统主表完全没有的设备。"] if google_disabled else []),
        f"- 卡槽别名复核：{'通过' if process.get('slot_alias_204_62_matches') else '失败'}；204_62 与 204_62.01 按同一卡槽处理。",
        ("- 字段来源：日期逐字段以本轮系统有效值为准；Kooda空日期仅从生成前冻结F库补，Chatr空白激活日期与截止日从本轮新鲜Google日期快照补；开卡日期留空。" if google_disabled and chatr_google_enabled else
         "- 字段来源：日期逐字段以本轮系统有效值为准；Kooda空日期从生成前冻结F库补，Chatr空激活日期允许保留，截止日使用系统值；开卡日期留空。" if google_disabled else
         "- 字段来源：日期逐字段以本轮系统有效值为准；Kooda空日期从生成前冻结F库补，Chatr空日期从本轮Google表补。Kooda开卡日期留空，Google缺失工作表保留原始字段。"),
        *chatr_google_date_report_lines(config, card_report),
        *(["- Google差异、未匹配卡槽及电信商/OPR对照表和对应列：未生成；Google关联检查未执行。"] if google_disabled else [
            f"- 電信商/價格与OPR不一致预警：{process.get('carrier_opr_mismatch_rows', '未知')}行；详见“电信商_OPR预警”工作表。"]),
        (
            f"- 激活后天数复核：{'通过' if card_report.get('checks', {}).get('激活后天数') else '失败'}；"
            f"可计算 {process.get('activation_age_known_rows', 0)}，"
            f"激活日期缺失 {process.get('activation_age_unknown_rows', 0)}，"
            f"未来激活日期 {process.get('activation_age_future_rows', 0)}。"
        ),
        f"- 3日/4日发送窗口数与回复率复核：{'通过' if card_report['checks']['3日4日回复率明细'] else '失败'}。",
        f"- 持续2天降低：{enrichment['decreasing_rate_counts'].get('持续2天降低', 0)}。",
        f"- 持续3天降低：{enrichment['decreasing_rate_counts'].get('持续3天降低', 0)}。",
        f"- 持续4天降低：{enrichment['decreasing_rate_counts'].get('持续4天降低', 0)}。",
        "- 已按最新要求取消工作表：今日总回复量为0、今日昨天总回复量均为0、快过期_今天明天、已过期_未进过期下架卡。",
        f"- 已激活未上架：{process['activated_unlisted_rows']}；涉及机器 {process['activated_unlisted_machine_count']}。",
        *(["- 已激活未上架范围：仅统计系统或本轮Chatr日期快照具有有效激活日期的行；两者都空时保留空白，不推断已激活、不计作任务F缺口。"] if google_disabled and chatr_google_enabled else
          ["- 已激活未上架范围：仅统计已有有效激活日期依据的行；Chatr系统空激活日期保持空白，激活后天数可空，不推断已激活、不计作缺激活故障或任务F缺口。"] if google_disabled else []),
        f"- 超过5张未上架机器：{process['over_five_unlisted_machine_count']}。",
        f"- 公式错误扫描：{verification['formula_error_matches']}处。",
        f"- 工作簿完整回执：{Path(config['cardReceipt']).resolve()}",
        f"- 余量与按日回复率回执：{Path(config['balance']).resolve()}",
    ])
    if config.get("parallelAuditReceipt"):
        lines.append(f"- 程序并行检查回执：{Path(config['parallelAuditReceipt']).resolve()}")
    if config.get("wpsReceipt"):
        lines.append(f"- WPS检查回执：{Path(config['wpsReceipt']).resolve()}")
    else:
        lines.append("- WPS检查：未打开；本轮由程序直接检查Excel和回执。")
    if config.get("roomDriverReceipt"):
        lines.append(f"- 机房回复率驱动分析回执：{Path(config['roomDriverReceipt']).resolve()}")
    for receipt in config.get("extraReceipts") or []:
        if google_disabled and any(term in str(receipt.get("label", "")).lower() + str(receipt.get("path", "")).lower()
                                   for term in ("google", "谷歌", "association")):
            allowed = {str(Path(config[key]).resolve()) for key in (
                "chatrGoogleSource", "chatrGoogleDownloadReceipt", "chatrGoogleDates") if config.get(key)} if chatr_google_enabled else set()
            if str(Path(receipt.get("path") or "").resolve()) not in allowed:
                lines.append("- 异常：关闭Google后配置仍引用范围外Google产物；未读取、未作为交付链接，须由审计拒绝。")
                continue
        lines.append(f"- [{receipt['label']}](<{Path(receipt['path']).resolve()}>)")
    lines.append("")
    lines.extend(path_line("打开本轮源文件", config["sourceArchive"]))
    lines.append("")
    lines.extend(path_line("打开卡槽关联结果表", config["cardResult"]))
    lines.append("")
    lines.extend(path_line("打开完整Markdown报告", args.output))
    lines.extend([
        "",
        "## 异常",
        "",
        *final_expiry_carrier_lines(config),
        "",
        *unexpired_restore_lines(config),
        "",
        f"- 未唯一解析运营商：{'、'.join(config.get('missingCarrierGroups') or []) or '无'}。",
        (
            "- A区Kooda/Koodo/Telus1规则（A1的Kini例外另列）：当前后台唯一属于A1、A11、A12或A3且OPR属于Kooda/Koodo/Telus1时优先转B12；"
            "不设发送、回复或回复率门槛；"
            f"本轮候选 {config.get('aZoneKoodaToB12Candidates', 0)}，"
            f"实际写入 {config.get('aZoneKoodaToB12Writes', 0)}。"
        ),
        (
            "- A1＋Kini独立规则：仅最新日发送窗口数>=50且回复率严格<4%时转B12；"
            "发送<50、回复率>=4%或统计无效均保持A1，停止后续普通A区分流；Chatr规则不变。"
            f"本轮候选 {config.get('a1KiniToB12Candidates', 0)}，"
            f"实际写入 {config.get('a1KiniToB12Writes', 0)}。"
        ),
        (
            f"- A21/A2运营商保护：本轮分别排除 {protected_source_summary}；"
            "两组来源候选 0，实际写入 0。"
        ),
        (
            "- 快过期续费C/T保护：普通流程及任务C/D/E仍不得写入或通知；"
            "任务B仅按当前全运营商到期规则作例外，逐轮授权和日期核验不变。"
        ),
        (
            f"- B区新规则：当前后台唯一属于B1或B11、OPR属于Kooda/T系、koo是否2天显示2天且10日发送窗口数<{b1_low_threshold}时优先转B11；"
            f"本轮候选 {b1_low_candidates}，"
            f"实际写入 {b1_low_writes}。"
        ),
        (
            f"- B区新规则：未命中koo是否2天=2天且10日发送<{b1_low_threshold}转B11规则时，当前后台唯一属于B1或B11、10日发送窗口数>={b1_high_threshold}时转B12；"
            f"本轮候选 {b1_high_candidates}，"
            f"实际写入 {b1_high_writes}。"
        ),
        (
            f"- B区V组优先级2：未命中0回复休息区规则，B1V/B11V逻辑koo是否2天字段精确等于2天且10日发送<{b1v_threshold}时转B11V；"
            f"本轮候选 {b1v_low_candidates}，实际写入 {b1v_low_writes}。"
        ),
        (
            f"- B区V组优先级3：未命中前两条，B1V/B11V十日发送>={b1v_threshold}时转B12V；"
            f"本轮候选 {b1v_high_candidates}，实际写入 {b1v_high_writes}。"
        ),
        f"- B区V组保持规则：B12V未命中最新日发送>=50且0回复时保持原组；本轮保持 {b12v_held}。",
        (
            f"- C2低回复率规则：当前后台唯一属于C2，报表前一天 {c2_previous_day} 发送窗口数>0且回复率严格低于5%时转A11；"
            "等于5%不命中；"
            f"本轮候选 {c2_candidates}，实际写入 {c2_writes}。"
        ),
        f"- C2每日执行门禁：北京时间21:00前每日最多执行一次；本轮状态 {c2_daily_status_text}。",
        f"- C2发送量提醒：{c2_send_alert_detail}",
        "- B13已废除：本轮B13目标候选0、实际写入0；后续巡检禁止生成任何以B13为目标的候选或写入；B12低回复号码保留在B12。",
        (
            "- A区4%高回复规则：当前后台唯一属于A11或A12，最新日发送窗口数>=50且回复率>=4%时转A1；"
            "等于4%必须命中；"
            f"本轮候选 {config.get('aZoneGte4ToA1Candidates', config.get('a12TodayGte4ToA1Candidates', 0))}，"
            f"实际写入 {config.get('aZoneGte4ToA1Writes', config.get('a12TodayGte4ToA1Writes', 0))}。"
        ),
        (
            "- A区4%低回复规则：当前后台唯一属于A1或A11，最新日发送窗口数>=50且回复率严格低于4%时转A12；"
            "非Chatr的0回复包含在低于4%内；"
            + ("Chatr符合零回复条件时优先转E-C休息区；" if rest_zone_entry_enabled
               else "Chatr零回复当前因休息区转入关闭而保持原运营商；")
            + "等于4%不命中低回复组。"
        ),
        (
            "- A区Chatr零回复休息区规则：转入功能当前暂停；"
            f"本轮命中后保持 {rest_zone_chatr_held}，转入候选 {a_zone_chatr_zero_candidates}，"
            f"实际写入 {config.get('aZoneChatrZeroToECWrites', 0)}。"
            if not rest_zone_entry_enabled
            else f"- A区Chatr零回复休息区规则：已启用；转入候选 {a_zone_chatr_zero_candidates}，实际写入 {config.get('aZoneChatrZeroToECWrites', 0)}。"
        ),
        (
            "- B区0回复休息区规则：转入功能当前暂停；"
            f"本轮命中后保持 {rest_zone_b_held}，转入候选 {b_zone_zero_candidates}，实际写入 {b_zone_zero_writes}。"
            if not rest_zone_entry_enabled
            else f"- B区0回复休息区规则：已启用；转入候选 {b_zone_zero_candidates}，实际写入 {b_zone_zero_writes}。"
        ),
        "- B区旧规则已废止：不再执行360阈值、B1/B11零回复先转B12、阈值严格大于50或E恢复排除B1/B11/B12。",
        (
            "- 过期区存活测试按最新授权执行，候选与结果见独立回执；旧泛化恢复关闭。"
            if probe else "- 下架恢复功能已取消：候选0、写入0、重试0、回读0。"
            if offline_disabled
            else "- 下架恢复候选为0，未提交或重试。"
            if count_candidates(offline_candidate) == 0
            else f"- 下架恢复最终仍下架：{final_inactive}；失败项最多重试一次后停止。"
        ),
        f"- A3客户绑定异常：{auto_binding.get('error') or '无'}。",
        (
            f"- WPS本轮源表打开：{'是' if wps.get('sourceOpenInWps') else '否'}；"
            f"结果表打开：{'是' if wps.get('resultOpenInWps') else '否'}；"
            "未退出WPS，未触碰无关文档。"
            if wps
            else "- WPS未打开；Excel完整性、字段、公式和业务回执由程序检查。"
        ),
        (
            "- 任务B已按当前交互明确授权执行；全部来源运营商纳入到期分流，"
            "包括快过期续费C/T及三方隔离，不再按来源区排除。"
            f"未配置运营商跳过 {task_b_manifest.get('unconfiguredCarrierSkippedCount', 0)} 行，不分配。"
            f"逐运营商覆盖核验：{'通过' if task_b_manifest.get('sourceCarrierCoveragePassed') is True else '未通过'}；"
            f"覆盖明细见[任务B清单]({config.get('taskBManifest')})。"
            if task_b_executed and task_b_manifest.get('sourceCarrierScope') == 'all_carriers_v1'
            else (
            "- 任务B已按当前交互明确授权执行，且生成候选时OPENSMS_FORCE_TASK_B=1；"
            "过期/快过期写入与独立复核结果详见运营商调整分组。"
            f"B14保留卡保护排除 {task_b_manifest.get('retainedSourceExcludedCount', '未知')} 个"
            f"（已过期 {task_b_manifest.get('expiredRetainedSourceExcludedCount', '未知')}，"
            f"明日到期 {task_b_manifest.get('tomorrowRetainedSourceExcludedCount', '未知')}）；"
            f"原续费区保护排除 {task_b_manifest.get('neverProcessSourceExcludedCount', '未知')} 个；"
            "排除明细见任务B清单。"
            if task_b_executed
            else "- 任务B默认关闭；本轮无当前真实用户授权，未执行过期/快过期运营商写入。"
            )
        ),
        (
            "- 任务C已按当前交互明确授权执行，且生成与提交候选时OPENSMS_FORCE_TASK_C=1；"
            "Kooda/T系4日回复率严格低于5%转换号区T、未换号Chatr 4日回复率严格低于4%转换号区C的写入与独立复核结果详见运营商调整分组；"
            "两个分支均要求10日发送窗口数严格>360；"
            f"未配置运营商跳过 {task_c_manifest.get('unconfiguredCarrierSkippedCount', 0)} 行，不分配；"
            f"10日发送门禁排除 {task_c_manifest.get('tenDaySendGateExcludedCount', '未知')} 行"
            f"（不大于360：{(task_c_manifest.get('tenDaySendGateExcludedCounts') or {}).get('not_above_360', '未知')}；"
            f"缺失或无效：{(task_c_manifest.get('tenDaySendGateExcludedCounts') or {}).get('missing_or_invalid', '未知')}）。"
            f"任务B过期/明日到期优先级锁定排除 {task_c_manifest.get('taskBPriorityExcludedCount', 0)} 个。"
            + (f"服务器当天 {(task_c_manifest.get('recentDailyReplyRateGuard') or {}).get('serverDate')} / "
               f"前一天 {(task_c_manifest.get('recentDailyReplyRateGuard') or {}).get('previousDate')} "
               f"任一天回复率严格大于T系5%或Chatr 4%则不转换号区；"
               f"高回复排除 {(task_c_manifest.get('recentDailyReplyRateExcludedCounts') or {}).get('above_threshold', '未知')} 行，"
               f"单日回复率缺失/无效排除 {(task_c_manifest.get('recentDailyReplyRateExcludedCounts') or {}).get('missing_or_invalid', '未知')} 行。"
               if task_c_manifest.get('recentDailyReplyRateGuard') else "")
            if task_c_executed
            else "- 任务C默认关闭，本轮未生成候选、未修改号码。"
        ),
        (
            ("- 任务D已按当前交互明确授权执行，且OPENSMS_FORCE_TASK_D=1；换号区和卡保留到期标记事务回执均通过。"
             + ("转入保留区工作已删除，未修改运营商，运营商写后回读不适用。"
                if config.get("taskDRetentionCarrierTransferEnabled") is False
                else "历史卡保留运营商写入已独立回读。"))
            if task_d_executed and task_d_swap_receipt.get("transactionalVerificationPassed") and task_d_retain_receipt.get("transactionalVerificationPassed") and confirmed_task_d_retention_summary(config.get("taskDRetentionSummary"))
            else "- 任务D默认关闭；本轮无当前真实用户授权，未生成候选、未读取卡保留、未查询任务D号码、未修改运营商、未写到期标记。"
            if config.get("taskDCurrentUserAuthorized") is not True
            else "- 任务D已获当前交互授权，但未完成全部业务回执与事务校验，详见任务D回执和断点状态。"
        ),
        (
            "- 任务E已按当前交互明确授权执行，按文档截止日分组串行提交；事务回执和永久手机号去重台账均通过。"
            if task_e_executed and task_e_receipt.get("transactionalVerificationPassed") and task_e_receipt.get("ledgerVerificationPassed")
            else "- 任务E默认关闭；本轮无当前真实用户授权，未生成候选、未读取去重台账、未查询任务E号码、未写到期标记。"
            if config.get("taskECurrentUserAuthorized") is not True
            else "- 任务E已获当前交互授权，但未完成事务回执或永久去重台账校验，详见任务E回执和断点状态。"
        ),
        (
            ("- 任务F未联网执行：仅任务A模式可只读冻结本地日期；本轮不生成任务F增量文件。" if config.get("reportDateCachePath") else "- 任务F未执行：仅任务A模式（task_a_only）完整跳过；本地库与补查缺口未检查，本轮不生成F库或截止日模板。")
            if task_a_only
            else "- 本轮主表日期完整，任务F未联网；没有新增任务F文件提示。"
            if config.get("taskFSkippedReason") in {"no_missing_activation_after_fallback", "no_missing_authoritative_dates"}
            else "- 任务F已分离为后台作业；主表交付不代表后台已完成，最终入库与本次增量文件以独立任务F报告为准，不改写本轮主表与审计清单。"
            if config.get("taskFBackground")
            else "- 任务F已在本轮文档生成后后台静默执行；结果保存在本地库，本轮冻结主表不变、下次按字段补缺；不自动上传云端。新增文件只包含本次新入库有效激活日期，不用全库或旧文件代替本次更新。"
            if task_f_executed and not config.get("taskFLoginRequired") and int(config.get("taskFTransientErrorCount") or 0) == 0
            else "- 任务F默认自动目录同步未完整完成，详见任务F回执和断点状态。"
        ),
    ])
    for note in config.get("extraAnomalies") or []:
        lines.append(f"- {note}")

    lines.extend(v_swap_lines(config.get("vGroupSwapDaily")))
    difference = config.get("systemMissingFDates") or {}
    from opensms_f_date_import import import_summary
    f_import_text = import_summary(config.get('fDateImport'))
    difference_count = difference.get("exportedRowCount", difference.get("rowCount"))
    if (difference.get("independentReadbackPassed") and difference.get("output")
            and type(difference_count) is int and difference_count > 0):
        format_description = (
            f"按用户截止日模板导出 {difference['exportedRowCount']} 条，列为ICCID、截止日、激活日期；全部源行及F库来源保留在独立核对JSON。"
            if difference.get('formatVersion') == 'task_f_deadline_template_v2'
            else "保留系统全部源列并追加F库字段。")
        lines.extend(["", "### 系统缺激活日期、F库已有", "",
            f"匹配 {difference['rowCount']} 行，{difference['uniqueIccidCount']} 个ICCID。原始系统激活日期为空，本地F库有有效日期；{format_description} {f_import_text}",
            f"[差异Excel](<{difference['output']}>)；独立逐行回读通过。"])
        if difference.get('sourceDetail'):
            lines.append(f"[完整来源核对明细](<{difference['sourceDetail']}>)；模板SHA256：`{difference['templateSha256']}`。")
    f_import = config.get('fDateImport') or {}
    if f_import:
        lines.extend(['', '### F日期自动导入结果', '', f_import_text])
        if f_import.get('receipt'):
            lines.append(f"[本轮提交及独立回读回执](<{f_import['receipt']}>)")
        if f_import.get('error'):
            lines.append('断点：' + f_import['error'])
    lines = compact_phone_tables(lines)

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "report": str(output),
        "lines": len(lines),
        "actualWrites": total_actual,
        "successfulWrites": total_success,
        "failedWrites": total_failure,
        "sourceArchive": str(Path(config["sourceArchive"]).resolve()),
        "cardResult": str(Path(config["cardResult"]).resolve()),
    }
    if args.receipt:
        receipt = Path(args.receipt).resolve()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["receipt"] = str(receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
