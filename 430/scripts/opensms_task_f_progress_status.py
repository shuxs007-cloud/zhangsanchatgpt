#!/usr/bin/env python3
"""Read-only Task F progress reporting: no browser, database, network or mutation."""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any


RUN_PATTERN = re.compile(r"\d{8}T\d{6}[+-]\d{4}")
STALE_SECONDS = 900
GOOD_STAGES = {"passed", "sample_passed", "success", "completed"}
FAILED_STAGES = {"failed", "error", "login_required", "partial_transient_error"}


class EvidenceError(RuntimeError):
    pass


def integer(value: Any, field: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if type(value) is not int or value < 0:
        raise EvidenceError(f"{field}必须为非负整数")
    return value


def timestamp(value: Any, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{field}缺少有效时间") from exc
    if result.tzinfo is None:
        raise EvidenceError(f"{field}必须包含时区")
    return result.astimezone(timezone.utc)


def parse_counts(payload: dict, field: str = "statusCounts") -> dict[str, int]:
    values = payload.get(field, {})
    if not isinstance(values, dict):
        raise EvidenceError(f"{field}不是状态计数字典")
    return {str(key): integer(value, f"{field}.{key}") for key, value in values.items()}


def read_processes() -> list[dict[str, Any]]:
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                            timeout=10, check=True)
    records = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)", line)
        if match:
            records.append({"pid": int(match.group(1)), "command": match.group(2)})
    return records


def owned_processes(state: dict, receipts: Path, processes: list[dict]) -> list[dict]:
    run_id = state["runId"]
    runner_pid = state.get("runnerPid") or state.get("pid")
    matched = []
    for process in processes:
        command = str(process.get("command") or "")
        pid = process.get("pid")
        if ("opensms_patrol_runner.py" in command or "opensms_task_f_background.py" in command) and (
            (type(runner_pid) is int and pid == runner_pid)
            or (run_id in command and str(receipts) in command)
        ):
            matched.append({"pid": pid, "kind": "runner"})
        elif "grabandgo_task_f_lookup.mjs" in command and run_id in command and str(receipts) in command:
            matched.append({"pid": pid, "kind": "lookup"})
    return matched


class LocalEvidence:
    def __init__(self, state_path: Path, state: dict, now: datetime):
        self.receipts = state_path.resolve().parent
        self.run_id = state["runId"]
        self.started = timestamp(state.get("createdAt"), "state.createdAt")
        self.now = now
        if self.started > now and (self.started - now).total_seconds() > 60:
            raise EvidenceError("运行开始时间晚于当前时间")
        self.paths = [str(state_path.resolve())]
        self.times = []

    def path(self, candidate: str | Path) -> Path:
        path = Path(candidate)
        if not path.is_absolute():
            path = self.receipts / path
        resolved = path.resolve()
        if resolved.parent != self.receipts or not resolved.name.endswith(f"_{self.run_id}.json"):
            raise EvidenceError("拒绝跨receipts目录或跨runId读取任务F证据")
        return resolved

    def read(self, candidate: str | Path, *, optional: bool = False) -> dict | None:
        path = self.path(candidate)
        if optional and not path.exists():
            return None
        if path.stat().st_size > 64 * 1024 * 1024:
            raise EvidenceError("任务F回执异常过大")
        try:
            result = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise EvidenceError(f"任务F证据读取失败:{path.name}") from exc
        if not isinstance(result, dict):
            raise EvidenceError("任务F证据必须为对象")
        if result.get("runId") not in (None, self.run_id):
            raise EvidenceError("任务F证据runId与状态文件不一致")
        for field in ("updatedAt", "createdAt"):
            if result.get(field):
                value = timestamp(result[field], f"{path.name}.{field}")
                if (value - self.now).total_seconds() > 60 or (self.started - value).total_seconds() > 60:
                    raise EvidenceError("任务F证据时间不属于本轮或位于未来")
                self.times.append(value)
                break
        if str(path) not in self.paths:
            self.paths.append(str(path))
        return result


def import_counts(receipt: dict) -> tuple[int, int, int, int]:
    if receipt.get("status") not in {"passed", "passed_with_transient_errors"}:
        raise EvidenceError("任务F导入回执未通过，不能计作入库")
    total = integer(receipt.get("resultCount"), "import.resultCount")
    inserted = integer(receipt.get("terminalImportedCount"), "import.terminalImportedCount")
    kept = integer(receipt.get("alreadyTerminalKeptCount"), "import.alreadyTerminalKeptCount", 0)
    if inserted + kept > total:
        raise EvidenceError("任务F导入回执数量矛盾")
    return total, inserted + kept, inserted, kept


def markdown(result: dict) -> str:
    if result.get("phase") == "skipped":
        return f"任务F未运行：本轮已跳过（{result.get('reason') or '未启用'}）。未启动或重试任何任务。"
    names = {"in_progress": "查询中", "preparing": "准备中（登录/生成待查清单）", "importing": "查询结束，入库/导出中", "completed": "已完成",
             "failed": "失败", "process_missing": "对应进程已消失", "not_started": "尚未开始查询"}
    show = lambda value: "未知" if value is None else str(value)
    status_names = {"found": "命中", "activation_missing": "未激活", "not_found": "未找到", "error": "错误"}
    counts = "、".join(f"{status_names.get(key, key)} {value}" for key, value in result["statusCounts"].items()) or "暂无"
    elapsed = result.get("elapsedSeconds")
    duration = "未知" if elapsed is None else f"{elapsed // 60}分{elapsed % 60}秒"
    lines = [f"任务F：{names.get(result['phase'], result['phase'])}（{result['runId']}）",
             f"总数 {show(result['total'])}｜已查询 {show(result['completed'])}｜已确认落库 {show(result['persisted'])}｜剩余 {show(result['remaining'])}",
             f"状态：{counts}",
             f"线程 请求/实际 {show(result['requestedWorkers'])}/{show(result['effectiveWorkers'])}｜独立会话 {show(result['independentSessionCount'])}｜模式 {result['sessionMode'] or '未知'}",
             f"批次 {show(result['completedBatchCount'])}/{show(result['batchCount'])}｜经过 {duration}"]
    if result.get("queriedNotPersisted"):
        lines.append(f"已查询但未确认落库 {result['queriedNotPersisted']} 条（含可能的查询失败，不计为入库成功）。")
    if result.get("warnings"):
        lines.append("注意：" + "；".join(result["warnings"]))
    return "\n\n".join(lines)


def inspect_progress(state_path: Path, *, processes: list[dict] | None = None,
                     now: datetime | None = None) -> dict:
    state_path = state_path.resolve()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = json.loads(state_path.read_bytes())
    run_id = state.get("runId") if isinstance(state, dict) else None
    if not isinstance(run_id, str) or not RUN_PATTERN.fullmatch(run_id):
        raise EvidenceError("状态文件runId无效")
    if state_path.parent.name != "receipts" or state_path.name != f"opensms_patrol_runner_state_{run_id}.json":
        raise EvidenceError("状态文件必须是本轮receipts目录中的runner状态")
    evidence = LocalEvidence(state_path, state, now)
    stages = state.get("stages") or {}
    task_f = stages.get("taskF") or {}
    if not isinstance(task_f, dict):
        raise EvidenceError("state.stages.taskF格式无效")
    if task_f.get("background") is True and task_f.get("backgroundState"):
        job_path = Path(task_f.get("backgroundJob") or "")
        if not job_path.is_absolute() or not job_path.is_file():
            raise EvidenceError("后台F绑定清单缺失")
        job_bytes = job_path.read_bytes()
        if hashlib.sha256(job_bytes).hexdigest() != task_f.get("backgroundJobSha256"):
            raise EvidenceError("后台F绑定清单哈希不一致")
        job = json.loads(job_bytes)
        child_state = Path(task_f["backgroundState"]).resolve()
        if (job.get("runId") != run_id or job.get("parentRunId") != run_id
                or Path(job.get("backgroundState") or "").resolve() != child_state
                or child_state == state_path or state_path.parent.parent in child_state.parents
                or job_path.parent.resolve() != child_state.parent.parent
                or job.get("mainReportIndependent") is not True):
            raise EvidenceError("后台F绑定范围或主子运行编号不一致")
        child = json.loads(child_state.read_bytes())
        if (child.get("backgroundTaskF") is not True
                or child.get("backgroundJobSha256") != task_f.get("backgroundJobSha256")
                or (child.get("stages") or {}).get("taskF", {}).get("background") is True):
            raise EvidenceError("后台F子状态归属不一致或循环引用")
        delegated = inspect_progress(child_state, processes=processes, now=now)
        delegated.update({"parentState": str(state_path), "parentPipelineStatus": state.get("status"),
                          "mainReportIndependent": True, "background": True,
                          "backgroundReport": task_f.get("backgroundReport")})
        return delegated
    if state.get("updatedAt"):
        updated = timestamp(state["updatedAt"], "state.updatedAt")
        if (updated - now).total_seconds() > 60 or (evidence.started - updated).total_seconds() > 60:
            raise EvidenceError("状态更新时间不属于本轮或位于未来")
    progress_path = task_f.get("progress") or f"grabandgo_task_f_progress_{run_id}.json"
    progress = evidence.read(progress_path, optional=True)
    process_errors = []
    if processes is None:
        try:
            processes = read_processes()
        except (OSError, subprocess.SubprocessError) as exc:
            processes = []
            process_errors.append(f"进程检查失败:{type(exc).__name__}")
    matched = owned_processes(state, evidence.receipts, processes)
    task_started = None
    elapsed_source = "unknown"
    if state.get("taskFStartedAt") or (progress or {}).get("startedAt"):
        task_started = timestamp(state.get("taskFStartedAt") or progress["startedAt"], "taskFStartedAt")
        elapsed_source = "taskFStartedAt"
    elif task_f.get("completedAt") and isinstance(task_f.get("durationSeconds"), (int, float)):
        duration = task_f["durationSeconds"]
        if isinstance(duration, bool) or duration < 0:
            raise EvidenceError("任务F终态耗时无效")
        task_started = timestamp(task_f["completedAt"], "taskF.completedAt") - timedelta(seconds=duration)
        elapsed_source = "completedAt_minus_durationSeconds"
    if task_started and ((task_started - now).total_seconds() > 60 or (evidence.started - task_started).total_seconds() > 60):
        raise EvidenceError("任务F开始时间不属于本轮或位于未来")
    result = {
        "status": "passed", "runId": run_id, "total": None, "completed": 0, "persisted": None,
        "remaining": None, "statusCounts": {}, "requestedWorkers": task_f.get("requestedWorkers"),
        "effectiveWorkers": task_f.get("effectiveWorkers"),
        "independentSessionCount": task_f.get("uniqueAspNetSessionCount"),
        "sessionMode": task_f.get("workerSessionMode"), "completedBatchCount": 0, "batchCount": 0,
        "elapsedSeconds": max(0, int((now - task_started).total_seconds())) if task_started else None,
        "elapsedSource": elapsed_source,
        "phase": "not_started", "pipelineStatus": state.get("status"),
        "pipelineTerminal": state.get("status") in {"success", "completed", "failed", "error"},
        "processes": matched, "processInspectionPassed": not process_errors,
        "warnings": process_errors, "evidencePaths": evidence.paths,
        "readOnly": True, "networkAccess": False, "browserAccess": False, "databaseAccess": False,
        "newlyPersisted": None, "retainedInDatabase": None, "queriedNotPersisted": None,
        "incrementalWorkbook": task_f.get("incrementalWorkbook"),
        "newActivationImportedCount": task_f.get("newActivationImportedCount"),
        "notifyIncrementalWorkbook": task_f.get("notifyIncrementalWorkbook") is True,
    }
    current = None
    counts_times = []
    if progress:
        if progress.get("updatedAt") or progress.get("createdAt"):
            counts_times.append(timestamp(progress.get("updatedAt") or progress["createdAt"], "progress.updatedAt"))
        total = integer(progress.get("totalInputCount"), "progress.totalInputCount")
        base = integer(progress.get("completedCount"), "progress.completedCount")
        statuses = parse_counts(progress)
        if sum(statuses.values()) != base:
            raise EvidenceError("总进度完成数与状态计数不一致")
        completed_batches = integer(progress.get("completedBatchCount"), "progress.completedBatchCount", 0)
        batch_count = integer(progress.get("batchCount"), "progress.batchCount", 0)
        batch_index = integer(progress.get("currentBatchIndex"), "progress.currentBatchIndex", completed_batches)
        if completed_batches > batch_count or batch_index > batch_count:
            raise EvidenceError("任务F批次索引超出总数")
        if progress.get("currentBatchProgress"):
            # Validate the path even when this batch is already aggregated.
            current_path = evidence.path(progress["currentBatchProgress"])
            if batch_index < 1 or current_path.name != f"grabandgo_task_f_progress_batch_{batch_index:03d}_{run_id}.json":
                raise EvidenceError("当前批次进度路径与批次索引不一致")
            current = evidence.read(current_path, optional=True)
        completed = base
        if current and batch_index > completed_batches:
            current_done = integer(current.get("completedCount"), "batch.completedCount")
            current_total = integer(current.get("totalInputCount"), "batch.totalInputCount")
            current_statuses = parse_counts(current)
            if current_done > current_total or current_done != sum(current_statuses.values()):
                raise EvidenceError("当前批次完成数与状态计数不一致")
            if progress.get("currentBatchInputCount") is not None and current_total != progress["currentBatchInputCount"]:
                raise EvidenceError("当前批次输入数与总进度不一致")
            completed += current_done
            for key, value in current_statuses.items():
                statuses[key] = statuses.get(key, 0) + value
            if current.get("updatedAt") or current.get("createdAt"):
                counts_times.append(timestamp(current.get("updatedAt") or current["createdAt"], "batch.updatedAt"))
        if completed > total:
            raise EvidenceError("合并进度超出本轮查询总数")
        result.update(total=total, completed=completed, remaining=total-completed,
                      statusCounts=statuses, completedBatchCount=completed_batches, batchCount=batch_count)
        worker_source = current or progress
        for out, key in (("requestedWorkers", "requestedWorkers"), ("effectiveWorkers", "effectiveWorkers"),
                         ("independentSessionCount", "uniqueAspNetSessionCount"), ("sessionMode", "workerSessionMode")):
            value = worker_source.get(key, progress.get(key, task_f.get(key)))
            if out != "sessionMode" and value is not None:
                value = integer(value, key)
            result[out] = value
    elif task_f.get("executed") is True:
        result["total"] = integer(task_f.get("pendingCount"), "taskF.pendingCount", 0)
        result["statusCounts"] = parse_counts(task_f, "lookupStatusCounts")
        result["completed"] = sum(result["statusCounts"].values())
        result["remaining"] = max(0, result["total"]-result["completed"])

    imports = []
    import_batch_numbers = set()
    for path in sorted(evidence.receipts.glob(f"opensms_task_f_database_import_batch_*_{run_id}.json")):
        match = re.fullmatch(rf"opensms_task_f_database_import_batch_(\d+)_({re.escape(run_id)})\.json", path.name)
        if not match:
            continue
        batch_number = int(match.group(1))
        if batch_number < 1 or batch_number in import_batch_numbers:
            raise EvidenceError("导入回执批次为0或重复")
        import_batch_numbers.add(batch_number)
        if progress and batch_number > result["batchCount"]:
            raise EvidenceError("导入回执批次超出本轮查询批次")
        imports.append(import_counts(evidence.read(path)))
    final_import_path = task_f.get("importReceipt") or f"opensms_task_f_database_import_{run_id}.json"
    final_import = evidence.read(final_import_path, optional=True)
    counts = import_counts(final_import) if final_import else tuple(sum(row[i] for row in imports) for i in range(4)) if imports else None
    if counts is not None:
        result["persisted"], result["newlyPersisted"], result["retainedInDatabase"] = counts[1:]
        result["persistedEvidence"] = "verified_import_receipts"
        if final_import and imports and any(counts[i] < sum(row[i] for row in imports) for i in range(4)):
            raise EvidenceError("最终导入回执少于已完成批次导入，证据不一致")
    elif task_f.get("status") in GOOD_STAGES and task_f.get("executed") is True:
        result["newlyPersisted"] = integer(task_f.get("terminalImportedCount"), "taskF.terminalImportedCount", 0)
        result["retainedInDatabase"] = integer(task_f.get("alreadyTerminalKeptCount"), "taskF.alreadyTerminalKeptCount", 0)
        result["persisted"] = result["newlyPersisted"] + result["retainedInDatabase"]
        result["persistedEvidence"] = "final_task_f_stage"
    elif progress:
        # No successful import receipt exists yet. This is a confirmed zero
        # persisted result for the run, not the lifetime database record count.
        result.update(persisted=0, newlyPersisted=0, retainedInDatabase=0)
        result["persistedEvidence"] = "no_import_receipt_yet"
    if result["persisted"] is not None:
        if result["persisted"] > result["completed"]:
            raise EvidenceError("入库数大于当前查询完成数，快照可能正在更新，请稍后只读重试")
        result["queriedNotPersisted"] = result["completed"] - result["persisted"]

    if task_f.get("status") == "skipped" and task_f.get("executed") is False:
        if progress or imports or final_import:
            raise EvidenceError("任务F跳过声明与查询或导入证据矛盾")
        result.update(phase="skipped", reason=task_f.get("reason"), elapsedSeconds=0,
                      total=0, completed=0, remaining=0, persisted=0)
    elif task_f.get("status") in GOOD_STAGES and task_f.get("executed") is True:
        result["phase"] = "completed"
        if result["remaining"]:
            raise EvidenceError("任务F终态通过但仍有未查询数量")
        completed_at = timestamp(task_f.get("completedAt"), "taskF.completedAt")
        if (completed_at - now).total_seconds() > 60 or (evidence.started - completed_at).total_seconds() > 60 or (task_started and completed_at < task_started):
            raise EvidenceError("任务F完成时间不属于本轮或早于开始时间")
        result["elapsedSeconds"] = max(0, int((completed_at - task_started).total_seconds())) if task_started else None
        counts_times.append(completed_at)
    elif task_f.get("status") in FAILED_STAGES or state.get("status") in {"failed", "error"} or (progress or {}).get("status") in {"failed", "error", "login_required"}:
        result["phase"] = "failed"
    elif not matched and not process_errors and (progress or state.get("status") == "running"):
        result["phase"] = "process_missing"
    elif progress:
        result["phase"] = "importing" if result["remaining"] == 0 else "in_progress"
    elif task_started:
        result["phase"] = "preparing"
    if process_errors:
        result["status"] = "unknown"
    latest = max(counts_times) if counts_times else None
    result["countsAsOf"] = latest.isoformat() if latest else None
    result["evidenceAgeSeconds"] = max(0, int((now-latest).total_seconds())) if latest else None
    result["stale"] = latest is not None and result["evidenceAgeSeconds"] > STALE_SECONDS and result["phase"] not in {"completed", "failed"}
    if progress and latest is None:
        result["status"] = "unknown"
        result["warnings"].append("计数证据没有时间戳，不能确认新鲜度")
    if result["stale"]:
        result["status"] = "unknown"
        result["warnings"].append("进度证据超过15分钟未更新，计数仅代表最后快照")
    result["markdown"] = markdown(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="只读汇总指定运行的任务F进度，不执行任务")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = inspect_progress(args.state)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        result = {"status": "unknown", "phase": "failed", "error": str(exc),
                  "readOnly": True, "networkAccess": False, "browserAccess": False, "databaseAccess": False,
                  "markdown": f"任务F监控证据无法确认：{exc}。未启动或修改任何任务。"}
        print(json.dumps(result, ensure_ascii=False) if args.json else result["markdown"])
        return 2
    print(json.dumps(result, ensure_ascii=False) if args.json else result["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
