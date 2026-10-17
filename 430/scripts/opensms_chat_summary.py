#!/usr/bin/env python3
"""Render the seven-section user summary from this run's final JSON only."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from opensms_task_d_report_details import TASK_D_LABEL, TASK_E_LABEL
from opensms_task_d_policy import population_statistics_removed


def confirmed_task_d_retention_summary(summary):
    """Accept only the completed population-count contract, never infer zeros."""
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


def task_d_retention_line(task):
    if population_statistics_removed(task):
        return ""
    summary = confirmed_task_d_retention_summary(task.get("retentionSummary")) if task.get("status") == "passed" else None
    if task.get("retentionCarrierTransferEnabled") is False:
        total = f"{summary['totalInRetentionCount']} 个" if summary else "未知（全量统计尚未核验）"
        return f"- 任务D保留区：转入功能已删除，本轮新转入0；执行后保留区总数 {total}。"
    if summary is None:
        return "- 任务D保留区：本轮新转入数量、执行后总量均未知（任务或全量统计尚未核验完成），不以申请记录或候选数代替。"
    return (f"- 任务D保留区：本轮新转入保留卡区 {summary['newTransferredCount']} 个；"
            f"执行后保留区共 {summary['totalInRetentionCount']} 个。")


def task_d_detail_lines(task, run_id=None):
    """Only use final JSON; do not reopen receipts or contact the backend."""
    from opensms_task_d_notification_summary import summary_lines
    lines = summary_lines(task.get("notificationSummary"), run_id)
    if "filteredUnmatchedCount" in task:
        lines.append(f"- 卡保留筛选：命中 {task.get('retentionMatchedCardCount', '未知')}，"
                     f"保护排除 {task.get('neverProcessSourceExcludedCount', '未知')}，"
                     f"未配置运营商跳过 {task.get('unconfiguredCarrierSkippedCount', 0)}，"
                     f"已删除运营商跳过 {task.get('retiredCarrierSkippedCount', 0)}，"
                     f"异常数据跳过 {task.get('dataAnomalySkippedCount', 0)}，"
                     f"未命中跳过 {task['filteredUnmatchedCount']}（不计成功，逐号见报告）。")
    summary = None if population_statistics_removed(task) else confirmed_task_d_retention_summary(task.get("retentionSummary"))
    if task.get("status") == "passed" and summary:
        lines.extend(["", "| 保留区 | 本轮新转入 | 执行后总量 |", "| --- | ---: | ---: |"])
        for row in summary["byCarrier"]:
            lines.append(f"| {row['carrierName']} | {row['newTransferredCount']} | {row['totalInRetentionCount']} |")
        lines.append("")
    return lines


def render_summary(result: dict) -> str:
    successful = result.get("status") == "success"
    task_a_only = result.get("taskAOnly") is True and result.get("taskSelection") == "A"
    def count(name, source=None):
        value = (source if source is not None else result).get(name)
        return "未知" if value is None else value

    lines = ["## 本轮概况", "",
             f"{'完成' if successful else '失败'}；运行编号 `{result.get('runId', '未知')}`；服务器日期 {result.get('serverDate', '未获取')}。",
             "", "## 0回复与回复率", ""]
    seconds = (result.get('performance') or {}).get('pipelineWallSecondsBeforeFinalManifest')
    if successful and type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
        lines.insert(3, f"主报表程序耗时约 {seconds:.1f} 秒（最终清单前，不含启动前等待及后台F）；最终文件已就绪，先交付，不等待F。")
    if task_a_only:
        lines.insert(3, "执行范围：仅任务A；任务B/C/D/E/F完整跳过（task_a_only）。")
    if successful:
        rate = "未知" if result.get("overallReplyRatePct") is None else f"{count('overallReplyRatePct')}%"
        lines.append(f"0回复 {count('currentZero')} 个；整体回复率 {rate}。休息区转入{'开启' if result.get('restZoneEntryEnabled') else '关闭'}，命中保持 {count('restZoneEntryHeldCount')} 个。")
    else:
        lines.append("本轮未形成最终统计结果。")
    # Per-carrier comparisons belong only in the full Markdown report.
    # Keep the overall reply rate above and all twelve balance groups below.
    lines.extend(["", "## 运营商调整", ""])
    if successful:
        lines.append(f"候选 {count('candidateCount')}；后台有效 {count('backendValidCount')}；实际写入 {count('actualWriteCount')}；通过独立复核 {count('independentVerificationPassed')} 组；最终回读覆盖 {count('liveWorkbookRefreshCount')} 个。")
        lines.append("")
        for key in ("taskB", "taskC"):
            task = result.get(key) or {}
            if task_a_only:
                lines.append(f"- 任务{key[-1]}：仅任务A模式，完整跳过。")
            elif task.get("status") == "passed":
                lines.append(f"- 任务{key[-1]}：候选 {count('candidateCount', task)}，后台有效 {count('backendValidCount', task)}，实际写入 {count('actualWriteCount', task)}，独立复核通过 {count('independentVerificationPassed', task)} 组。")
                if task.get("unconfiguredCarrierSkippedCount"):
                    lines.append(f"- 任务{key[-1]}未配置运营商跳过 {task['unconfiguredCarrierSkippedCount']} 行，不分配。")
                if key == "taskB":
                    if task.get("sourceCarrierScope") == "all_carriers_v1":
                        proof = "已核验" if task.get("sourceCarrierCoveragePassed") is True else "未确认"
                        lines.append(f"- 任务B覆盖全部来源运营商（含续费区、三方隔离），逐运营商覆盖{proof}。")
                    else:
                        lines.append(f"- B14保留卡保护：排除 {count('retainedSourceExcludedCount', task)} 个"
                                 f"（已过期 {count('expiredRetainedSourceExcludedCount', task)}，"
                                 f"明日到期 {count('tomorrowRetainedSourceExcludedCount', task)}）；不修改运营商。")
            else:
                lines.append(f"- 任务{key[-1]}：未执行。")
        for key in ("taskD", "taskE"):
            task = result.get(key) or {}
            status = task.get("status") or "未执行"
            if task_a_only:
                lines.append(f"- 任务{key[-1]}：仅任务A模式，完整跳过。")
            elif str(status).startswith("skipped"):
                lines.append(f"- 任务{key[-1]}：未授权跳过。")
            elif key == "taskD":
                display_status = "业务回执通过，保留区统计未确认" if status == "passed" and not population_statistics_removed(task) and not confirmed_task_d_retention_summary(task.get("retentionSummary")) else status
                lines.append(f"- 任务D：{display_status}；备注：{TASK_D_LABEL}；换号区通知 {count('swapNotificationCount', task)}，保留申请来源去重号码 {count('retentionUniqueCardCount', task)}，运营商写入 {count('carrierActualWrite', task)}。")
                lines.append(task_d_retention_line(task))
                lines.extend(task_d_detail_lines(task, result.get("runId")))
            else:
                lines.append(f"- 任务E：{status}；备注：{TASK_E_LABEL}；候选 {count('candidateCount', task)}，成功提交 {count('submittedCount', task)}，未写入 {count('notWrittenCount', task)}。")
                if task.get("unconfiguredCarrierSkippedCount"):
                    lines.append(f"- 任务E未配置运营商跳过 {task['unconfiguredCarrierSkippedCount']} 行，不通知。")
                if task.get('notWrittenCount'):
                    lines.append("- 请排查任务E后台未找到的号码：完整报告提供一行一个号码的复制区；这些号码未写入、不计成功。")
                if task.get("retentionDeadlineAlertRequired"):
                    lines.append(f"提醒：{count('retentionDeadlineMatchPhoneCount', task)} 个号的卡保留下卡通知时间与文档截止日相同，逐号见完整报告。")
    else:
        lines.append("完成范围以失败报告与断点为准；未完成的写入不计为成功。")
        lines.append("")
        for key in ("taskB", "taskC"):
            task = result.get(key) or {}
            if task.get("status") == "passed":
                lines.append(f"- 任务{key[-1]}已完成：实际写入 {count('actualWriteCount', task)}，独立复核通过 {count('independentVerificationPassed', task)} 组。")
            elif task.get("status") == "pending_stage_readback":
                lines.append(f"- 任务{key[-1]}未完成：{count('stageReadbackPendingGroups', task)} 组尚未完成阶段回读；实际写入 {count('actualWriteCount', task)}。")
        task_d = result.get("taskD") or {}
        if not task_a_only and task_d and not str(task_d.get("status", "")).startswith("skipped"):
            if task_d.get("status") == "passed" and (population_statistics_removed(task_d) or confirmed_task_d_retention_summary(task_d.get("retentionSummary"))):
                lines.append(f"- 任务D已完成：换号区通知 {count('swapNotificationCount', task_d)}，保留申请来源去重号码 {count('retentionUniqueCardCount', task_d)}，运营商写入 {count('carrierActualWrite', task_d)}。")
            lines.append(task_d_retention_line(task_d))
            lines.extend(task_d_detail_lines(task_d, result.get("runId")))
        if result.get("taskDUncertainSubmittedCount"):
            lines.append(f"- 任务D：{result['taskDUncertainSubmittedCount']} 个号码提交结果不确定，禁止自动重复提交。")
        if "taskEConfirmedSubmittedCount" in result:
            lines.append(f"- 任务E：已确认提交 {result['taskEConfirmedSubmittedCount']} 个；详见断点，不能据此推断已完整执行。")
    from opensms_task_g import report_lines as task_g_lines
    lines.extend(task_g_lines(result.get("taskG")))
    lines.extend(["", "## 下架恢复", ""])
    probe = result.get('expiredCardProbe')
    if probe and probe.get('background') is True:
        lines.append('过期下架卡T/C全区存活测试：后台独立处理，不筛截止日，不等待测试结果、不改写本轮文档。')
        lines.append(f"派发快照：{probe.get('status', '未知')}（非当前状态）；已上架每轮一次，未上架只尝试一次，未恢复不重复测试。")
        if probe.get('error'):
            lines.append('后台派发未确认，未执行测试；主文档不受影响，详见完整回执。')
    elif probe:
        probe_count = lambda key: probe.get(key, '未知')
        lines.append(f"仅过期下架卡C/T、截止日早于平台今天：{probe.get('status', '未确认')}；"
                     f"候选 {probe_count('candidateCount')}，下架提交 {probe_count('offlineSubmittedCount')}，"
                     f"恢复接受 {probe_count('acceptedCount')}，回读已上架 {probe_count('onlineVerifiedCount')}，"
                     f"待确认 {probe_count('pendingCount')}，已测跳过 {probe_count('alreadyTestedSkippedCount')}。")
        if probe.get('unknownDateCount') or probe.get('ineligibleStatusCount') or probe.get('invalidIdentityCount'):
            lines.append(f"缺有效截止日 {count('unknownDateCount', probe)}，其他/异常状态 {count('ineligibleStatusCount', probe)}，号码/身份格式异常 {count('invalidIdentityCount', probe)}；未操作。")
        if probe.get('pendingCount') or probe.get('blockedCount') or probe.get('error'):
            lines.append("异步或提交结果尚未确认，不重复测试；详见独立回执。")
    else:
        lines.append("功能永久关闭，未执行。")  # Historical reports retain their original contract.
    lines.extend(["", "## 余量与提醒", ""])
    groups = {row.get("group"): row for row in result.get("balanceGroups") or []}
    for name in ("A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V"):
        value = groups.get(name, {}).get("percentRemaining")
        if value is None:
            lines.append(f"- {name}：未知。")
        else:
            alert = "，低于10%紧急" if value < 10 else "，低于20%提醒" if value < 20 else ""
            lines.append(f"- {name}：{value:.2f}%{alert}。")
    lines.extend(["", "## 文件与回执", ""])
    for key, label in (("sourceWorkbook", "源文件"), ("finalWorkbook", "最终合并卡槽结果表"), ("report", "完整报告")):
        file = result.get(key)
        if key == "finalWorkbook" and not file and result.get("latestVerifiedWorkbook"):
            file = result["latestVerifiedWorkbook"]
            label = "最近已核验阶段合并表（非全任务最终表）"
        if file:
            lines.extend([f"[{label}](<{file}>)  ", str(file), ""])
        else:
            lines.append(f"{label}：本轮未生成。")
    daily = result.get("vGroupSwapDaily") or {}
    difference = result.get("systemMissingFDates") or {}
    difference_count = difference.get("exportedRowCount", difference.get("rowCount"))
    from opensms_f_date_import import import_summary
    f_import_text = import_summary(result.get('fDateImport'))
    if (difference.get("independentReadbackPassed") and difference.get("output")
            and type(difference_count) is int and difference_count > 0):
        if difference.get('formatVersion') == 'task_f_deadline_template_v2':
            lines.append(f"[系统缺激活日期、F库已有模板导入表](<{difference['output']}>)：{difference['exportedRowCount']} 条，匹配源表 {difference['rowCount']} 行；{f_import_text}")
        else:
            lines.append(f"[系统缺激活日期、F库已有差异表](<{difference['output']}>)：{difference['rowCount']} 行，{difference['uniqueIccidCount']} 个ICCID；未上传系统。")
    if daily.get("status") == "passed" and daily.get("independentReadbackPassed"):
        today = daily["today"]
        lines.append(f"[V组每日转换号统计](<{daily['output']}>)：{daily['date']}（北京时间，截至本轮）B1V {today['B1V']}、B11V {today['B11V']}、B12V {today['B12V']}，去重合计 {today['unique']} 个。仅含已核验程序写入，启用前历史未回填。")
    lines.extend(["", "## 异常", ""])
    for task_key in ("taskB", "taskC", "taskD", "taskE"):
        task_anomalies = result.get(task_key) or {}
        if task_anomalies.get("dataAnomalySkippedCount"):
            lines.append(f"任务{task_key[-1]}异常数据跳过 {task_anomalies['dataAnomalySkippedCount']} 条，未计成功；已处理其它合法记录，待你指导。")
            evidence = task_anomalies.get("dataAnomalyEvidencePath") or (task_anomalies.get("retentionReceipt") if task_key == "taskD" else None)
            if evidence:
                lines.append(f"[异常明细与原始依据](<{evidence}>)")
    if (result.get('fDateImport') or {}).get('status') in ('failed_before_submission', 'submission_outcome_uncertain', 'accepted_pending_readback'):
        lines.append('F日期自动导入：' + f_import_text)
    if successful:
        audit = result.get("audit") or {}
        lines.append(f"审计通过 {count('passed', audit)} 项，警告 {count('warnings', audit)} 项，失败 {count('failed', audit)} 项。")
        task = result.get("taskF") or {}
        if task_a_only:
            if result.get("reportDateCachePath"):
                lines.append("任务F：未联网执行（task_a_only）；主表可复用生成前冻结的本地日期，未生成任务F增量文件。")
            else:
                lines.append("任务F：未执行（task_a_only）；未检查本地库或补查缺口，未生成F库及截止日模板。")
        elif task.get("reason") in {"no_missing_activation_after_fallback", "no_missing_authoritative_dates"}:
            lines.append("任务F：本轮10日表与F库均缺激活日期的有效ICCID为0，无需联网补查。"
                if task.get('dateEligibilityMode') == 'raw_10day_activation_missing_and_local_activation_missing'
                else "任务F：主表日期已完整，无需联网补查。")
        elif task.get("background"):
            status = task.get("status", "未知")
            if task.get("statusIsLaunchSnapshot") is True:
                lines.append(f"任务F：派发时快照 {status}（非当前运行状态）；后台补查不阻塞本轮最终文件；完成和入库数量尚未确认，请以独立F状态为准。")
            elif status in {"queued", "running", "pending"}:
                lines.append(f"任务F：{status}，后台补查不阻塞本轮最终文件；完成和入库数量尚未确认，进度见完整报告。")
            elif status in {"failed", "blocked", "needs_login"}:
                lines.append(f"任务F：{status}；本轮最终文件已独立完成，补查异常见完整报告。")
            else:
                lines.append(f"任务F：{status}；新增有效激活日期 {count('newActivationImportedCount', task)} 条。")
            if (task.get("status") in {"passed", "success", "completed"}
                    and task.get("statusIsLaunchSnapshot") is not True
                    and type(task.get("newActivationImportedCount")) is int
                    and task["newActivationImportedCount"] > 0 and task.get("incrementalWorkbook")):
                lines.append("本轮任务F新入库数据已生成增量Excel，位置见完整报告；新日期从下次报表补入。")
        else:
            lines.append(f"任务F：{task.get('status', '未知')}；库存 {count('inventoryCount', task)}，待补查 {count('pendingCount', task)}，补入 {count('foundImportedCount', task)}，缺激活标红 {count('documentMissingActivationRedRows', task)} 行。ICCID库位置见完整报告。")
        if not task_a_only and not task.get("background") and task.get("reportBackfillMode"):
            lines.append(f"空激活范围：本地已有可导出 {count('readyForUploadCount', task)}，旧空记录重查 {count('refreshExistingMissingCount', task)}，无法查询排除 {count('documentExcludedCount', task)}；仅保存本地，云端由你人工导入。")
    else:
        lines.append(str(result.get("error") or "请查看失败报告。"))
        if task_a_only:
            lines.append("任务F：未执行（task_a_only）；未检查本地库或补查缺口，未生成F库及截止日模板。")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    args = parser.parse_args()
    print(render_summary(json.loads(args.summary.read_text(encoding="utf-8"))), end="")
