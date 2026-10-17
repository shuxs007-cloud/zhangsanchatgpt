#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path

from opensms_generate_from_downloads import analyze_export


def parse_args():
    parser = argparse.ArgumentParser(description="生成任务B执行报告与总回执")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--export-receipt", type=Path, required=True)
    parser.add_argument("--google-receipt", type=Path, required=True)
    parser.add_argument("--day-date-receipt", type=Path, required=True)
    parser.add_argument("--phone-date-receipt", type=Path, required=True)
    parser.add_argument("--card-result", type=Path, required=True)
    parser.add_argument("--card-receipt", type=Path, required=True)
    parser.add_argument("--expired-candidates", type=Path, required=True)
    parser.add_argument("--tomorrow-candidates", type=Path, required=True)
    parser.add_argument("--expired-write", type=Path, required=True)
    parser.add_argument("--tomorrow-write", type=Path, required=True)
    parser.add_argument("--expired-verify", type=Path, required=True)
    parser.add_argument("--expired-full-verify", type=Path, required=True)
    parser.add_argument("--tomorrow-verify", type=Path, required=True)
    parser.add_argument("--wps-receipt", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--final-output-dir", type=Path)
    return parser.parse_args()


def load(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, payload):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def successful_updates(receipt):
    total = 0
    for response in receipt.get("updateResponses", []):
        if response.get("code") == 200 and not response.get("skipped"):
            data = response.get("data") or {}
            total += int(data.get("updated") or 0)
    return total


def group_summary(write_receipt, verify_receipt):
    before = write_receipt["before"]
    candidate_count = int(before.get("candidateCount") or 0)
    missing = int(before.get("missingCount") or 0)
    duplicate = int(before.get("duplicateBackendCount") or 0)
    non_source = int(before.get("nonSourceCount") or 0)
    eligible = int(before.get("eligibleCount") or 0)
    success = successful_updates(write_receipt)
    backend_valid = candidate_count - missing - duplicate - non_source
    return {
        "candidateCount": candidate_count,
        "backendValidCount": backend_valid,
        "alreadyTargetCount": int(before.get("alreadyTargetCount") or 0),
        "nonSourceCount": non_source,
        "nonSourceDetails": before.get("nonSource") or [],
        "missingCount": missing,
        "duplicateCount": duplicate,
        "actualWriteCount": eligible,
        "successCount": success,
        "failureCount": max(eligible - success, 0),
        "verifyTargetCount": int(verify_receipt.get("targetCount") or 0),
        "verifyStillNotTargetCount": int(verify_receipt.get("stillNotTargetCount") or 0),
        "verifyMissingCount": int(verify_receipt.get("missingCount") or 0),
        "verifyDuplicateCount": int(verify_receipt.get("duplicateBackendCount") or 0),
        "verifyPassed": verify_receipt.get("passed") is True,
    }


def delivery_path(path, final_output_dir, bucket=""):
    if final_output_dir is None:
        return path.resolve()
    return (final_output_dir.resolve() / bucket / path.name).resolve()


def non_source_summary(groups):
    details = []
    for group in groups:
        for item in group.get("nonSourceDetails") or []:
            row = item.get("row") or {}
            details.append(
                f"号码{item.get('phone', '未知')}，卡槽{row.get('slot', '未知')}，"
                f"OPR {row.get('opr', '未知')}"
            )
    return "；".join(details) if details else "无"


def link(label, path):
    resolved = path.resolve()
    return [f"[{label}]({resolved})", "", str(resolved), ""]


def main():
    args = parse_args()
    required_paths = [
        args.source, args.export_receipt, args.google_receipt, args.day_date_receipt,
        args.phone_date_receipt, args.card_result, args.card_receipt,
        args.expired_candidates, args.tomorrow_candidates, args.expired_write,
        args.tomorrow_write, args.expired_verify, args.expired_full_verify,
        args.tomorrow_verify, args.wps_receipt,
    ]
    missing_files = [str(path) for path in required_paths if not path.is_file()]
    if missing_files:
        raise FileNotFoundError(f"报告输入缺失: {missing_files}")

    export_receipt = load(args.export_receipt)
    google_receipt = load(args.google_receipt)
    day_date = load(args.day_date_receipt)
    phone_date = load(args.phone_date_receipt)
    card_receipt = load(args.card_receipt)
    expired_write = load(args.expired_write)
    tomorrow_write = load(args.tomorrow_write)
    expired_verify = load(args.expired_verify)
    expired_full_verify = load(args.expired_full_verify)
    tomorrow_verify = load(args.tomorrow_verify)
    wps = load(args.wps_receipt)
    analysis = analyze_export(args.source)

    expired = group_summary(expired_write, expired_full_verify)
    tomorrow = group_summary(tomorrow_write, tomorrow_verify)
    card_checks = card_receipt.get("checks") or {}
    card_failed = card_receipt.get("failed_checks") or []
    source_sha = file_sha256(args.source)
    result_sha = file_sha256(args.card_result)
    server_dates_match = day_date.get("serverDate") == phone_date.get("serverDate")
    verification = card_receipt.get("verification") or {}
    window_rate = verification.get("window_rate_review") or {}
    zero_total = verification.get("zero_total_reply_review") or {}
    all_passed = (
        export_receipt.get("saved", {}).get("size", 0) > 100_000
        and args.source.stat().st_size == export_receipt.get("saved", {}).get("size")
        and server_dates_match
        and card_receipt.get("result") == "成功"
        and not card_failed
        and expired["failureCount"] == 0
        and expired["verifyPassed"]
        and expired_verify.get("passed") is True
        and tomorrow["failureCount"] == 0
        and tomorrow["verifyPassed"]
        and not wps.get("checkError")
    )

    args.report_dir.mkdir(parents=True, exist_ok=True)
    args.snapshot_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"opensms_task_b_report_{args.tag}.md"
    final_receipt_path = args.snapshot_dir / f"opensms_task_b_{args.tag}_final_receipt.json"

    lines = [
        "## 本轮概况",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        "| 任务 | 使用刚刚最新的10日报表及卡槽关联结果强制执行任务 B |",
        "| 强制授权 | 当前交互明确授权，`OPENSMS_FORCE_TASK_B=1` 已满足 |",
        f"| 后台服务器日期 | {day_date.get('serverDate')} |",
        f"| 明日到期截止日 | {load(args.tomorrow_candidates).get('tomorrowDate')} |",
        f"| 本轮源表行数 | {card_receipt.get('process', {}).get('main_rows')} |",
        f"| 本轮源表 SHA-256 | `{source_sha}` |",
        f"| Google 表 SHA-256 | `{google_receipt.get('sha256')}` |",
        f"| 卡槽结果复核 | {sum(1 for value in card_checks.values() if value)}/{len(card_checks)} 通过 |",
        f"| 总实际写入 | {expired['actualWriteCount'] + tomorrow['actualWriteCount']} |",
        f"| 总成功数 | {expired['successCount'] + tomorrow['successCount']} |",
        f"| 总失败数 | {expired['failureCount'] + tomorrow['failureCount']} |",
        f"| 整体结果 | {'通过' if all_passed else '失败'} |",
        "",
        "## 0回复与回复率",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 最新统计日 | {day_date.get('serverDate')} |",
        f"| 当日发送窗口数 | {analysis.get('send', 0)} |",
        f"| 当日回复窗口数 | {analysis.get('reply', 0)} |",
        f"| 当日整体回复率 | {analysis.get('replyRatePct')}% |",
        f"| 发送大于70且0回复 | {analysis.get('zeroReplySendGt70', 0)} |",
        f"| 今日总回复量为0 | {zero_total.get('today_actual_rows', '未知')} |",
        f"| 今日昨天总回复量均为0 | {zero_total.get('today_yesterday_actual_rows', '未知')} |",
        f"| 持续2天降低 | {(window_rate.get('actual_decreasing_counts') or {}).get('持续2天降低', 0)} |",
        f"| 持续3天降低 | {(window_rate.get('actual_decreasing_counts') or {}).get('持续3天降低', 0)} |",
        f"| 持续4天降低 | {(window_rate.get('actual_decreasing_counts') or {}).get('持续4天降低', 0)} |",
        "",
        "## 运营商调整",
        "",
        "| 分组 | 候选数 | 后台有效数 | 提交前已在目标 | 已离开允许来源 | 后台缺失 | 重复记录 | 实际写入 | 成功数 | 失败数 | 独立复核已在目标 | 仍非目标 | 复核缺失 | 复核重复 | 结果 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        f"| 已过期转“过期下架卡” | {expired['candidateCount']} | {expired['backendValidCount']} | {expired['alreadyTargetCount']} | {expired['nonSourceCount']} | {expired['missingCount']} | {expired['duplicateCount']} | {expired['actualWriteCount']} | {expired['successCount']} | {expired['failureCount']} | {expired['verifyTargetCount']} | {expired['verifyStillNotTargetCount']} | {expired['verifyMissingCount']} | {expired['verifyDuplicateCount']} | {'通过' if expired['verifyPassed'] and expired['failureCount'] == 0 else '失败'} |",
        f"| 明日到期转“快过期隔离区” | {tomorrow['candidateCount']} | {tomorrow['backendValidCount']} | {tomorrow['alreadyTargetCount']} | {tomorrow['nonSourceCount']} | {tomorrow['missingCount']} | {tomorrow['duplicateCount']} | {tomorrow['actualWriteCount']} | {tomorrow['successCount']} | {tomorrow['failureCount']} | {tomorrow['verifyTargetCount']} | {tomorrow['verifyStillNotTargetCount']} | {tomorrow['verifyMissingCount']} | {tomorrow['verifyDuplicateCount']} | {'通过' if tomorrow['verifyPassed'] and tomorrow['failureCount'] == 0 else '失败'} |",
        "",
        f"- 已过期实际写入独立复核：{expired_verify.get('targetCount')}/{expired_verify.get('total')} 已在目标，结果{'通过' if expired_verify.get('passed') else '失败'}。",
        f"- 已过期完整候选独立复核：{expired_full_verify.get('targetCount')}/{expired_full_verify.get('total')} 已在目标，结果{'通过' if expired_full_verify.get('passed') else '失败'}。",
        f"- 明日到期独立复核：{tomorrow_verify.get('targetCount')}/{tomorrow_verify.get('total')} 已在目标，结果{'通过' if tomorrow_verify.get('passed') else '失败'}。",
        "",
        "## 下架恢复",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        "| 候选数 | 未生成 |",
        "| 后台有效数 | 未验证 |",
        "| 实际写入 | 0 |",
        "| 成功数 | 0 |",
        "| 失败数 | 0 |",
        "| 独立复核结果 | 本轮任务B不包含下架恢复 |",
        "",
        "## 余量与提醒",
        "",
        "| 分组 | 当日剩余量 | 当日总量 | 剩余比例 | 回复率 | 回复率统计日期 | 状态 |",
        "|---|---:|---:|---:|---:|---|---|",
        "| A1 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| A12 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| A11 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| B1 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| B11 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| B12 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| C2 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "| A3 | 未知 | 未知 | 未知 | 未知 | 未知 | 本轮任务B未查询 |",
        "",
        "## 文件与回执",
        "",
    ]
    for label, path in [
        ("打开本轮源文件", delivery_path(args.source, args.final_output_dir)),
        ("打开卡槽关联结果表", delivery_path(args.card_result, args.final_output_dir)),
        ("打开完整任务B报告", delivery_path(report_path, args.final_output_dir)),
        ("打开本轮 Google 关联表", delivery_path(Path(google_receipt["outputPath"]), args.final_output_dir)),
        ("打开卡槽结果完整复核回执", delivery_path(args.card_receipt, args.final_output_dir, "receipts")),
        ("打开任务B已过期候选", delivery_path(args.expired_candidates, args.final_output_dir, "receipts")),
        ("打开任务B明日到期候选", delivery_path(args.tomorrow_candidates, args.final_output_dir, "receipts")),
        ("打开已过期写入回执", delivery_path(args.expired_write, args.final_output_dir, "receipts")),
        ("打开明日到期写入回执", delivery_path(args.tomorrow_write, args.final_output_dir, "receipts")),
        ("打开已过期实际写入独立复核", delivery_path(args.expired_verify, args.final_output_dir, "receipts")),
        ("打开已过期完整候选独立复核", delivery_path(args.expired_full_verify, args.final_output_dir, "receipts")),
        ("打开明日到期独立复核", delivery_path(args.tomorrow_verify, args.final_output_dir, "receipts")),
        ("打开程序化文档检查回执", delivery_path(args.wps_receipt, args.final_output_dir, "receipts")),
    ]:
        lines.extend(link(label, path))
    lines.extend([
        "## 异常",
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        f"| 卡槽复核失败项 | {', '.join(card_failed) if card_failed else '无'} |",
        "| 协议错误 | 无 |",
        f"| 后台缺失 | {expired['missingCount'] + tomorrow['missingCount']} |",
        f"| 后台重复 | {expired['duplicateCount'] + tomorrow['duplicateCount']} |",
        f"| 复核非目标 | {expired['verifyStillNotTargetCount'] + tomorrow['verifyStillNotTargetCount']} |",
        f"| 已离开允许来源详情 | {non_source_summary((expired, tomorrow))} |",
        "| 程序化处理 | WPS未打开；使用本轮卡槽结果完整复核回执作为检查依据 |",
        "| 其它异常 | 无 |" if all_passed else "| 其它异常 | 本轮存在失败项，请检查回执 |",
        "",
    ])
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    final_receipt = {
        "result": "成功" if all_passed else "失败",
        "task": "B",
        "forceGate": True,
        "serverDate": day_date.get("serverDate"),
        "sourcePath": str(args.source.resolve()),
        "sourceSha256": source_sha,
        "cardResultPath": str(args.card_result.resolve()),
        "cardResultSha256": result_sha,
        "googleReceiptPath": str(args.google_receipt.resolve()),
        "cardReceiptPath": str(args.card_receipt.resolve()),
        "expired": expired,
        "tomorrow": tomorrow,
        "localReportPath": str(report_path.resolve()),
        "reportPath": str(delivery_path(report_path, args.final_output_dir)),
        "allInputs": [str(path.resolve()) for path in required_paths],
    }
    final_receipt["finalReceiptPath"] = str(final_receipt_path.resolve())
    write_json(final_receipt_path, final_receipt)
    print(json.dumps(final_receipt, ensure_ascii=False, indent=2))
    if not all_passed:
        raise SystemExit(4)


if __name__ == "__main__":
    main()
