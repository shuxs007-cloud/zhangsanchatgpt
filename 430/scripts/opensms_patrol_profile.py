#!/usr/bin/env python3
"""Summarize OpenSMS patrol stage timings from local receipts/state files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def first(root: Path, pattern: str) -> Path | None:
    paths = sorted(root.glob(pattern))
    return paths[-1] if paths else None


def first_any(root: Path, patterns: tuple[str, ...]) -> Path | None:
    paths = sorted({path for pattern in patterns for path in root.glob(pattern)})
    return paths[-1] if paths else None


def seconds(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def build_profile(
    delivery: Path,
    optimized_refresh_seconds: float | None = None,
    optimized_document_receipt: Path | None = None,
    integration_audit_receipt: Path | None = None,
) -> dict[str, Any]:
    receipts = delivery / "receipts"
    stages: list[dict[str, Any]] = []
    state_path = first(receipts, "opensms_patrol_runner_state_*.json")
    if state_path:
        state = load(state_path)
        for item in state.get("timings") or []:
            duration = seconds(item.get("durationSeconds"))
            if duration is not None:
                stages.append({"stage": item.get("stage"), "seconds": duration, "source": str(state_path)})

    document_path = first_any(
        receipts,
        ("opensms_card_slot_report*_receipt.json", "opensms_card_slot_report_*.json"),
    )
    if document_path:
        document = load(document_path)
        mapping = {
            "document.buildMainWorkbook": "build_main_workbook",
            "document.enrichWorkbook": "enrich_workbook",
            "document.verifyWorkbook": "verify_workbook",
            "document.pipelineTotal": "before_receipt",
        }
        timing = document.get("stage_timings_seconds") or {}
        for stage, key in mapping.items():
            duration = seconds(timing.get(key))
            if duration is not None:
                stages.append({"stage": stage, "seconds": duration, "source": str(document_path)})

    dispatch_path = first_any(
        receipts,
        ("opensms_document_dispatch*_receipt.json", "opensms_document_dispatch_*.json"),
    )
    if dispatch_path:
        dispatch = load(dispatch_path)
        duration = seconds(dispatch.get("duration_seconds"))
        if duration is not None:
            stages.append({"stage": "document.total", "seconds": duration, "source": str(dispatch_path)})

    export_path = first(receipts, "opensms_export_once*.json")
    if export_path:
        export = load(export_path)
        duration = seconds(export.get("elapsedMs"))
        if duration is not None:
            stages.append({"stage": "freshExport", "seconds": round(duration / 1000, 3), "source": str(export_path)})

    audit_path = first(receipts, "opensms_parallel_audit*_receipt.json")
    if audit_path:
        audit = load(audit_path)
        performance = audit.get("performance") or audit.get("timing") or {}
        duration = seconds(performance.get("wall_seconds"))
        if duration is None and performance.get("wall_ms") is not None:
            duration = round(float(performance["wall_ms"]) / 1000, 3)
        if duration is not None:
            serial = seconds(performance.get("serial_equivalent_seconds"))
            saved = seconds(performance.get("saved_seconds"))
            if serial is None and performance.get("serial_sum_ms") is not None:
                serial = round(float(performance["serial_sum_ms"]) / 1000, 3)
            if saved is None and performance.get("estimated_parallel_saving_ms") is not None:
                saved = round(float(performance["estimated_parallel_saving_ms"]) / 1000, 3)
            stages.append({
                "stage": "parallelAudit",
                "seconds": duration,
                "serialEquivalentSeconds": serial,
                "savedSeconds": saved,
                "source": str(audit_path),
            })

    improvements: dict[str, Any] = {}
    if optimized_document_receipt:
        optimized = load(optimized_document_receipt)
        optimized_timing = optimized.get("stage_timings_seconds") or {}
        optimized_mapping = {
            "optimized.document.buildMainWorkbook": "build_main_workbook",
            "optimized.document.enrichWorkbook": "enrich_workbook",
            "optimized.document.verifyWorkbook": "verify_workbook",
            "optimized.document.total": "before_receipt",
        }
        for stage, key in optimized_mapping.items():
            duration = seconds(optimized_timing.get(key))
            if duration is not None:
                stages.append({"stage": stage, "seconds": duration, "source": str(optimized_document_receipt)})
        baseline_total = next(
            (item["seconds"] for item in stages if item["stage"] == "document.pipelineTotal"),
            next((item["seconds"] for item in stages if item["stage"] == "document.total"), None),
        )
        optimized_total = seconds(optimized_timing.get("before_receipt"))
        if baseline_total and optimized_total:
            improvements["documentPipeline"] = {
                "baselineSeconds": baseline_total,
                "optimizedSeconds": optimized_total,
                "savedSeconds": round(baseline_total - optimized_total, 3),
                "speedupX": round(baseline_total / optimized_total, 2),
                "reductionPct": round((baseline_total - optimized_total) / baseline_total * 100, 1),
            }

    if optimized_refresh_seconds is not None:
        stages.append({
            "stage": "optimized.targetedWorkbookRefresh",
            "seconds": round(optimized_refresh_seconds, 3),
            "source": "offline historical replay",
        })
        improvements["targetedWorkbookRefresh"] = {
            "optimizedSeconds": round(optimized_refresh_seconds, 3),
            "historicalObservedRangeSeconds": [480, 720],
            "estimatedReductionPctRange": [
                round((480 - optimized_refresh_seconds) / 480 * 100, 1),
                round((720 - optimized_refresh_seconds) / 720 * 100, 1),
            ],
        }

    stages.sort(key=lambda item: item["seconds"], reverse=True)
    verification = None
    if integration_audit_receipt:
        audit = load(integration_audit_receipt)
        verification = {
            "result": audit.get("result"),
            "summary": audit.get("summary"),
            "timing": audit.get("timing"),
            "receipt": str(integration_audit_receipt.resolve()),
        }
    recommendations = [
        {
            "stage": "document.enrichWorkbook",
            "cause": "两个完整工作簿的解析、格式处理和整本导出",
            "implemented": "已合并进首次生成：主导出与Google关联表并发读取，逐日统计在内存中一次计算，整本工作簿只保存一次；保留独立复核",
            "userAction": "把交付目录放在本机SSD，避免网络盘；关闭实时扫描该目录的同步/杀毒软件",
        },
        {
            "stage": "document.buildMainWorkbook",
            "cause": "大表多次整列搬移与逐单元格格式复制",
            "implemented": "五次列插入合并为两次等价的从右向左块插入",
            "userAction": "Google关联表只保留要求的三个机房页签和有效数据范围，不要保留几十万空白格式行",
        },
        {
            "stage": "workbookRefresh",
            "cause": "过去为更新运营商列而整本导入、导出、再导入",
            "implemented": "只定点修改运营商单元格；其他OOXML成员逐字节保持；第二次独立解析和哈希复核",
            "userAction": "无需操作；不要手动打开本轮工作簿，以免产生锁文件",
        },
        {
            "stage": "acquireAndAnalysis",
            "cause": "互不依赖的只读查询和候选分析原来串行等待",
            "implemented": "Google下载、双日期、运营商解析、余量查询并发；分析、主候选、E恢复并发",
            "userAction": "保持BitBrowser 430已登录且不要频繁重启；网络稳定比增加更多线程更重要",
        },
        {
            "stage": "carrierWrites",
            "cause": "每组必须写入后独立回读，属于安全串行段",
            "implemented": "不并发写入，避免重复写和跨规则竞态",
            "userAction": "不建议提速；若要减少耗时，只能减少候选分组数量，不能取消回读",
        },
        {
            "stage": "aiOrchestration",
            "cause": "AI仅负责启动本地程序并整理最终JSON，通常不是主耗时",
            "implemented": "正常巡检和任务B均已程序化，AI不再手工计算候选或逐步拼文档",
            "userAction": "可选GPT-5.6 Terra中级以缩短交互等待；它不会替代浏览器导出、Excel生成和独立回读的本地耗时",
        },
    ]
    return {
        "delivery": str(delivery.resolve()),
        "state": str(state_path) if state_path else None,
        "stages": stages,
        "improvements": improvements,
        "integrationVerification": verification,
        "recommendations": recommendations,
    }


def markdown(profile: dict[str, Any]) -> str:
    lines = [
        "# OpenSMS 巡检性能剖析",
        "",
        "## 阶段耗时",
        "",
        "| 排名 | 阶段 | 秒数 | 说明 |",
        "| ---: | --- | ---: | --- |",
    ]
    for index, item in enumerate(profile["stages"], start=1):
        extra = ""
        if item.get("savedSeconds") is not None:
            extra = f"并行已节省 {item['savedSeconds']} 秒"
        lines.append(f"| {index} | {item['stage']} | {item['seconds']} | {extra} |")
    lines.append("")
    if profile.get("improvements"):
        lines.extend(["## 已验证提速", "", "```json", json.dumps(profile["improvements"], ensure_ascii=False, indent=2), "```", ""])
    if profile.get("integrationVerification"):
        verification = profile["integrationVerification"]
        summary = verification.get("summary") or {}
        lines.extend([
            "## 完整交付审计回放",
            "",
            f"- 结果：{verification.get('result')}",
            f"- 通过：{summary.get('passed', 0)}；提示：{summary.get('warnings', 0)}；失败：{summary.get('failed', 0)}",
            f"- 实际修改/成功/失败：{summary.get('actual_writes', 0)}/{summary.get('successful_writes', 0)}/{summary.get('failed_writes', 0)}",
            "",
        ])
    lines.extend(["## 优化与需要配合", ""])
    for item in profile["recommendations"]:
        lines.extend([
            f"### {item['stage']}",
            "",
            f"- 慢因：{item['cause']}",
            f"- 已实现：{item['implemented']}",
            f"- 你可以做：{item['userAction']}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenSMS巡检本地阶段耗时剖析")
    parser.add_argument("delivery", type=Path)
    parser.add_argument("--optimized-refresh-seconds", type=float)
    parser.add_argument("--optimized-document-receipt", type=Path)
    parser.add_argument("--integration-audit-receipt", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    profile = build_profile(
        args.delivery,
        args.optimized_refresh_seconds,
        args.optimized_document_receipt,
        args.integration_audit_receipt,
    )
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown(profile), encoding="utf-8")
    print(json.dumps(profile, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
