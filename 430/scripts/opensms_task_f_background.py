#!/usr/bin/env python3
"""Detached, receipt-bound Task F; never modifies a delivered main run.

All browser/network work remains in the reviewed Task F implementation. This
module only owns immutable input publication, process isolation and DB locking.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JOBS_ROOT = ROOT / "opensms_artifacts" / "task_f_jobs"
from opensms_bitbrowser_430 import PROFILE_ID, SESSION_POLICY
sys.path.insert(0, str(ROOT / 'services/opensms_document_api/pipeline'))
from report_kooda_source import is_report_source, validate_projection, verify_projection_workbook, ELIGIBILITY


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def publish_json(path: Path, payload: dict[str, Any]) -> None:
    """Exclusive publication on local or external filesystems; no hard links."""
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def database_writer_lock(database: Path):
    """One F DB-mutating workflow at a time, including catalog/export barriers.

    Frozen report-cache readers do not take this lock: SQLite read snapshots
    provide their own transaction consistency without waiting on network calls.
    """
    lock_path = database.with_name(database.name + ".task_f_writer.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _verified_document(runner, document, acquire) -> tuple[Path, dict]:
    path = Path(document["cardResult"]).resolve()
    proof = read_json(Path(document["cardReceipt"]))
    review = (proof.get("verification") or {}).get("system_kooda_source_review") or {}
    proved_output = Path(proof.get("output_path") or "")
    if (proof.get("result") != "成功" or not proof.get("checks")
            or any(value is not True for value in proof["checks"].values())
            or proof.get("failed_checks") != [] or review.get("matches") is not True
            or review.get("run_id") != runner.run_id
            or review.get("snapshot_sha256") != acquire.get("systemKoodaSha256")
            or not path.is_file() or not proved_output.is_file()
            or digest(path) != digest(proved_output)):
        raise RuntimeError("后台F输入不是本轮独立回读通过的文档")
    runner.verify_system_kooda_acquisition(acquire)
    return path, proof


def prepare_and_start(runner, document: dict, acquire: dict) -> dict:
    if runner.task_a_only:
        raise RuntimeError("任务A-only禁止后台任务F派发")
    document_path, proof = _verified_document(runner, document, acquire)
    red = (proof.get("verification") or {}).get("task_f_missing_activation_red_review") or {}
    if red.get("matches") is not True or type(red.get("expected_rows")) is not int:
        raise RuntimeError("后台F缺少本轮空激活日期独立回读计数")
    dates = (proof.get("verification") or {}).get("system_kooda_source_review") or {}
    missing_dates = dates.get("missing_either_date_rows")
    if type(missing_dates) is not int or missing_dates < 0:
        raise RuntimeError("后台F缺少解析后激活/截止日期任一缺失的独立计数")
    from opensms_task_f_db import verified_creation_fallbacks, document_backfill_rows
    inventory = read_json(Path(acquire['systemKoodaInventory']))
    report_source = is_report_source(inventory)
    export_scope = None
    if report_source:
        source_rows, export_scope = document_backfill_rows(inventory, document_path, digest(document_path), runner.run_id)
        cache = read_json(Path(runner.state['reportDateCache']['path']))
        known = {row['queryIccid'] for row in cache['records'] if row.get('activationDate')}
        # Frozen cache avoids even starting a Grab job for already-known dates.
        lookup_needed = any(row['queryIccid'] not in known for row in source_rows)
    fallback_rows = verified_creation_fallbacks(proof, runner.run_id, acquire["systemKoodaSha256"])
    fallback_count = 0
    if fallback_rows and not report_source:
        # Reuse the original proof, then match exact workbook rows. Chatr's
        # creation fallback must never start a Kooda query by itself.
        _, scope = document_backfill_rows(read_json(Path(acquire["systemKoodaInventory"])),
            document_path, digest(document_path), runner.run_id, creation_fallbacks=fallback_rows)
        fallback_count = scope["documentCreationFallbackRows"]
    ko_proof = None if report_source else (proof.get('process') or {}).get('ko_unactivated_sheet')
    ko_count = 0
    if ko_proof is not None:
        from opensms_task_f_ko_refresh import load_evidence
        cache = runner.state['reportDateCache']
        evidence, _ = load_evidence(proof, read_json(Path(acquire['systemKoodaInventory'])),
            acquire['systemKoodaSha256'], runner.run_id, Path(cache['path']), cache['sha256'])
        ko_count = len(evidence['rows'])
    if (not lookup_needed if report_source else missing_dates + fallback_count + ko_count == 0):
        return {"status": "skipped", "reason": "no_missing_authoritative_dates",
                **({'dateEligibilityMode': ELIGIBILITY, 'sourceScope': inventory['sourceScope'],
                    'documentPath': str(document_path), 'documentSha256': digest(document_path),
                    'documentRunId': runner.run_id} if report_source else {}),
                "executed": False, "background": False, "mainReportIndependent": True,
                "documentMissingActivationRows": red['expected_rows'] if report_source else 0,
                "documentMissingDateRows": missing_dates if report_source else 0,
                "documentMissingCutoffRows": dates.get('missing_cutoff_rows') if report_source else 0,
                "requestedWorkers": 8,
                "documentCreationFallbackRows": 0, "documentLookupNeededRows": 0,
                "documentKoRefreshRows": 0,
                "onlineLookupTriggered": False, "resultsApplyNextExport": True,
                "resultsWriteBackToDocument": False, "cloudUploadAttempted": False,
                "incrementalWorkbook": None, "newActivationImportedCount": 0,
                "notifyIncrementalWorkbook": False}
    directory = (JOBS_ROOT / runner.run_id).resolve()
    # A duplicate runId cannot overwrite its receipts or start another worker.
    directory.mkdir(parents=True, exist_ok=False)
    receipts = directory / "receipts"
    receipts.mkdir()
    from opensms_storage_policy import input_directory
    inputs = input_directory(directory, ROOT)
    bound = {}
    sources = {
        "document": document_path,
        "documentReceipt": Path(document["cardReceipt"]),
        "inventory": Path(acquire["systemKoodaInventory"]),
        "reportDateCache": Path(runner.state["reportDateCache"]["path"]),
    }
    inventory = read_json(sources["inventory"])
    if report_source:
        sources['rawExport'] = Path(inventory['sourceWorkbookPath'])
    if inventory.get("paginationEvidencePath"):
        # Keep the original inventory/receipt bytes intact and bind a private
        # copy of the sidecar so detached work survives source archival.
        sources["inventoryPaginationEvidence"] = Path(inventory["paginationEvidencePath"])
    for key, original in sources.items():
        destination = inputs / original.name
        shutil.copy2(original, destination)
        expected = (inventory.get("paginationEvidenceSha256")
                    if key == "inventoryPaginationEvidence" else digest(original))
        if digest(destination) != expected:
            raise RuntimeError(f"任务F独立输入复制校验失败:{key}")
        bound[key] = {"path": str(destination), "sha256": expected,
                      "sourcePath": str(original.resolve())}
    state_path = receipts / f"opensms_patrol_runner_state_{runner.run_id}.json"
    report_path = directory / f"OpenSMS_任务F增量补查_{runner.run_id}.md"
    job_path = directory / "job.json"
    job = {
        "schemaVersion": 1, "mode": "verified-document-detached-task-f",
        "runId": runner.run_id, "parentRunId": runner.run_id,
        "createdAt": datetime.now().astimezone().isoformat(),
        "mainDelivery": str(runner.delivery.resolve()),
        "mainReportIndependent": True, "requestedWorkers": 8,
        "resultsApplyNextExport": True, "resultsWriteBackToDocument": False,
        "cloudUploadAttempted": False, "silentBrowserMode": True,
        "backgroundDirectory": str(directory), "backgroundJob": str(job_path),
        "backgroundState": str(state_path), "backgroundReport": str(report_path),
        "documentPath": str(document_path), "documentSha256": digest(document_path),
        "documentRunId": runner.run_id, "sourceDocumentVerified": True,
        "reportDateCachePath": runner.state["reportDateCache"]["path"],
        "reportDateCacheSha256": runner.state["reportDateCache"]["sha256"],
        "inventory": str(Path(acquire["systemKoodaInventory"]).resolve()),
        "inventorySha256": acquire["systemKoodaSha256"],
        "documentMissingActivationRows": red["expected_rows"],
        "documentMissingDateRows": missing_dates,
        "documentCreationFallbackRows": fallback_count,
        "documentLookupNeededRows": export_scope['documentLookupNeededRows'] if report_source else missing_dates + fallback_count,
        "koRefreshEnabled": ko_proof is not None,
        "documentKoRefreshRows": ko_count,
        "totalLookupNeededRows": export_scope['documentLookupNeededRows'] if report_source else missing_dates + fallback_count + ko_count,
        "dateEligibilityMode": ELIGIBILITY if report_source else "missing_or_verified_creation_fallback",
        "documentMissingCutoffRows": dates.get("missing_cutoff_rows"),
        "inputs": bound,
        # Bind the exact profile, not the parent's OpenSMS/CloakBrowser port.
        # Resolve its current port only after local valid-date dedup leaves work.
        "sessionPolicy": SESSION_POLICY, "browserProfileId": PROFILE_ID,
        "useCurrent430Session": True, "cdpEndpoint": None,
    }
    publish_json(job_path, job)
    job_hash = digest(job_path)
    startup = {"schemaVersion": 2, "runId": runner.run_id,
               "createdAt": job["createdAt"], "updatedAt": job["createdAt"],
               "status": "queued", "backgroundTaskF": True,
               "backgroundJob": str(job_path), "backgroundJobSha256": job_hash,
               "stages": {"taskF": {"status": "queued", "executed": False,
                                     "requestedWorkers": 8}}, "timings": []}
    publish_json(state_path, startup)
    from opensms_patrol_runner import PYTHON
    log_path = directory / "background.log"
    child_env = runner.child_environment()
    # It may continue after main's API page is closed. Only isolated local HTTP
    # is allowed; no inherited page ID, no OpenSMS inventory fallback, no BCDE.
    for key in tuple(child_env):
        if key.startswith("OPENSMS_TASK_F_") or key.startswith("OPENSMS_FORCE_TASK_") or key in (
            "OPENSMS_RAW_API_TARGET_ID", "OPENSMS_ENABLE_TASK_D", "OPENSMS_EXECUTE_TASK_D",
            "OPENSMS_SITE_LOCK_TOKEN", "OPENSMS_SITE_LOCK_PID",
            "OPENSMS_SITE_JOB_LOCK_TOKEN", "OPENSMS_SITE_JOB_LOCK_PID", "OPENSMS_BROWSER_KIND",
            "OPENSMS_EXPECTED_BROWSER_WS_PATH",
            "OPENSMS_BROWSER_SESSION_ID", "OPENSMS_BROWSER_SERVICE_PID",
            "OPENSMS_ENABLE_TASK_E", "OPENSMS_EXECUTE_TASK_E",
            "OPENSMS_ENABLE_TASK_F", "OPENSMS_EXECUTE_TASK_F",
        ):
            child_env.pop(key, None)
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(PYTHON), str(Path(__file__).resolve()), "--job", str(job_path),
             "--job-sha256", job_hash], cwd=ROOT, env=child_env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    return {**{key: job[key] for key in (
        "backgroundJob", "backgroundState", "backgroundReport", "backgroundDirectory",
        "mainReportIndependent", "requestedWorkers", "resultsApplyNextExport",
        "resultsWriteBackToDocument", "cloudUploadAttempted", "documentPath",
        "documentSha256", "documentRunId", "inventory", "inventorySha256",
        "sourceDocumentVerified", "documentMissingActivationRows",
        "documentMissingDateRows", "documentMissingCutoffRows",
        "documentCreationFallbackRows", "documentLookupNeededRows", "dateEligibilityMode",
        "koRefreshEnabled", "documentKoRefreshRows", "totalLookupNeededRows",
        "reportDateCachePath", "reportDateCacheSha256",
    )}, "status": "running", "statusIsLaunchSnapshot": True,
        "background": True, "executed": True, "backgroundJobSha256": job_hash,
        "backgroundPid": process.pid, "backgroundLog": str(log_path),
        "documentGeneratedBeforeLookup": True, "postDocumentSupplemental": True,
        "reportBackfillMode": True, "automaticCatalogSync": True,
        "newActivationImportedCount": None, "incrementalWorkbook": None,
        "notifyIncrementalWorkbook": False}


def validate_job(job_path: Path, expected_sha256: str) -> dict:
    if not job_path.is_absolute() or not job_path.is_file() or digest(job_path) != expected_sha256:
        raise RuntimeError("任务F独立任务清单SHA256核验失败")
    job = read_json(job_path)
    directory = Path(job["backgroundDirectory"]).resolve()
    main = Path(job["mainDelivery"]).resolve()
    if (job.get("schemaVersion") != 1 or job.get("mode") != "verified-document-detached-task-f"
            or directory != job_path.parent.resolve()
            or directory == main or main in directory.parents
            or job.get("requestedWorkers") != 8 or job.get("mainReportIndependent") is not True
            or job.get("resultsWriteBackToDocument") is not False
            or job.get("cloudUploadAttempted") is not False
            or job.get("resultsApplyNextExport") is not True
            or job.get("parentRunId") != job.get("runId")):
        raise RuntimeError("任务F独立任务范围或不回填门禁错误")
    for field in ("backgroundState", "backgroundReport"):
        if directory not in Path(job[field]).resolve().parents:
            raise RuntimeError("任务F输出不得逃逸独立任务目录")
    if job.get("sessionPolicy") is not None and (
            job["sessionPolicy"] != SESSION_POLICY or job.get("browserProfileId") != PROFILE_ID
            or job.get("useCurrent430Session") is not True or job.get("cdpEndpoint") is not None):
        raise RuntimeError("任务F430固定会话策略或浏览器归属不符")
    from opensms_storage_policy import verified_input_root
    input_directory_owner = directory
    if job.get("retrySourceJob"):
        source_path = Path(job["retrySourceJob"])
        if (source_path != (JOBS_ROOT / job["runId"] / "job.json").resolve()
                or directory.parent != source_path.parent / "attempts"
                or not re.fullmatch(r"\d{8}T\d{6}\+0800", directory.name)
                or job.get("retryAuthorizationSource") != "current-user"
                or job.get("useCurrent430Session") is not True):
            raise RuntimeError("任务F人工重试范围或授权不符")
        source = validate_job(source_path, job["retrySourceJobSha256"])
        if job.get('koRefreshEnabled') != source.get('koRefreshEnabled'):
            raise RuntimeError('任务F重试不得改变ko表补查范围')
        if source.get("retrySourceJob") or any(job[key] != source[key] for key in (
                "runId", "parentRunId", "mainDelivery", "inputs", "documentSha256",
                "inventorySha256", "reportDateCacheSha256", "cdpEndpoint")):
            raise RuntimeError("任务F重试不得改变原文档、库存或会话端点绑定")
        if any(job.get(key) != source.get(key) for key in ("sessionPolicy", "browserProfileId")):
            raise RuntimeError("任务F重试不得改变原会话策略")
        input_directory_owner = source_path.parent
    input_root = verified_input_root(input_directory_owner, ROOT)
    for item in job["inputs"].values():
        path = Path(item["path"]).resolve()
        if path.parent != input_root or not path.is_file() or digest(path) != item["sha256"]:
            raise RuntimeError("任务F独立输入身份或哈希不一致")
    if (job["inputs"]["document"]["sha256"] != job["documentSha256"]
            or job["inputs"]["inventory"]["sha256"] != job["inventorySha256"]
            or job["inputs"]["reportDateCache"]["sha256"] != job["reportDateCacheSha256"]):
        raise RuntimeError("任务F独立输入绑定不一致")
    inventory = read_json(Path(job['inputs']['inventory']['path']))
    if is_report_source(inventory):
        raw = job['inputs'].get('rawExport') or {}
        if (raw.get('sha256') != inventory['sourceWorkbookSha256']
                or raw.get('sourcePath') != inventory['sourceWorkbookPath']
                or job.get('dateEligibilityMode') != ELIGIBILITY or job.get('koRefreshEnabled') is not False):
            raise RuntimeError('任务F10日表范围或原始源绑定不一致')
        verify_projection_workbook(inventory, Path(raw['path']), job['runId'])
    return job


def run_job(job_path: Path, expected_sha256: str) -> dict:
    job = validate_job(job_path, expected_sha256)
    directory = Path(job["backgroundDirectory"])
    state_path = Path(job["backgroundState"])
    # Nonblocking job lock plus terminal-state refusal prevents double workers
    # even if a second caller explicitly invokes this exact job file.
    with (directory / "job.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_json(state_path)
        if previous.get("status") != "queued":
            raise RuntimeError("任务F已启动或有终态，拒绝重入或隐式重试")
        import opensms_patrol_runner as patrol
        worker = patrol.PatrolRunner.__new__(patrol.PatrolRunner)
        worker.args = argparse.Namespace(task_a_only=False, task_f_workers=8,
                                        task_f_sample_size=0, cdp=job["cdpEndpoint"])
        worker.authorization = {"taskB": False, "taskC": False, "taskD": False,
                                "taskE": False, "taskF": False}
        worker.run_id = job["runId"]
        worker.day = worker.run_id[:8]
        from opensms_storage_policy import output_directory
        worker.delivery = output_directory(directory, ROOT)
        worker.receipts = state_path.parent
        worker.run_dir = directory / "logs"
        worker.run_dir.mkdir(exist_ok=True)
        worker.state_path = state_path
        worker.api_target_id = None
        worker.started_monotonic = time.monotonic()
        worker.document_process = None
        worker.document_log_handle = None
        worker.failure_result = None
        worker.state = {**previous, "status": "running", "runnerPid": os.getpid(),
            "taskFKoRefreshEnabled": job.get('koRefreshEnabled') is True,
            "backgroundVerifiedInputs": job["inputs"],
            "taskFStartedAt": patrol.now_hkt().isoformat(),
            "mainReportIndependent": True, "delivery": str(directory),
            "artifactDelivery": str(worker.delivery),
            "stages": {
                "acquire": {"systemKoodaInventory": job["inputs"]["inventory"]["path"],
                            "systemKoodaSha256": job["inventorySha256"]},
                "document": {"completedAt": job["createdAt"],
                             "cardResult": job["inputs"]["document"]["path"],
                             "cardReceipt": job["inputs"]["documentReceipt"]["path"]},
                "taskF": {"status": "waiting_database_lock", "requestedWorkers": 8},
            }, "writeGroups": {}, "timings": []}
        if job.get("retrySourceJob"):
            worker.state.update(taskFUseCurrent430Session=True,
                retryAuthorizationSource=job["retryAuthorizationSource"],
                retrySourceJobSha256=job["retrySourceJobSha256"],
                retrySourceJob=job["retrySourceJob"], attemptId=directory.name)
        if job.get("sessionPolicy") == SESSION_POLICY:
            worker.state.update(taskFUseCurrent430Session=True,
                taskFSessionPolicy=SESSION_POLICY, taskFBrowserProfileId=PROFILE_ID,
                taskFSessionJobSha256=expected_sha256)
        worker._save_state()
        try:
            result = worker.run_task_f()
            worker.state["status"] = "success"
            worker.state["summary"] = result
        except Exception as exc:
            result = dict(worker.state.get("stages", {}).get("taskF") or {})
            result.update({"status": "failed", "error": str(exc)})
            worker.state["status"] = "failed"
            worker.state["error"] = str(exc)
            worker.state["stages"]["taskF"] = result
        lines = ["# 任务F独立后台补查", "", f"运行编号：{worker.run_id}",
                 f"状态：{worker.state['status']}", "",
                 "主报表由主流水线独立生成及交付；本任务不会修改该报表或主交付清单。",
                 "本轮新增日期只进入本地库及独立增量文件，下轮新鲜报表才补入。", "",
                 f"已查：{sum((result.get('lookupStatusCounts') or {}).values())}",
                 f"新激活日期入库：{result.get('newActivationImportedCount', 0)}",
                 f"本地最新库：{result.get('latestIccidLibrary') or '未生成'}",
                 f"进度状态：{state_path}"]
        if result.get("error"):
            lines.extend(["", f"异常：{result['error']}"])
        if result.get("notifyIncrementalWorkbook") is True and result.get("incrementalWorkbook"):
            lines.extend(["", f"[任务F新增激活日期Excel]({result['incrementalWorkbook']})"])
        report_text = "\n".join(lines) + "\n"
        report_path = Path(job["backgroundReport"])
        try:
            with report_path.open("x", encoding="utf-8") as report:
                report.write(report_text)
                report.flush()
                os.fsync(report.fileno())
            if report_path.read_text(encoding="utf-8") != report_text:
                raise RuntimeError("任务F后台报告独立回读不一致")
            worker.state["backgroundReport"] = str(report_path)
            worker.state["backgroundReportSha256"] = digest(report_path)
            worker.state["reportVerificationPassed"] = True
        except Exception as exc:
            worker.state["status"] = "failed"
            worker.state["error"] = f"任务F报告发布失败:{exc}"
            result.update({"status": "failed", "error": worker.state["error"]})
            worker.state["stages"]["taskF"] = result
        # Publish the terminal pipeline state only after the F report readback.
        worker._save_state()
        return {"status": worker.state["status"], "runId": worker.run_id,
                "backgroundState": str(state_path), "backgroundReport": job["backgroundReport"],
                "taskF": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--job-sha256", required=True)
    parser.add_argument("--retry-current-session", action="store_true")
    parser.add_argument("--authorization-source", choices=("current-user",))
    args = parser.parse_args()
    if args.retry_current_session:
        try:
            if args.authorization_source != "current-user":
                raise RuntimeError("任务F重试必须获得当前用户授权")
            result = retry_current_session(args.job, args.job_sha256)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error": str(exc), "workerSpawned": False}, ensure_ascii=False))
            return 2
    try:
        result = run_job(args.job, args.job_sha256)
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
        record_startup_failure(args.job, args.job_sha256, str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 2


def record_startup_failure(job_path: Path, expected_sha256: str, error: str) -> None:
    """Record failures before the worker initializes, without trusting JSON paths.

    Never mutate a running/terminal job or the owner of a contended lock. Only a
    queued state already bound to this CLI hash under the fixed job root is ours.
    """
    directory = job_path.resolve().parent
    run_id = directory.name
    if directory.parent.name == "attempts" and directory.parent.parent.parent == JOBS_ROOT.resolve():
        run_id = directory.parent.parent.name
    elif directory.parent != JOBS_ROOT.resolve():
        return
    if (job_path.name != "job.json"
            or not re.fullmatch(r"\d{8}T\d{6}[+-]\d{4}", run_id)):
        return
    state_path = directory / "receipts" / f"opensms_patrol_runner_state_{run_id}.json"
    try:
        with (directory / "job.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = read_json(state_path)
            if (state.get("status") != "queued" or state.get("runId") != run_id
                    or state.get("backgroundTaskF") is not True
                    or state.get("backgroundJobSha256") != expected_sha256
                    or Path(state.get("backgroundJob") or "").resolve() != job_path.resolve()):
                return
            receipt = directory / "startup_failure.json"
            payload = {"status": "failed", "runId": run_id, "error": error,
                       "createdAt": datetime.now().astimezone().isoformat(),
                       "workerSpawned": False, "mainReportIndependent": True}
            publish_json(receipt, payload)
            state.update(status="failed", error=error, updatedAt=payload["createdAt"],
                         startupFailureReceipt=str(receipt))
            state.setdefault("stages", {})["taskF"] = {
                "status": "failed", "executed": False, "error": error,
                "requestedWorkers": 8, "mainReportIndependent": True}
            from opensms_patrol_runner import atomic_json
            atomic_json(state_path, state)
    except (OSError, ValueError, KeyError):
        # stderr/background.log is still the immutable dispatch-linked fallback.
        return


def retry_current_session(source_path: Path, source_sha256: str) -> dict:
    """Explicit retry of a pre-query failure, without rewriting any old receipt."""
    source = validate_job(source_path, source_sha256)
    directory = source_path.parent
    if source.get("retrySourceJob") or directory != (JOBS_ROOT / source["runId"]).resolve():
        raise RuntimeError("必须指定原始F作业，不递归重试其它重试清单")
    from opensms_storage_policy import config, guard
    settings = config(ROOT)
    if settings:
        guard(settings)
    with (directory / "retry_dispatch.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_json(Path(source["backgroundState"]))
        if previous.get("status") != "failed":
            raise RuntimeError("原F作业未确认失败，禁止重复派发")
        from opensms_task_f_progress_status import inspect_progress
        progress = inspect_progress(Path(source["backgroundState"]))
        if progress.get("completed") != 0 or progress.get("processes"):
            raise RuntimeError("此重试入口仅处理原轮查询前失败；已有查询或活动进程时停止")
        if list((directory / "receipts").glob("grabandgo_task_f_lookup*.json")):
            raise RuntimeError("原轮已有查询回执，禁止通过查询前重试入口重放")
        attempts = directory / "attempts"
        attempts.mkdir(exist_ok=True)
        for attempt in attempts.iterdir():
            if not attempt.is_dir():
                continue
            old_state = attempt / "receipts" / Path(source["backgroundState"]).name
            if not old_state.is_file() or read_json(old_state).get("status") not in ("failed", "success"):
                raise RuntimeError("已有未终态人工重试，不重复派发")
        from opensms_patrol_runner import PYTHON, now_hkt
        attempt_id = now_hkt().strftime("%Y%m%dT%H%M%S%z")
        attempt = attempts / attempt_id
        attempt.mkdir()
        receipts = attempt / "receipts"
        receipts.mkdir()
        state_path = receipts / Path(source["backgroundState"]).name
        job_path = attempt / "job.json"
        job = {**source, "createdAt": now_hkt().isoformat(),
            "backgroundDirectory": str(attempt), "backgroundJob": str(job_path),
            "backgroundState": str(state_path),
            "backgroundReport": str(attempt / f"OpenSMS_任务F补查重试_{attempt_id}.md"),
            "retrySourceJob": str(source_path), "retrySourceJobSha256": source_sha256,
            "retryAuthorizationSource": "current-user", "useCurrent430Session": True,
            "attemptId": attempt_id}
        publish_json(job_path, job)
        job_hash = digest(job_path)
        validate_job(job_path, job_hash)
        publish_json(state_path, {"schemaVersion": 2, "runId": source["runId"],
            "createdAt": job["createdAt"], "updatedAt": job["createdAt"],
            "status": "queued", "backgroundTaskF": True,
            "backgroundJob": str(job_path), "backgroundJobSha256": job_hash,
            "attemptId": attempt_id, "stages": {"taskF": {"status": "queued",
                "executed": False, "requestedWorkers": 8}}, "timings": []})
        child_env = {key: value for key, value in os.environ.items()
                     if not key.startswith("OPENSMS_")}
        log_path = attempt / "background.log"
        try:
            with log_path.open("x") as log:
                process = subprocess.Popen([str(PYTHON), str(Path(__file__).resolve()),
                    "--job", str(job_path), "--job-sha256", job_hash], cwd=ROOT,
                    env=child_env, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except Exception as exc:
            record_startup_failure(job_path, job_hash, str(exc))
            raise
        return {"status": "running", "statusIsLaunchSnapshot": True,
            "event": "task_f_retry_started", "runId": source["runId"],
            "attemptId": attempt_id, "backgroundState": str(state_path),
            "backgroundJob": str(job_path), "backgroundJobSha256": job_hash,
            "backgroundPid": process.pid, "backgroundLog": str(log_path),
            "requestedWorkers": 8, "mainReportIndependent": True,
            "cloudUploadAttempted": False, "resultsApplyNextExport": True}


if __name__ == "__main__":
    sys.exit(main())
