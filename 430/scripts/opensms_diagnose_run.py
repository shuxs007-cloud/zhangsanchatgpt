#!/usr/bin/env python3
"""Read-only, run-bound timing diagnosis. Never launches or retries business work."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = re.compile(r"\d{8}T\d{6}[+-]\d{4}\Z")
TERMINAL = {"success", "failed", "error", "completed"}
MAX_BYTES = 16 * 1024 * 1024
PROOF_NAMES = re.compile(r"(?:opensms_document_dispatch_|opensms_card_slot_report_|opensms_local_bundle_).+\.json\Z")


class EvidenceError(ValueError):
    pass


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def seconds(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 3)


def moment(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("时间缺失或不是ISO格式") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("时间必须包含时区")
    return parsed


def delta(start: Any, end: Any) -> float | None:
    try:
        return seconds((moment(end) - moment(start)).total_seconds())
    except EvidenceError:
        return None


def load_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise EvidenceError("证据缺失或超过16MB限制")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError("证据JSON无法解析") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("证据JSON不是对象")
    return payload


def last_json_object(text: str) -> dict:
    """Accept a full JSON object or a JSON result after harmless log lines."""
    decoder = json.JSONDecoder()
    found: list[dict] = []
    position = 0
    while position < len(text):
        index = text.find("{", position)
        if index < 0:
            break
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except ValueError:
            position = index + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        position = index + consumed
    if not found:
        raise EvidenceError("日志没有结构化结果，根因未知")
    return found[-1]


class BoundEvidence:
    def __init__(self, state_path: Path, run_id: str):
        self.state_path = state_path
        self.run_id = run_id
        self.paths: list[dict] = []
        self.warnings: list[str] = []
        self.seen: set[Path] = set()

    def path(self, value: Any, kind: str) -> Path:
        if not isinstance(value, str) or not value:
            raise EvidenceError("关联证据路径为空")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
            raise EvidenceError("拒绝相对路径、上级目录或符号链接证据")
        parents = path.parts[:-1]
        matching_parent = self.run_id in parents or f"opensms_patrol_{self.run_id}" in parents
        foreign = [part for part in parents if (
            RUN_ID.fullmatch(part) and part != self.run_id
        ) or (part.startswith("opensms_patrol_") and RUN_ID.fullmatch(part.removeprefix("opensms_patrol_"))
              and part != f"opensms_patrol_{self.run_id}")]
        if not matching_parent or foreign:
            raise EvidenceError("拒绝跨runId目录读取")
        if kind == "log":
            if path.name != "document.log":
                raise EvidenceError("只允许本轮document.log")
        elif not PROOF_NAMES.fullmatch(path.name):
            raise EvidenceError("只允许本轮文档dispatch或卡槽核验回执")
        return path

    def read(self, value: Any, kind: str) -> dict | None:
        try:
            path = self.path(value, kind)
            if path in self.seen:
                return None
            self.seen.add(path)
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise EvidenceError("证据缺失或超过16MB限制")
            raw = path.read_bytes()
            payload = last_json_object(raw.decode("utf-8")) if kind == "log" else load_json(path)
            identities = [payload.get("runId"), payload.get("run_id")]
            identities += [obj(obj(payload.get("inputs")).get(name)).get("run_id")
                           for name in ("system_kooda", "report_date_cache")]
            if any(value not in (None, "", self.run_id) for value in identities):
                raise EvidenceError("证据内容runId不一致")
            self.paths.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "kind": kind})
            return payload
        except (EvidenceError, OSError, UnicodeError) as exc:
            self.warnings.append(f"{kind}关联证据未采用：{exc}")
            return None


def failed_checks(payload: dict) -> list[str]:
    """Only copy explicit check identifiers, never dump raw stderr or source rows."""
    result = []
    for section in (payload, obj(payload.get("local_failure")), obj(payload.get("localFailure")),
                    obj(payload.get("verification"))):
        values = section.get("failed_checks") or section.get("failedChecks") or []
        if isinstance(values, list):
            result += [value[:200] for value in values if isinstance(value, str)]
        checks = section.get("checks")
        if isinstance(checks, dict):
            result += [str(key)[:200] for key, value in checks.items() if value is False]
    return list(dict.fromkeys(result))


def diagnose(state_path: Path, trigger_time: str | None = None) -> dict:
    state_path = state_path.expanduser().absolute()
    if state_path.resolve() != state_path:
        raise EvidenceError("拒绝符号链接state")
    state = load_json(state_path)
    run_id = state.get("runId")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise EvidenceError("state缺少合法runId")
    if state_path.name != f"opensms_patrol_runner_state_{run_id}.json":
        raise EvidenceError("state文件名与runId不一致")
    if trigger_time is not None:
        moment(trigger_time)
    evidence = BoundEvidence(state_path, run_id)
    summary = obj(state.get("summary"))
    perf = obj(summary.get("performance")) or obj(state.get("performance"))
    stages = obj(state.get("stages"))
    acquire = obj(stages.get("acquire")) or obj(state.get("partialAcquisition"))
    document = obj(stages.get("document"))
    status = state.get("status")
    created = state.get("createdAt")
    observed = state.get("completedAt") or state.get("updatedAt")
    measured_end = observed if status in TERMINAL else None
    wall = delta(created, measured_end) if measured_end else None
    prelaunch = delta(trigger_time, created) if trigger_time else None
    if trigger_time and prelaunch is None:
        evidence.warnings.append("触发时间晚于启动或启动时间无效，启动前耗时未知")
    if wall is None:
        evidence.warnings.append("运行尚未终态或起止时间无效，完整程序耗时未知")
    failure = obj(state.get("documentFailure")) or obj(summary.get("documentFailure"))
    log_path = state.get("documentLog") or failure.get("log")
    log = evidence.read(log_path, "log") if log_path else None
    pending: list[Any] = [document.get("dispatchReceipt"), document.get("cardReceipt"), failure.get("dispatchReceipt"),
                          obj(failure.get("localFailure")).get("receipt_path")]
    if log:
        pending += [log.get("dispatch_receipt_path"), log.get("receipt_path"), obj(log.get("local_failure")).get("receipt_path")]
    documents = ([log] if log else []) + ([failure] if failure else [])
    # Bounded graph from explicit same-run references only; no glob or directory scan.
    while pending and len(evidence.seen) < 8:
        reference = pending.pop(0)
        if not reference:
            continue
        payload = evidence.read(reference, "receipt")
        if payload:
            documents.append(payload)
            pending += [payload.get("dispatch_receipt_path"), payload.get("receipt_path"),
                        obj(payload.get("local_failure")).get("receipt_path"),
                        obj(payload.get("local_result")).get("receipt_path")]
    checks = list(dict.fromkeys(check for entry in documents for check in failed_checks(entry)))
    process_timing = obj(state.get("documentProcessTiming"))
    document_seconds = seconds(process_timing.get("documentProcessSeconds"))
    document_source = "state.documentProcessTiming.documentProcessSeconds" if document_seconds is not None else None
    if document_seconds is None:
        document_seconds = seconds(perf.get("documentProcessSeconds"))
        document_source = "summary.performance.documentProcessSeconds" if document_seconds is not None else None
    for value, label in ((document.get("processDurationSeconds"), "stages.document.processDurationSeconds"),
                         (document.get("durationSeconds"), "stages.document.durationSeconds")):
        if document_seconds is None and seconds(value) is not None:
            document_seconds, document_source = seconds(value), label
    if document_seconds is None:
        for entry in documents:
            value = seconds(entry.get("duration_seconds"))
            if value is not None:
                document_seconds, document_source = value, "bound_document_log_or_dispatch.duration_seconds"
                break
    commands = state.get("commandTimings") or perf.get("commandTimings") or []
    commands = [item for item in commands if isinstance(item, dict)] if isinstance(commands, list) else []
    top_commands = sorted(
        [{"command": str(item.get("command") or "unknown"),
          "durationSeconds": seconds(item.get("durationSeconds")), "exitCode": item.get("exitCode")}
         for item in commands if seconds(item.get("durationSeconds")) is not None],
        key=lambda item: item["durationSeconds"], reverse=True)[:12]
    export_command = next((item for item in commands if item.get("command") == "export"), {})
    export_perf = obj(acquire.get("exportPerformance")) or obj(perf.get("exportPerformance"))
    backend_ms = seconds(export_perf.get("backendGenerationMs"))
    timings = {
        "triggerToLaunchSeconds": prelaunch,
        "runnerWallSeconds": wall,
        "triggerToTerminalStateSeconds": delta(trigger_time, measured_end) if trigger_time and measured_end else None,
        "acquisitionWallSeconds": seconds(acquire.get("durationSeconds")),
        "exportCommandSeconds": seconds(export_command.get("durationSeconds")),
        "backendGenerationSeconds": round(backend_ms / 1000, 3) if backend_ms is not None else None,
        "documentProcessSeconds": document_seconds,
        "documentTimingSource": document_source,
        "documentCommandWallSeconds": seconds(process_timing.get("durationSeconds")),
        "documentJoinSeconds": seconds(process_timing.get("documentJoinSeconds")) if seconds(process_timing.get("documentJoinSeconds")) is not None else seconds(perf.get("documentJoinSeconds")),
        "candidateWallSeconds": seconds(obj(stages.get("candidates")).get("durationSeconds")),
        "durationsAreNotAdditive": True,
        "endBoundary": "terminal_state_saved_not_chat_delivered" if measured_end else "no_terminal_evidence",
    }
    result = {
        "schemaVersion": 1, "status": "diagnosed", "runId": run_id, "runStatus": status,
        "scope": "historical_run_readonly_diagnosis_not_new_delivery",
        "statePath": str(state_path), "stateSha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        "triggerTime": trigger_time, "createdAt": created, "terminalStateAt": measured_end,
        "timings": timings,
        "concurrency": {
            "acquisitionWorkers": acquire.get("readConcurrency") or perf.get("ioWorkers"),
            "analysisWorkers": obj(stages.get("candidates")).get("analysisConcurrency") or perf.get("analysisWorkers"),
            "documentConcurrentWithAnalysis": obj(stages.get("candidates")).get("documentProcessingConcurrentWithAnalysis"),
            "sharedScanCacheUsed": obj(stages.get("candidates")).get("sharedScanCacheUsed"),
            "liveWritesSerial": state.get("liveWritesSerial"),
        },
        "failedChecks": checks, "rootCauseKnown": bool(checks),
        "taskStatuses": {f"task{task}": obj(stages.get(f"task{task}")).get("status", "unknown") for task in "BCDEF"},
        "topCommands": top_commands,
        "aiUsage": {"billedTokens": None, "availableInRunnerState": False,
                    "executionMode": summary.get("executionMode"),
                    "manualAiStepsRequired": summary.get("manualAiStepsRequired"),
                    "measurementNote": "子程序耗时不是AI token；状态不含模型计费，不能据此猜测。"},
        "evidence": evidence.paths, "warnings": evidence.warnings,
        "businessExecuted": False, "networkAccessed": False, "databaseAccessed": False,
    }
    result["markdown"] = render_markdown(result)
    return result


def render_markdown(result: dict) -> str:
    timings = result["timings"]
    labels = (
        ("触发到程序启动", "triggerToLaunchSeconds"), ("程序运行到终态保存", "runnerWallSeconds"),
        ("触发到终态保存", "triggerToTerminalStateSeconds"), ("采集阶段墙钟时间", "acquisitionWallSeconds"),
        ("导出子程序", "exportCommandSeconds"), ("其中：服务器生成", "backendGenerationSeconds"),
        ("本地文档处理", "documentProcessSeconds"), ("并发候选计算", "candidateWallSeconds"),
    )
    lines = [f"# OpenSMS运行耗时诊断：{result['runId']}", "", "本报告仅诊断指定历史运行，不是新鲜导出或新交付。",
             "", f"运行状态：{result['runStatus']}。", "", "| 环节 | 耗时（秒） |", "|---|---:|"]
    lines += [f"| {label} | {timings[key]:.3f} |" if timings[key] is not None else f"| {label} | 未知 |"
              for label, key in labels]
    lines += ["", "服务器生成包含在导出中，导出包含在采集中；文档与候选计算可重叠。不得将这些时长相加。终点不含终态后的聊天交付时间。",
              "", "## 并发与失败证据", "",
              f"采集工作器：{result['concurrency']['acquisitionWorkers'] or '未知'}；分析工作器：{result['concurrency']['analysisWorkers'] or '未知'}。固定规则由本地程序执行，写入及依赖最新状态的阶段保持屏障顺序。",
              "", "失败核验项：" + ("、".join(result["failedChecks"]) if result["failedChecks"] else
                  "无（该轮成功）。" if result["runStatus"] == "success" else
                  "未从已绑定证据取得，根因未知；普通样式警告不能当作根因。")]
    lines += ["", "## 最耗时子程序（不可累加为总耗时）", "",
              "| 子程序 | 秒 | 退出码 |", "| --- | ---: | ---: |"]
    lines += [f"| {item['command']} | {item['durationSeconds']:.3f} | {item['exitCode']} |"
              for item in result.get("topCommands") or []]
    lines += ["", "## AI与固定规则", "",
              "runner状态不提供模型计费token；不得把等待时间、字符数或表格行数当成实际token用量。",
              "固定规则由现有脚本处理；AI仅做规则阅读、授权判断、一次启动、必要异常处理与最终摘要。"]
    if result["warnings"]:
        lines += ["", "证据限制：" + "；".join(result["warnings"])]
    return "\n".join(lines) + "\n"


def write_development_output(result: dict, directory: Path) -> dict:
    directory = directory.expanduser().absolute()
    if directory.resolve() != directory or ".." in directory.parts:
        raise EvidenceError("开发输出路径不能包含上级目录或符号链接")
    try:
        relative = directory.relative_to(ROOT / "outputs")
    except ValueError as exc:
        raise EvidenceError("输出只允许430/outputs/development_*开发目录") from exc
    if not relative.parts or not relative.parts[0].startswith("development_"):
        raise EvidenceError("输出只允许430/outputs/development_*开发目录")
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"opensms_diagnose_{result['runId']}.json"
    md_path = directory / f"OpenSMS_耗时诊断_{result['runId']}.md"
    if json_path.exists() or md_path.exists():
        raise EvidenceError("诊断文件已存在，拒绝覆盖")
    with json_path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with md_path.open("x", encoding="utf-8") as handle:
        handle.write(result["markdown"])
    return {"json": str(json_path), "markdown": str(md_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--trigger-time", help="可选，触发此轮的含时区ISO时间；不得猜测")
    parser.add_argument("--output-dir", type=Path, help="可选，仅写430/outputs/development_*开发目录")
    args = parser.parse_args()
    try:
        result = diagnose(args.state, args.trigger_time)
        if args.output_dir:
            result["diagnosticFiles"] = write_development_output(result, args.output_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (EvidenceError, OSError) as exc:
        print(json.dumps({"status": "diagnosis_failed", "error": str(exc), "businessExecuted": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
