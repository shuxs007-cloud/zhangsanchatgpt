#!/usr/bin/env python3
"""One-command, receipt-gated OpenSMS 10-day patrol orchestrator.

The policy markdown remains the only complete business-rule source.  This
program only sequences the already-reviewed export, candidate, write,
readback, document, refresh and audit components with fail-closed gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from opensms_site_execution import SiteExecutionLease, REQUEST_LOCK, REPORT_LOCK
from opensms_browser_session import resolve_configured_browser, ensure_configured_browser, show_pending_manual_login
from opensms_acquire_schedule import read_submission_ready
from opensms_data_anomalies import anomaly_summary
sys.path.insert(0, str(ROOT / 'services/opensms_document_api/pipeline'))
from report_kooda_source import is_report_source, validate_projection, SCOPE as REPORT_KOODA_SCOPE
POLICY = SCRIPTS / "OPENSMS_10DAY_PATROL_POLICY.md"
ARTIFACTS = ROOT / "opensms_artifacts"
SNAPSHOTS = ARTIFACTS / "snapshots"
RUN_LOG_ROOT = ARTIFACTS / "logs" / "patrol_runner"
STATE_ROOT = ARTIFACTS / "state"
C2_STATE_ROOT = STATE_ROOT / "c2_daily"
TASK_E_LEDGER = STATE_ROOT / "task_e_submitted_phones.json"
TASK_F_DB = STATE_ROOT / "task_f_kooda_activation.sqlite3"
TASK_F_CATALOG_SOURCE = STATE_ROOT / "grabandgo_customer_sim_cards_authoritative.json"
TASK_F_LATEST_LIBRARY = STATE_ROOT / "task_f_kooda_iccid_library_latest.json"
PRIMARY_DELIVERY_ROOT = Path("/Volumes/zs01/chatgpt/OpenSMS")
FALLBACK_DELIVERY_ROOT = ARTIFACTS / "deliveries"
TZ = ZoneInfo("Asia/Hong_Kong")

from opensms_task_d_policy import removed_population_fields, population_statistics_removed

PIPELINE_VERSION = "2026.10.07-v68-b14-removed-local-cpu"
EXECUTION_MODE = "local-program-only"
PROGRAM_ENTRYPOINT = SCRIPTS / "run_opensms_patrol.sh"
STAGE_ORDER = ("document", "taskF", "standard", "taskB", "taskC", "taskD", "taskE", "expiredCardProbe", "taskG")
BALANCE_SUMMARY_GROUPS = ("A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V")
TASK_D_RETENTION_PARTITION_FIELDS = (
    "retentionMatchedCardCount", "retentionActionableCardCount", "filteredUnmatchedCount",
    "filteredUnmatchedCards", "neverProcessSourceExcludedCount", "neverProcessSourceExcludedCards",
    "chatrCandidateCount", "koodaCandidateCount", "retentionReceipt",
    "unconfiguredCarrierSkippedCount", "unconfiguredCarrierSkippedCards",
    "retiredCarrierSkippedCount", "retiredCarrierSkippedCards",
    "dataAnomalyPolicy", "dataAnomalySkippedCount", "dataAnomalySkippedCards",
)
TASK_BCDE_GATE_ENV_VARS = (
    "OPENSMS_FORCE_TASK_B",
    "OPENSMS_FORCE_TASK_C",
    "OPENSMS_FORCE_TASK_D",
    "OPENSMS_ENABLE_TASK_D",
    "OPENSMS_EXECUTE_TASK_D",
    "OPENSMS_FORCE_TASK_E",
    "OPENSMS_ENABLE_TASK_E",
    "OPENSMS_EXECUTE_TASK_E",
)
BITBROWSER_430_PROFILE_ID = "a53ce74e6c9e4fa08aec35f81ce1e2a3"
BITBROWSER_430_PORT_FILE = Path(
    "/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache"
) / BITBROWSER_430_PROFILE_ID / "DevToolsActivePort"

RUNTIME_ROOT = Path(
    "/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies"
)
PYTHON = RUNTIME_ROOT / "python/bin/python3"
NODE = RUNTIME_ROOT / "node/bin/node"
NODE_MODULES = RUNTIME_ROOT / "node/node_modules"
ARTIFACT_MARKER = Path(
    "/Users/zhangsan/.codex/plugins/cache/openai-primary-runtime/"
    "spreadsheets/26.904.11930/skills/spreadsheets/container_tools/"
    "mark_artifact_operation_started.mjs"
)

REQUIRED_FILES = (
    POLICY,
    SCRIPTS / "opensms_task_g.py",
    SCRIPTS / "opensms_task_g_settings.json",
    SCRIPTS / "opensms_task_g_engine.mjs",
    SCRIPTS / "opensms_execute_task_g.mjs",
    SCRIPTS / "opensms_task_e_unwritten_sheet.py",
    SCRIPTS / "opensms_task_e_unwritten_sheet.mjs",
    SCRIPTS / "opensms_v_group_guard.mjs",
    SCRIPTS / "opensms_v_swap_daily.py",
    SCRIPTS / "opensms_report_summary.py",
    SCRIPTS / "opensms_carrier_reply_comparison.py",
    SCRIPTS / "opensms_device_reply_periods.py",
    ROOT / "opensms_protocol_client.mjs",
    ROOT / "opensms_api_response.mjs",
    ROOT / "opensms_payload_readiness.mjs",
    ROOT / "opensms_cdp_export_once.mjs",
    SCRIPTS / "opensms_export_fast_raw.mjs",
    SCRIPTS / "opensms_export_submission_ready.mjs",
    SCRIPTS / "opensms_acquire_schedule.py",
    ROOT / "opensms_request_pacing.mjs",
    ROOT / "opensms_site_execution.mjs",
    SCRIPTS / "opensms_site_execution.py",
    SCRIPTS / "opensms_browser_session.py",
    SCRIPTS / "opensms_new_browser.mjs",
    SCRIPTS / "opensms_browser_auth_store.mjs",
    SCRIPTS / "opensms_auth_keychain.py",
    SCRIPTS / "grabandgo_auth_keychain.py",
    SCRIPTS / "grabandgo_login_policy.json",
    SCRIPTS / "opensms_expired_card_probe.mjs",
    SCRIPTS / "opensms_expired_card_probe_lib.mjs",
    SCRIPTS / "opensms_expired_card_probe_audit.py",
    SCRIPTS / "opensms_expired_card_probe_background.py",
    SCRIPTS / "opensms_execution_settings.json",
    Path("/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs"),
    ROOT / "opensms_bounded_status_fetch.mjs",
    SCRIPTS / "opensms_login_recovery_cli.mjs",
    SCRIPTS / "opensms_api_target_guard_cli.mjs",
    SCRIPTS / "ensure_opensms_captcha_service.sh",
    ROOT / "opensms_analyze_current.py",
    ROOT / "opensms_export_scan_cache.py",
    SCRIPTS / "opensms_build_report_kooda_source.py",
    ROOT / "services/opensms_document_api/pipeline/report_kooda_source.py",
    SCRIPTS / "opensms_inventory_coverage.py",
    ROOT / "services/opensms_document_api/pipeline/archived_system_identity.py",
    SCRIPTS / "opensms_download_google_sheet.mjs",
    SCRIPTS / "opensms_google_download_transport.mjs",
    SCRIPTS / "opensms_chatr_google_dates.py",
    ROOT / "opensms_generate_opr_zero_candidates.py",
    ROOT / "opensms_generate_e_reply_recovery.py",
    ROOT / "opensms_set_carrier_candidates.mjs",
    ROOT / "opensms_verify_carrier_candidates_readonly.mjs",
    SCRIPTS / "opensms_read_server_date.mjs",
    SCRIPTS / "opensms_read_server_dates_fast_raw.mjs",
    SCRIPTS / "opensms_resolve_active_carriers.mjs",
    SCRIPTS / "opensms_balance_readonly_raw.mjs",
    SCRIPTS / "opensms_process_document.py",
    SCRIPTS / "opensms_collect_card_retention_report.mjs",
    ROOT / "services/opensms_document_api/pipeline/card_retention_report.py",
    ROOT / "services/opensms_document_api/pipeline/ko_unactivated.py",
    SCRIPTS / "opensms_task_f_ko_refresh.py",
    SCRIPTS / "opensms_generate_a_zone_split.py",
    SCRIPTS / "opensms_generate_task_b_candidates.py",
    SCRIPTS / "opensms_generate_task_c_candidates.py",
    SCRIPTS / "opensms_task_c_daily_guard.py",
    SCRIPTS / "opensms_resolve_task_c_target.mjs",
    SCRIPTS / "opensms_generate_task_d_zero_metrics.py",
    SCRIPTS / "opensms_generate_quick_renew_c_restore.py",
    SCRIPTS / "opensms_prepare_task_d.mjs",
    SCRIPTS / "opensms_task_d_filtered_retention.mjs",
    SCRIPTS / "opensms_task_d_policy.py",
    SCRIPTS / "opensms_task_d_notification_summary.py",
    SCRIPTS / "opensms_card_retention_readonly_lib.mjs",
    SCRIPTS / "opensms_collect_task_e_card_retention.mjs",
    SCRIPTS / "opensms_execute_task_d_expiry.mjs",
    SCRIPTS / "opensms_generate_task_e_candidates.py",
    SCRIPTS / "opensms_execute_task_e_expiry.mjs",
    SCRIPTS / "opensms_expiry_receipt.mjs",
    SCRIPTS / "opensms_expiry_submission.mjs",
    SCRIPTS / "opensms_expiry_submission_settings.json",
    SCRIPTS / "opensms_collect_task_f_kooda_cards.mjs",
    SCRIPTS / "opensms_task_f_db.py",
    SCRIPTS / "opensms_export_task_f_deadlines.py",
    SCRIPTS / "grabandgo_task_f_lookup.mjs",
    SCRIPTS / "grabandgo_task_f_session_queue.mjs",
    SCRIPTS / "opensms_task_f_export_xlsx.mjs",
    SCRIPTS / "opensms_report_date_cache.py",
    SCRIPTS / "opensms_task_f_background.py",
    SCRIPTS / "opensms_task_f_delta.py",
    SCRIPTS / "opensms_finalize_patrol_report.py",
    SCRIPTS / "opensms_task_d_report_details.py",
    SCRIPTS / "opensms_make_delivery_manifest.py",
    SCRIPTS / "opensms_parallel_audit.py",
    SCRIPTS / "run_opensms_parallel_audit.sh",
    SCRIPTS / "opensms_collect_final_carrier_state.mjs",
    SCRIPTS / "opensms_refresh_workbook_carriers.mjs",
    SCRIPTS / "opensms_refresh_workbook_carriers_fast.py",
    SCRIPTS / "opensms_highlight_google_activated_missing.py",
    SCRIPTS / "opensms_patrol_profile.py",
    SCRIPTS / "opensms_chat_summary.py",
    SCRIPTS / "opensms_local_jobs.py",
    SCRIPTS / "opensms_diagnose_run.py",
    SCRIPTS / "opensms_readonly_concurrency.mjs",
    SCRIPTS / "opensms_carriers_readonly.mjs",
    SCRIPTS / "opensms_balance_statistics_readonly.mjs",
    ROOT / "services/opensms_document_api/pipeline/process_card_slots.py",
    ROOT / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py",
    ROOT / "services/opensms_document_api/pipeline/add_a11_low_reply_sheet.mjs",
    ROOT / "services/opensms_document_api/pipeline/build_card_slot_report.sh",
    SCRIPTS / "run_opensms_export.sh",
)

TITLE_BY_ROUTE = {
    "a1_kini_latest_lt4_to_b12": "A1的Kini最新日发送>=50且回复率<4%转B12",
    "a_zone_kooda_to_b12": "A区T系转B12",
    "b1_b11_b12_today_gte50_zero_to_e": "B1/B11/B12及V组零回复转E-T休息区",
    "b1_b11_use2_lt240_to_b11": "B1/B11的koo是否2天=2天且10日发送<240转B11",
    "b1_b11_gte240_to_b12": "B1/B11十日发送>=240转B12",
    "b1v_b11v_use2_lt240_to_b11v": "V组优先级2：B1V/B11Vkoo是否2天字段等于2天且10日发送<240转B11V",
    "b1v_b11v_gte240_to_b12v": "V组优先级3：B1V/B11V十日发送>=240转B12V",
    "chatr_to_e": "A1/A11/A12/A3的Chatr零回复转E-C休息区",
    "c2_previous_day_reply_rate_lt5_to_a11": "C2前一天回复率<5%转A11",
    "c2_rule_correction_restore_to_c2": "C2规则纠偏恢复到C2",
    "a_zone_lt4_to_a12": "A1/A11当天回复率<4%转A12",
    "a_zone_gte4_to_a1": "A11/A12当天回复率>=4%转A1",
    "a_zone_zero_to_e": "A区Chatr零回复转E-C休息区",
    "task_b_expired_to_offline_c": "任务B：Chatr已过期转过期下架卡C",
    "task_b_expired_to_offline_t": "任务B：Kooda/Koodo/Kini已过期转过期下架卡T",
    "task_b_tomorrow_fast_to_isolation_c": "任务B：Chatr明日到期转快过期隔离区C",
    "task_b_tomorrow_fast_to_isolation_t": "任务B：Kooda/Koodo/Kini明日到期转快过期隔离区T",
    "task_c_kooda_4day_reply_rate_lt5_to_swap": "任务C：Kooda/T系四日回复率<5%且10日发送>360转换号区T",
    "task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap": "任务C：Chatr未换号、四日回复率<4%且10日发送>360转换号区C",
    "task_d_retain_chatr_to_b14_c": "任务D：卡保留Chatr转B14-C快下保留卡",
    "task_d_retain_kooda_to_b14_t": "任务D：卡保留Kooda/Koodo/Kini转B14-T快下保留卡",
}


class PatrolError(RuntimeError):
    pass


def execution_contract(task_a_only: bool = False) -> dict[str, Any]:
    """Machine-readable contract consumed by the thin AI launcher/UI layer."""
    return {
        "executionMode": EXECUTION_MODE,
        "pipelineVersion": PIPELINE_VERSION,
        "programEntrypoint": str(PROGRAM_ENTRYPOINT),
        "manualAiStepsRequired": False,
        "aiRole": "read-policy-launch-once-summarize-final-json",
        "taskSelection": "A" if task_a_only else "default",
        "taskAOnly": task_a_only,
        "taskAOnlySupported": True,
        "googleAssociationEnabled": False,
        "googleDisabledReason": "user_disabled",
        "googleChatrDateFallbackEnabled": True,
        "googleChatrDateFallbackScope": "chatr_blank_activation_and_cutoff_only",
        "googleSystemMissingSheetEnabled": True,
        "googleSystemMissingSheetScope": "google_slots_absent_from_current_system_export",
        "koUnactivatedSheetEnabled": True,
        "systemKoodaSourceScope": REPORT_KOODA_SCOPE,
        "fullKoodaInventoryAcquisitionEnabled": False,
        "taskFDateEligibilityMode": "raw_10day_activation_missing_and_local_activation_missing",
        "chatrDocumentDateSource": "current_run_system_then_fresh_google",
        "unusedPortDeviceScope": "system_source_only",
        "cardRetentionReportEnabled": True,
        "cardRetentionReportScope": "task_a_readonly_all_pages",
        "cardRetentionRequestSelection": "latest_valid_createTime_per_phone",
        "taskGStandaloneEnabled": False,
        "taskGWeekendRestEnabledForRun": not task_a_only,
        "taskDRetentionSummaryContractVersion": 2,
        "taskDRetentionPopulationEnabled": False,
        "taskDRetentionPopulationStatus": "removed_by_user",
        "taskDRetentionPopulationReadExecuted": False,
        "stageDependencyMode": "frozen-dates-document-standard-final-readback" if task_a_only else "frozen-dates-document-detached-f-sequential-writes",
        "stageOrder": ["document", "standard", "expiredCardProbe"] if task_a_only else list(STAGE_ORDER),
        "expiredCardProbeEnabled": True,
        "expiredCardProbeScope": "expired_zones_all_listing_states_v2",
        "expiredCardProbeBackground": True,
        "expiredCardProbeDeadlineFilterEnabled": False,
        "balanceSummaryGroups": list(BALANCE_SUMMARY_GROUPS),
        "safeReadOnlyConcurrency": True,
        "liveWritesSerial": True,
        "automaticRecoveryPolicy": "v53-startup-and-allowlisted-reads-only",
        "automaticBusinessReplayEnabled": False,
        "taskFDefault": "on_post_document_catalog_incremental",
        "taskFEnabledForRun": not task_a_only,
        "taskFSkippedReason": "task_a_only" if task_a_only else None,
        "taskFRequestedWorkers": 0 if task_a_only else 8,
        "taskFResultsApply": "local_database_and_incremental_file_then_next_export_frozen_cache",
        "koodaDocumentDateSource": "current_run_system_then_pre_document_frozen_local_cache",
        "taskFBackfillScope": "current_document_missing_or_verified_creation_fallback",
        "taskFMainCriticalPathWait": False,
        "cloudDateUploadEnabled": False,
    }


def build_balance_summary_groups(balance: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all configured balance groups in stable UI order, including unknowns."""
    by_group = {
        str(record.get("group")): record
        for record in balance.get("records") or []
        if record.get("group")
    }
    result: list[dict[str, Any]] = []
    for group in BALANCE_SUMMARY_GROUPS:
        record = by_group.get(group) or {}
        daily_remaining = record.get("dailyRemaining")
        daily_total = record.get("dailyTotal")
        percent_remaining = record.get("percentRemaining")
        available = (
            daily_remaining is not None
            and daily_total is not None
            and percent_remaining is not None
            and float(daily_total) != 0
        )
        result.append({
            "group": group,
            "dailyRemaining": daily_remaining if available else None,
            "dailyTotal": daily_total if available else None,
            "percentRemaining": percent_remaining if available else None,
            "availability": "available" if available else "unknown",
        })
    return result


def resolve_bitbrowser_430_cdp(explicit: str | None = None) -> str:
    """Resolve only the designated BitBrowser 430 profile, never a neighbor."""
    if explicit:
        return explicit
    try:
        port = BITBROWSER_430_PORT_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        raise PatrolError(f"无法读取BitBrowser 430调试端口: {BITBROWSER_430_PORT_FILE}") from exc
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise PatrolError(f"BitBrowser 430调试端口无效: {port!r}")
    return f"http://127.0.0.1:{port}"


def now_hkt() -> datetime:
    return datetime.now(TZ)


def run_id_now() -> str:
    return now_hkt().strftime("%Y%m%dT%H%M%S+0800")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path | str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_last_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    objects: list[tuple[int, int, dict[str, Any]]] = []
    for match in re.finditer(r"\{", text):
        try:
            value, end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            absolute_end = match.start() + end
            # Prefer the outermost final JSON document.  Starting raw_decode at
            # a nested object also succeeds, so "last opening brace wins" can
            # silently return an inner receipt object without status fields.
            if not text[absolute_end:].strip():
                return value
            objects.append((match.start(), absolute_end, value))
    if not objects:
        raise PatrolError("命令输出中没有可解析的JSON对象")
    # Fallback for commands that append non-JSON diagnostics after the payload:
    # choose the latest-ending, widest object instead of the latest opening brace.
    return max(objects, key=lambda item: (item[1], item[1] - item[0]))[2]


def count_candidate(path: Path | str) -> int:
    payload = read_json(path)
    value = payload.get("count", payload.get("candidateCount"))
    if value is not None:
        if int(value) == 0 and any(payload.get(field) for field in ("rows", "candidates", "phones")):
            raise PatrolError(f"候选计数为0但记录非空: {path}")
        return int(value)
    return len(payload.get("rows") or payload.get("candidates") or [])


def target_from_candidate(path: Path | str) -> str:
    target = str(read_json(path).get("targetCarrierName") or "").strip()
    if not target:
        raise PatrolError(f"候选缺少目标运营商: {path}")
    return target


def ensure_xlsx(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size < 10_000:
        raise PatrolError(f"{label}不存在或大小异常: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
    except zipfile.BadZipFile as exc:
        raise PatrolError(f"{label}不是有效XLSX: {path}") from exc
    if bad:
        raise PatrolError(f"{label}压缩结构损坏: {bad}")


def validate_task_selection(args: argparse.Namespace) -> None:
    """Reject mixed task modes before any checkpoint, CDP or business access."""
    if getattr(args, 'skip_data_anomalies_this_run', False) and (
            not getattr(args, 'continue_task_d', False) or not getattr(args, 'resume_state', None)
            or getattr(args, 'authorization_source', None) != 'current-user'):
        raise PatrolError('TASK_D_PREPARATION_CONTINUATION_AUTHORIZATION_INVALID')
    if not getattr(args, "task_a_only", False):
        return
    conflicts = [
        "--" + name.replace("_", "-")
        for name in (
            "force_task_b", "force_task_c", "force_task_d", "force_task_e", "force_task_f",
            "task_e_recovery_progress", "task_f_only", "task_f_sample_size",
            "task_f_repair_lookup", "task_f_debug_iccid", "resume_state", "restore_unexpired_to_a11",
            "continue_task_d", "continue_task_g", "skip_data_anomalies_this_run",
        )
        if getattr(args, name, None)
    ]
    if getattr(args, "task_f_workers_supplied", False):
        conflicts.append("--task-f-workers")
    if getattr(args, "authorization_source", None) in ("current-user", "history"):
        conflicts.append("--authorization-source")
    if conflicts:
        raise PatrolError("--task-a-only 仅允许本轮新鲜任务A，不得混用: " + ", ".join(conflicts))


def validate_authorization(args: argparse.Namespace) -> dict[str, Any]:
    validate_task_selection(args)
    source = getattr(args, "authorization_source", None)
    if getattr(args, "restore_unexpired_to_a11", False):
        if source != "current-user" or os.environ.get("OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE") != "1":
            raise PatrolError("未到期恢复A11只接受当前用户明确授权及独立环境门禁")
        if any(getattr(args, name, None) for name in (
            "force_task_b", "force_task_c", "force_task_d", "force_task_e", "force_task_f",
            "task_a_only", "resume_state", "task_f_only", "task_f_sample_size",
            "task_f_repair_lookup", "task_f_debug_iccid", "task_e_recovery_progress",
        )):
            raise PatrolError("未到期恢复A11不得与其它强制任务或旧断点混用")
    result = {
        "taskB": bool(getattr(args, "force_task_b", False)),
        "taskC": bool(getattr(args, "force_task_c", False)),
        "taskD": bool(getattr(args, "force_task_d", False)),
        "taskE": bool(getattr(args, "force_task_e", False)),
        "taskF": bool(getattr(args, "force_task_f", False)),
        "authorizationSource": source,
    }
    for task, enabled, env_name in (
        ("B", result["taskB"], "OPENSMS_FORCE_TASK_B"),
        ("C", result["taskC"], "OPENSMS_FORCE_TASK_C"),
        ("D", result["taskD"], "OPENSMS_FORCE_TASK_D"),
        ("E", result["taskE"], "OPENSMS_FORCE_TASK_E"),
        ("F", result["taskF"], "OPENSMS_FORCE_TASK_F"),
    ):
        if not enabled:
            continue
        if source != "current-user":
            raise PatrolError(f"任务{task}只接受 --authorization-source current-user")
        if os.environ.get(env_name) != "1":
            raise PatrolError(f"任务{task}还要求环境门禁 {env_name}=1")
        if task == "F":
            for extra_gate in ("OPENSMS_ENABLE_TASK_F", "OPENSMS_EXECUTE_TASK_F"):
                if os.environ.get(extra_gate) != "1":
                    raise PatrolError(f"任务F还要求环境门禁 {extra_gate}=1")
    recovery_progress = getattr(args, "task_e_recovery_progress", None)
    if recovery_progress:
        if not result["taskE"] or source != "current-user":
            raise PatrolError("任务E不确定批次恢复只接受当前真实用户再次强制授权")
        if not Path(recovery_progress).is_file():
            raise PatrolError(f"任务E恢复断点不存在: {recovery_progress}")
    task_f_sample_size = int(getattr(args, "task_f_sample_size", 0) or 0)
    if task_f_sample_size:
        if not result["taskF"] or source != "current-user":
            raise PatrolError("任务F样例查询只接受当前真实用户强制授权")
        if any(result[name] for name in ("taskB", "taskC", "taskD", "taskE")):
            raise PatrolError("任务F样例查询不得与任务B、C、D、E同时执行")
    task_f_only = bool(getattr(args, "task_f_only", False))
    if task_f_only:
        if not result["taskF"] or source != "current-user":
            raise PatrolError("任务F独立全量查询只接受当前真实用户强制授权")
        if any(result[name] for name in ("taskB", "taskC", "taskD", "taskE")):
            raise PatrolError("任务F独立全量查询不得与任务B、C、D、E同时执行")
    task_f_repair_lookup = getattr(args, "task_f_repair_lookup", None)
    if task_f_repair_lookup:
        if not result["taskF"] or source != "current-user":
            raise PatrolError("任务F错误终态修复只接受当前真实用户强制授权")
        if not Path(task_f_repair_lookup).is_file():
            raise PatrolError(f"任务F待撤销查询回执不存在: {task_f_repair_lookup}")
    task_f_debug_iccid = str(getattr(args, "task_f_debug_iccid", "") or "").strip()
    if task_f_debug_iccid:
        if not result["taskF"] or source != "current-user":
            raise PatrolError("任务F单条诊断只接受当前真实用户强制授权")
        if any(result[name] for name in ("taskB", "taskC", "taskD", "taskE")):
            raise PatrolError("任务F单条诊断不得与任务B、C、D、E同时执行")
    return result


@dataclass
class CommandResult:
    payload: dict[str, Any]
    stdout: str
    stderr: str
    duration_seconds: float


class PatrolRunner:
    def __init__(self, args: argparse.Namespace, state: dict[str, Any] | None = None):
        self.args = args
        self.authorization = validate_authorization(args)
        if state and (self.task_a_only or state.get("taskAOnly") is True or state.get("taskSelection") == "A"):
            raise PatrolError("任务A-only必须使用本轮新鲜导出，禁止断点续跑")
        if state and (state.get("partialAcquisition") or any(
            name in (state.get("stages") or {}) for name in ("acquire", "document")
        )) and (state.get("googleAssociationEnabled") is not False
                or state.get("googleDisabledReason") != "user_disabled"
                or state.get("googleChatrDateFallbackEnabled") is not True):
            raise PatrolError("旧报表断点未证明Google依赖已关闭，请保留原回执并启动新轮次")
        self.args.cdp = resolve_bitbrowser_430_cdp(getattr(args, "cdp", None))
        if state and (state.get("authorization") or {}) != self.authorization:
            raise PatrolError("断点续跑必须使用与原轮次完全相同的任务B/任务C/任务D/任务E/任务F授权参数和环境门禁")
        self.run_id = (state or {}).get("runId") or run_id_now()
        self.day = self.run_id[:8]
        self.run_dir = RUN_LOG_ROOT / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.delivery = Path(state["delivery"]).resolve() if state and state.get("delivery") else self._delivery_dir(self.day, self.run_id)
        self.receipts = self.delivery / "receipts"
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.state_path = self.receipts / f"opensms_patrol_runner_state_{self.run_id}.json"
        self.state: dict[str, Any] = state or {
            "schemaVersion": 2,
            "runId": self.run_id,
            "createdAt": now_hkt().isoformat(),
            "status": "running",
            "policyPath": str(POLICY),
            "delivery": str(self.delivery),
            **execution_contract(self.task_a_only),
            "bitBrowserProfileId": None if getattr(args, "browser_session", None) else BITBROWSER_430_PROFILE_ID,
            "bitBrowserCdp": None if getattr(args, "browser_session", None) else self.args.cdp,
            "browser": {key: value for key, value in (getattr(args, "browser_session", None) or {
                "browser": "bitbrowser-430", "cdp": self.args.cdp}).items() if key != "owner"},
            "authorization": self.authorization,
            "stages": {},
            "writeGroups": {},
            "timings": [],
        }
        # Older checkpoints remain recoverable, but every new save and final
        # JSON advertises the current program-only execution contract.
        self.state.update(execution_contract(self.task_a_only))
        if self.task_a_only:
            # Record the exclusion before acquisition so early failures remain
            # distinguishable from an incomplete default Task F run.
            for task in "BCDEF":
                self.state["stages"]["task" + task] = self.task_a_excluded_stage()
        self.document_process: subprocess.Popen[str] | None = None
        self.document_log_handle = None
        self.document_started_monotonic: float | None = None
        self.failure_result: dict[str, Any] | None = None
        # Ownership is never inferred from an inherited environment or an old
        # checkpoint. A resumed process creates a fresh target for its own reads.
        self.api_target_id: str | None = None
        self.started_monotonic = time.monotonic()
        self.state["runnerPid"] = os.getpid()
        self._save_state()

    @property
    def chatr_google_date_fallback(self) -> bool:
        return getattr(self, "state", {}).get("googleChatrDateFallbackEnabled") is True

    @property
    def task_a_only(self) -> bool:
        return bool(getattr(getattr(self, "args", None), "task_a_only", False)
                    or getattr(self, "state", {}).get("taskAOnly") is True)

    @staticmethod
    def task_a_excluded_stage() -> dict[str, Any]:
        return {
            "executed": False, "status": "skipped", "reason": "task_a_only",
            "currentUserAuthorized": False, "automaticCatalogSync": False,
            "candidateGenerationExecuted": False, "inventoryRead": False,
            "catalogRead": False, "databaseRead": False, "databaseWritten": False,
            "loginChecked": False, "onlineLookupTriggered": False,
            "databaseExported": False, "sensitiveIccidSubmittedCount": 0,
        }

    @staticmethod
    def _delivery_dir(day: str, run_id: str) -> Path:
        root = PRIMARY_DELIVERY_ROOT if PRIMARY_DELIVERY_ROOT.is_dir() and os.access(PRIMARY_DELIVERY_ROOT, os.W_OK) else FALLBACK_DELIVERY_ROOT
        path = root / day / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def _save_state(self) -> None:
        self.state["updatedAt"] = now_hkt().isoformat()
        atomic_json(self.state_path, self.state)

    def complete_stage(self, name: str, data: dict[str, Any], started: float) -> dict[str, Any]:
        duration = round(time.monotonic() - started, 3)
        self.state["stages"][name] = {"completedAt": now_hkt().isoformat(), "durationSeconds": duration, **data}
        self.state["timings"].append({"stage": name, "durationSeconds": duration})
        self._save_state()
        return data

    def reusable_stage(self, name: str, required_paths: tuple[str, ...] = ()) -> dict[str, Any] | None:
        data = self.state.get("stages", {}).get(name)
        if not data:
            return None
        for key in required_paths:
            value = data.get(key)
            if not value or not Path(value).is_file():
                return None
        return data

    def command(
        self,
        label: str,
        argv: list[str | Path],
        *,
        env: dict[str, str] | None = None,
        allow_codes: tuple[int, ...] = (0,),
        cwd: Path = ROOT,
    ) -> CommandResult:
        command = [str(value) for value in argv]
        started = time.monotonic()
        command_started_at = now_hkt().isoformat()
        # Task B/C/D/E are opt-in per current user turn. Never leak a stale
        # parent-shell gate into subprocesses; an authorized stage must pass
        # every gate it needs explicitly through ``env`` below.
        child_env = self.child_environment(env)
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )
        duration = round(time.monotonic() - started, 3)
        # Measure actual child commands, including failures; parallel durations
        # overlap and must not be summed as end-to-end wall time.
        self.state.setdefault("commandTimings", []).append({
            "command": label, "startedAt": command_started_at,
            "durationSeconds": duration, "exitCode": completed.returncode,
        })
        log_path = self.run_dir / f"{label}.log"
        log_path.write_text(
            "$ " + " ".join(command) + "\n\nSTDOUT\n" + completed.stdout + "\nSTDERR\n" + completed.stderr,
            encoding="utf-8",
        )
        recovery_events = []
        for line in completed.stderr.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            for kind in ("backgroundStartupRecovery", "readRequest"):
                if isinstance(event.get(kind), dict):
                    safe = {key: event[kind][key] for key in (
                        "attempt", "status", "category", "command", "targetId",
                        "cleanupConfirmed", "elapsedMs", "durationMs", "retry", "waitMs",
                        "method", "apiPath", "pageNum", "pageSize", "success",
                    ) if key in event[kind]}
                    recovery_events.append({"kind": kind, **safe})
        if recovery_events:
            self.state.setdefault("automaticRecovery", []).append({
                "stage": label, "log": str(log_path), "events": recovery_events,
            })
        if completed.returncode not in allow_codes:
            if label == "task_a_f_date_import":
                # Pre-receipt gates (input/replay/policy) can fail before the
                # importer writes JSON. Preserve only its safe symbolic code,
                # never arbitrary stderr, so the outer handler cannot hide it.
                for line in reversed(completed.stderr.splitlines()):
                    if re.fullmatch(r"F_IMPORT_[A-Z_]+", line.strip()):
                        raise PatrolError(line.strip())
            if label == "google_chatr_download":
                try:
                    failure = parse_last_json(completed.stdout)
                except (PatrolError, ValueError, TypeError):
                    failure = None
                descriptions = {"read_timeout": "新鲜下载请求超时", "invalid_xlsx": "下载响应不是有效XLSX",
                                "http_error": "下载收到HTTP错误", "transport_error": "下载网络传输失败"}
                if (isinstance(failure, dict) and failure.get("status") == "failed"
                        and failure.get("runId") == self.run_id
                        and failure.get("code") == "OPENSMS_GOOGLE_DOWNLOAD_FAILED"
                        and type(failure.get("businessWrites")) is int and failure["businessWrites"] == 0
                        and isinstance(failure.get("category"), str) and failure["category"] in descriptions
                        and type(failure.get("attemptCount")) is int and 0 <= failure["attemptCount"] <= 3):
                    raise PatrolError(f"Chatr Google源{descriptions[failure['category']]}；"
                                      f"已尝试{failure['attemptCount']}次，未复用旧文件；日志: {log_path}")
            if label == "chatr_google_date_snapshot":
                try:
                    failure = parse_last_json(completed.stdout)
                except (PatrolError, ValueError, TypeError):
                    failure = None
                if (isinstance(failure, dict) and failure.get("status") == "failed"
                        and failure.get("runId") == self.run_id
                        and isinstance(failure.get("error"), str) and failure["error"].strip()):
                    reason = " ".join(failure["error"].split())[:1000]
                    raise PatrolError(f"Chatr日期Google源校验失败：{reason}；未进入文档或写入；日志: {log_path}")
            if label == "system_kooda_inventory":
                for line in completed.stderr.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    failure = event.get("inventoryFailure") if isinstance(event, dict) else None
                    request_descriptions = {"read_timeout": "库存请求超时",
                                            "browser_runtime_timeout": "浏览器执行通道超时",
                                            "network_fetch_failed": "库存请求网络失败"}
                    if (isinstance(failure, dict) and failure.get("schemaVersion") == 1
                            and failure.get("runId") == self.run_id
                            and type(failure.get("businessWrites")) is int and failure["businessWrites"] == 0
                            and failure.get("code") == "request-failed"
                            and isinstance(failure.get("requestCategory"), str)
                            and failure["requestCategory"] in request_descriptions
                            and type(failure.get("pageNum")) is int and failure["pageNum"] > 0
                            and type(failure.get("paginationAttemptCount")) is int
                            and 1 <= failure["paginationAttemptCount"] <= 3):
                        raise PatrolError(f"系统Kooda第{failure['pageNum']}页"
                                          f"{request_descriptions[failure['requestCategory']]}；"
                                          f"已尝试{failure['paginationAttemptCount']}次，未采用不完整库存，"
                                          f"不能据此判定登录失效；日志: {log_path}")
                    if (not isinstance(failure, dict) or failure.get("schemaVersion") != 1
                            or failure.get("runId") != self.run_id
                            or type(failure.get("businessWrites")) is not int or failure["businessWrites"] != 0
                            or failure.get("code") != "total-drift"
                            or any(type(failure.get(key)) is not int or failure[key] < 0
                                   for key in ("expectedTotal", "receivedTotal", "paginationAttemptCount"))):
                        continue
                    raise PatrolError(
                        f"系统Kooda库存读取停止：分页期间库存总数由{failure['expectedTotal']}变为"
                        f"{failure['receivedTotal']}，{failure['paginationAttemptCount']}次有限尝试后仍不一致；"
                        f"未采用不完整库存，不能据此判定403或登录失效；日志: {log_path}")
            if label == "task_d_prepare" or label.startswith("task_d_prepare_resume_"):
                descriptions = {
                    "carrier_unconfigured": "后台明确未配置运营商，需配置或明确授权排除，不能自动跳过",
                    "carrier_identity_missing": "后台运营商标识缺失或无效，不能当作未命中",
                    "backend_id_missing": "后台号码记录ID缺失或无效",
                    "outside_requested": "后台查询返回了请求范围以外的号码",
                }
                for line in completed.stderr.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    failure = event.get("taskDPreparationFailure") if isinstance(event, dict) else None
                    if (not isinstance(failure, dict) or failure.get("version") != 1
                            or failure.get("runId") != self.run_id or failure.get("task") != "D"
                            or failure.get("stage") != "task_d_prepare"
                            or failure.get("code") != "OPENSMS_TASK_D_ROW_INVALID"
                            or not isinstance(failure.get("category"), str)
                            or failure["category"] not in descriptions
                            or failure.get("notificationSubmitted") is not False
                            or type(failure.get("businessWrites")) is not int
                            or failure["businessWrites"] != 0
                            or not all(isinstance(failure.get(key), str) for key in ("cardNo", "backendId", "slot"))):
                        continue
                    self.state["taskDPreparationFailure"] = {
                        key: failure[key] for key in (
                            "version", "runId", "task", "stage", "code", "category", "cardNo",
                            "backendId", "slot", "businessWrites", "notificationSubmitted",
                        )
                    }
                    self.state["taskDPreparationFailure"]["log"] = str(log_path)
                    raise PatrolError(f"任务D准备停止：{descriptions[failure['category']]}；"
                                      f"未提交通知；明细见日志: {log_path}")
            download_error = None
            if label == "export":
                progress_path = SNAPSHOTS / f"opensms_export_progress_{self.run_id}.json"
                try:
                    if progress_path.is_file():
                        progress = read_json(progress_path)
                        if isinstance(progress, dict) and progress.get("runId") == self.run_id:
                            target = self.receipts / progress_path.name
                            shutil.copy2(progress_path, target)
                            diagnostic = progress.get("downloadFailure")
                            diagnostic = diagnostic if isinstance(diagnostic, dict) else None
                            generation = progress.get("generationFailure")
                            generation = generation if isinstance(generation, dict) else None
                            self.state["exportFailure"] = {
                                "receipt": str(target), "status": progress.get("status"),
                                "downloadFailure": diagnostic,
                                "generationFailure": generation,
                            }
                            if diagnostic:
                                transport_descriptions = {"read_timeout": "下载等待达到上限",
                                    "browser_runtime_timeout": "下载浏览器执行通道超时",
                                    "download_transport_error": "下载请求传输失败"}
                                if diagnostic.get("category") in transport_descriptions:
                                    completion = "已确认请求结束" if diagnostic.get("completionConfirmed") is True else "请求结束未确认，禁止重试"
                                    download_error = (f"export下载失败：{transport_descriptions[diagnostic['category']]}；"
                                                      f"{completion}；回执: {target}")
                                else:
                                    download_error = (
                                        f"export下载失败：HTTP {diagnostic.get('httpStatus')}，"
                                        f"业务码{diagnostic.get('businessCode')}，"
                                        f"类型{diagnostic.get('category')}，"
                                        f"响应{diagnostic.get('byteLength')}字节；回执: {target}"
                                    )
                            elif generation:
                                category = generation.get("category")
                                descriptions = {
                                    "browser_runtime_timeout": "浏览器执行通道超时（不等同服务器限流）",
                                    "read_timeout": "生成状态读取超时",
                                    "network_fetch_failed": "生成状态请求网络失败",
                                    "server_generation_failed": "后台明确返回生成失败",
                                    "generation_timeout": "生成等待达到上限",
                                    "status_query_error": "生成状态查询异常",
                                    "http_429": "生成状态查询被服务器限流（HTTP 429）",
                                    "http_500": "生成状态查询收到服务器HTTP 500",
                                    "http_502": "生成状态查询收到网关HTTP 502",
                                    "http_503": "生成状态查询收到服务器HTTP 503",
                                    "http_504": "生成状态查询收到网关HTTP 504",
                                }
                                description = descriptions.get(category, "生成状态查询失败")
                                download_error = (
                                    f"export失败：{description}；导出已提交，禁止重复生成；回执: {target}"
                                )
                except (OSError, ValueError, TypeError) as receipt_error:
                    # A damaged/missing diagnostic must never hide the original child failure.
                    self.state["exportFailure"] = {
                        "receipt": None, "status": "diagnostic_capture_failed",
                        "captureErrorType": type(receipt_error).__name__,
                    }
            if download_error:
                raise PatrolError(download_error)
            raise PatrolError(f"{label}失败，退出码{completed.returncode}；日志: {log_path}")
        payload = parse_last_json(completed.stdout)
        return CommandResult(payload, completed.stdout, completed.stderr, duration)

    def automatic_recovery_report(self) -> Path:
        receipt = self.receipts / f"opensms_automatic_recovery_{self.run_id}.json"
        report = self.receipts / f"OpenSMS_自动恢复_{self.run_id}.md"
        events = self.state.get("automaticRecovery") or []
        receipt.write_text(json.dumps({"runId": self.run_id, "policy": "v53",
            "businessReplayEnabled": False, "stages": events}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["# 本轮自动恢复记录", "", f"运行编号：{self.run_id}", "",
                 "仅隐藏页面启动和白名单只读请求允许有限重试；不重放业务提交、不复用旧源。", "",
                 "| 阶段 | 类型 | 尝试 | 结果 | 重试 |", "| --- | --- | ---: | --- | --- |"]
        for stage in events:
            for event in stage["events"]:
                outcome = event.get("status") or ("success" if event.get("success") else event.get("category", "failed"))
                lines.append(f"| {stage['stage']} | {event['kind']} | {event.get('attempt')} | {outcome} | {event.get('retry', False)} |")
        if not events:
            lines.extend(["", "本轮没有已记录的自动恢复事件；不代表所有阶段已完成。"])
        lines.extend(["", f"[完整脱敏回执](<{receipt}>)"])
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report

    def child_environment(self, env: dict[str, str] | None = None) -> dict[str, str]:
        child_env = dict(os.environ)
        child_env.pop("OPENSMS_SITE_LOCK_TOKEN", None)
        child_env.pop("OPENSMS_SITE_LOCK_PID", None)
        child_env.pop("OPENSMS_SITE_JOB_LOCK_TOKEN", None)
        child_env.pop("OPENSMS_SITE_JOB_LOCK_PID", None)
        child_env.pop("OPENSMS_BROWSER_KIND", None)
        child_env.pop("OPENSMS_EXPECTED_BROWSER_WS_PATH", None)
        child_env.pop("OPENSMS_BROWSER_SESSION_ID", None)
        child_env.pop("OPENSMS_BROWSER_SERVICE_PID", None)
        child_env.pop("OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED", None)
        child_env.pop("OPENSMS_KO_UNACTIVATED_REQUIRED", None)
        child_env.pop("OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE", None)
        child_env.pop("OPENSMS_SYSTEM_KOODA_STABILIZE_BEFORE_READ", None)
        child_env.pop("OPENSMS_F_DATE_IMPORT_RUN_ID", None)
        child_env.pop("OPENSMS_EXPIRED_PROBE_RUN_ID", None)
        child_env.pop("OPENSMS_TASK_G_RUN_ID", None)
        child_env.pop("OPENSMS_TASK_G_INPUT_SHA256", None)
        child_env.pop("OPENSMS_EXPORT_SUBMISSION_READY_PATH", None)
        child_env.pop("OPENSMS_EXPORT_SUBMISSION_READY_TOKEN", None)
        for gate_name in TASK_BCDE_GATE_ENV_VARS:
            child_env.pop(gate_name, None)
        for field in ("OPENSMS_SYSTEM_KOODA_PATH", "OPENSMS_SYSTEM_KOODA_SHA256", "OPENSMS_SYSTEM_KOODA_SNAPSHOT", "OPENSMS_REPORT_DATE_CACHE_PATH", "OPENSMS_REPORT_DATE_CACHE_SHA256", "OPENSMS_CHATR_GOOGLE_DATES_PATH", "OPENSMS_CHATR_GOOGLE_DATES_SHA256", "OPENSMS_RUN_ID", "OPENSMS_TASK_G_AUTHORIZATION", "OPENSMS_CARD_RETENTION_REPORT_PATH", "OPENSMS_CARD_RETENTION_REPORT_SHA256", "OPENSMS_CARD_RETENTION_REPORT_REQUIRED", "OPENSMS_CARD_RETENTION_REPORT_RUN_ID"):
            child_env.pop(field, None)
        child_env.update(env or {})
        child_env.update(getattr(self, "site_execution_environment", {}))
        if getattr(self.args, "browser_session", None):
            child_env["BITBROWSER_CDP"] = self.args.cdp
            child_env["OPENSMS_BROWSER_KIND"] = "cloakbrowser"
            child_env["OPENSMS_EXPECTED_BROWSER_WS_PATH"] = self.args.browser_session["webSocketPath"]
            child_env["OPENSMS_BROWSER_SESSION_ID"] = self.args.browser_session["sessionId"]
            child_env["OPENSMS_BROWSER_SERVICE_PID"] = str(self.args.browser_session["pid"])
            child_env["OPENSMS_EXACT_CDP_ONLY"] = "1"
            child_env["OPENSMS_FORCE_RAW_CDP"] = "1"
        if self.task_a_only:
            for field in tuple(child_env):
                if field in TASK_BCDE_GATE_ENV_VARS or field in (
                    "OPENSMS_FORCE_TASK_F", "OPENSMS_ENABLE_TASK_F", "OPENSMS_EXECUTE_TASK_F",
                ) or field.startswith("OPENSMS_TASK_F_"):
                    child_env.pop(field, None)
        child_env["OPENSMS_RUN_ID"] = self.run_id
        # Ordinary report runs cannot inherit or override the retired input.
        child_env["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"] = "0"
        child_env.pop("OPENSMS_RAW_API_TARGET_ID", None)
        if getattr(self, "api_target_id", None):
            child_env["OPENSMS_RAW_API_TARGET_ID"] = self.api_target_id
            child_env["BITBROWSER_CDP"] = self.args.cdp
        return child_env

    def create_api_target(self) -> None:
        if getattr(self, "api_target_id", None):
            return
        receipt = self.receipts / f"opensms_api_target_{self.run_id}.json"
        if receipt.exists():
            receipt = self.receipts / f"opensms_api_target_{self.run_id}_{time.time_ns()}.json"
        prior = self.state.get("apiTarget")
        if prior:
            self.state.setdefault("apiTargetHistory", []).append(dict(prior))
        # Save the exact ownership receipt location before invoking create, so a
        # create failure after target allocation can still be safely cleaned up.
        self.state["apiTarget"] = {
            "runId": self.run_id, "receipt": str(receipt), "status": "creating",
            "endpoint": self.args.cdp,
        }
        self._save_state()
        created = self.command(
            "api_target_create",
            [NODE, SCRIPTS / "opensms_api_target_guard_cli.mjs", "create",
             "--run-id", self.run_id, "--receipt", receipt],
            env={"BITBROWSER_CDP": self.args.cdp, "OPENSMS_EXACT_CDP_ONLY": "1"},
        ).payload
        if (not created.get("success") or not created.get("targetId")
                or created.get("receipt") != str(receipt)
                or created.get("endpoint") != self.args.cdp):
            raise PatrolError("本轮独立后台API页创建或身份回执核验失败")
        self.api_target_id = str(created["targetId"])
        self.state["apiTarget"].update({**created, "status": "open", "runId": self.run_id})
        self._save_state()

    def close_api_target(self) -> None:
        owned = self.state.get("apiTarget") or {}
        if (owned.get("runId") != self.run_id or not owned.get("receipt")
                or owned.get("status") in ("closed", "cleanup_failed")):
            return
        # A failed create without a receipt cannot prove ownership. The guard
        # verifies the receipt before it closes any target; never guess an ID.
        try:
            if not Path(owned["receipt"]).is_file():
                if owned.get("status") == "creating":
                    return
                raise PatrolError("本轮后台API页归属回执缺失，无法安全关闭")
            closed = self.command(
                "api_target_close",
                [NODE, SCRIPTS / "opensms_api_target_guard_cli.mjs", "close",
                 "--run-id", self.run_id, "--receipt", owned["receipt"]],
                env={"BITBROWSER_CDP": self.args.cdp, "OPENSMS_EXACT_CDP_ONLY": "1"},
            ).payload
            if not closed.get("success") or not (closed.get("closed") or closed.get("alreadyClosed")):
                raise PatrolError("本轮后台API页关闭未取得确认回执")
            self.api_target_id = None
            owned.update({"status": "closed", "cleanup": closed, "closedAt": now_hkt().isoformat()})
            self._save_state()
        except Exception as exc:
            owned.update({"status": "cleanup_failed", "cleanupError": str(exc)})
            self._save_state()
            raise

    def record_partial_acquisition(self, **facts: Any) -> None:
        """Persist independently verified facts, never a completed acquire stage."""
        partial = self.state.setdefault("partialAcquisition", {"runId": self.run_id, "status": "partial"})
        partial.update(facts)
        partial["verifiedAt"] = now_hkt().isoformat()
        self._save_state()

    def copy_artifact(self, source: Path | str, name: str | None = None) -> Path:
        src = Path(source).expanduser().resolve()
        if not src.is_file():
            raise PatrolError(f"待归档文件不存在: {src}")
        target = self.receipts / (name or src.name)
        if src != target:
            shutil.copy2(src, target)
        return target

    def task_f_lookup_batch(self, index: int, pending: Path, archive: Path,
                            progress: Path, env: dict[str, str]) -> dict[str, Any]:
        # The exclusive Node publisher needs hard links. Keep its receipt on the
        # local filesystem; external delivery volumes may not support fs.link.
        local = self.run_dir / "task_f_receipts" / archive.name
        if os.path.lexists(local) or os.path.lexists(archive):
            raise PatrolError(f"任务F批次回执已存在，禁止重复查询或覆盖: {local} / {archive}")
        local.parent.mkdir(parents=True, exist_ok=True)
        self.command(
            f"task_f_grabandgo_lookup_batch_{index:03d}",
            [NODE, SCRIPTS / "grabandgo_task_f_lookup.mjs", pending, local,
             str(self.args.task_f_workers), progress],
            env=env, allow_codes=(0, 4),
        )
        payload = read_json(local)
        digest = sha256(local)
        # Preserve the local original even if delivery or verification fails.
        # Exclusive creation also refuses a target created during the query.
        with local.open("rb") as source, archive.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        if sha256(archive) != digest or read_json(archive) != payload:
            raise PatrolError("任务F本机查询回执归档独立回读不一致，禁止导入数据库")
        self.state.setdefault("taskFReceiptStaging", []).append({
            "batchIndex": index, "localReceipt": str(local),
            "archiveReceipt": str(archive), "sha256": digest,
            "archiveReadbackPassed": True,
        })
        self._save_state()
        return payload

    def acquire(self) -> dict[str, Any]:
        reused = self.reusable_stage(
            "acquire",
            (
                "sourceArchive", "processingSource",
                "scanCache", "scanCacheReceipt", "exportReceipt", "loginReceipt",
                "phoneDateReceipt", "dayDateReceipt", "activeCarrierReceipt", "balance",
                "systemKoodaInventory", "processingSystemKooda",
                "cardRetentionReceipt", "processingCardRetention", "cardRetentionPhoneList",
            ),
        )
        if reused and (reused.get("googleAssociationEnabled") is not False
                       or reused.get("googleDisabledReason") != "user_disabled"
                       or (self.chatr_google_date_fallback and reused.get("googleChatrDateFallbackEnabled") is not True)
                       or any(reused.get(key) for key in (
                           "association", "processingAssociation", "googleReceipt", "googleSha256",
                       ))):
            raise PatrolError("旧采集断点包含Google依赖，禁止复用，请启动新轮次")
        started = time.monotonic()
        # A fresh attempt must not surface facts from a previous incomplete
        # acquisition, even when reusing the same run checkpoint.
        if not reused:
            self.state["partialAcquisition"] = {
                "runId": self.run_id, "status": "partial",
                "googleAssociationEnabled": False, "googleDisabledReason": "user_disabled",
                "google": {"enabled": False, "status": "skipped", "reason": "user_disabled"},
                "association": None, "processingAssociation": None,
                "googleReceipt": None, "googleSha256": None,
                "googleChatrDateFallbackEnabled": self.chatr_google_date_fallback,
            }
            self._save_state()
        working_input_dir = ROOT / "outputs" / f"opensms_patrol_{self.run_id}" / "inputs"
        working_input_dir.mkdir(parents=True, exist_ok=True)
        # Normal acquisition uses exact 430 raw CDP background targets.  They
        # preserve the two-route date proof without paying Playwright's ~120s
        # browser-wide handshake.  Independent system API reads can
        # overlap the export; the combined date read stays after the export so
        # all three business-date values describe the fresh completed snapshot.
        api_env = {
            "BITBROWSER_CDP": self.args.cdp,
            "OPENSMS_FORCE_RAW_CDP": "1",
            "OPENSMS_EXACT_CDP_ONLY": "1",
        }
        # Manual login: verify the existing session without starting local OCR.
        login = self.command(
            "login_preflight",
            [NODE, SCRIPTS / "opensms_login_recovery_cli.mjs"],
            env=api_env,
        )
        if not login.payload.get("success"):
            raise PatrolError("OpenSMS后台静默登录预检或恢复未通过")
        login_receipt = self.copy_artifact(
            login.payload["path"],
            f"opensms_login_recovery_{self.run_id}.json",
        )
        self.record_partial_acquisition(loginReceipt=str(login_receipt))
        self.create_api_target()
        if reused:
            self.verify_system_kooda_acquisition(reused)
            self.verify_card_retention_acquisition(reused)
            if self.chatr_google_date_fallback:
                self.verify_chatr_google_acquisition(reused)
            return reused
        jobs: dict[str, Future[CommandResult]] = {}
        results: dict[str, CommandResult] = {}
        errors: list[tuple[str, Exception]] = []
        ready_token = secrets.token_hex(16)
        ready_path = self.run_dir / f"export_submission_ready_{ready_token}.json"
        submission_ready = None
        gate_failed = False
        background_reads_started = False
        scheduling = {"mode": "export-submit-first-then-overlap-generation",
                      "deferredSystemReads": ["active", "balance", "card_retention_report"],
                      "koodaSourceScope": REPORT_KOODA_SCOPE,
                      "submissionReadyPath": str(ready_path), "httpInFlightLimit": 1,
                      "requestPacingUnchanged": True}

        def collect(name: str, confirm: Any) -> None:
            if name not in jobs:
                return
            try:
                result = jobs[name].result()
                results[name] = result
                confirm(result.payload)
            except Exception as exc:
                # All already-dispatched independent reads are joined and their
                # validated facts saved, while every failure remains fatal.
                errors.append((name, exc))
                self.record_partial_acquisition(
                    failures=[{"stage": stage, "error": str(error)} for stage, error in errors],
                )
                print(json.dumps({
                    "event": "acquisition_subtask_failed", "runId": self.run_id,
                    "stage": name, "observedAt": now_hkt().isoformat(),
                    "error": str(exc), "action": "drain_started_reads_preserve_verified_source",
                    "state": str(self.state_path),
                }, ensure_ascii=False), file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=max(1, min(self.args.io_workers, 4)), thread_name_prefix="opensms-acquire") as pool:
            jobs["export"] = pool.submit(
                self.command, "export", [NODE, SCRIPTS / "opensms_export_fast_raw.mjs"],
                env={**api_env, "OPENSMS_EXPORT_SUBMISSION_READY_PATH": str(ready_path),
                     "OPENSMS_EXPORT_SUBMISSION_READY_TOKEN": ready_token},
            )
            system_inventory = self.receipts / f"opensms_10day_kooda_source_{self.run_id}.json"
            if self.chatr_google_date_fallback:
                jobs["google_chatr_download"] = pool.submit(
                    self.command, "google_chatr_download", [NODE, SCRIPTS / "opensms_download_google_sheet.mjs"],
                )
            retention_path = self.receipts / f"opensms_card_retention_report_{self.run_id}.json"
            def dispatch_background_reads() -> None:
                jobs["active"] = pool.submit(
                    self.command, "active_carriers", [NODE, SCRIPTS / "opensms_resolve_active_carriers.mjs"], env=api_env)
                jobs["balance"] = pool.submit(
                    self.command, "balance", [NODE, SCRIPTS / "opensms_balance_readonly_raw.mjs"], env=api_env)
                jobs["card_retention_report"] = pool.submit(
                    self.command, "card_retention_report",
                    [NODE, SCRIPTS / "opensms_collect_card_retention_report.mjs", self.run_id, retention_path],
                    env={**api_env, "OPENSMS_CARD_RETENTION_REPORT_RUN_ID": self.run_id})

            def release_background_reads() -> None:
                nonlocal submission_ready, background_reads_started, gate_failed
                if background_reads_started or gate_failed:
                    return
                try:
                    submission_ready = read_submission_ready(ready_path, self.run_id, ready_token)
                    if submission_ready is not None and not errors:
                        dispatch_background_reads()
                        background_reads_started = True
                        scheduling.update(submissionReadySeconds=round(time.monotonic() - started, 3),
                                          taskId=submission_ready["taskId"],
                                          submissionReadySha256=sha256(ready_path))
                        self.record_partial_acquisition(acquisitionScheduling=scheduling)
                except Exception as exc:
                    gate_failed = True
                    errors.append(("export_submission_gate", PatrolError(str(exc))))
                    self.record_partial_acquisition(
                        failures=[{"stage": stage, "error": str(error)} for stage, error in errors])

            def confirm_card_retention(payload: dict[str, Any]) -> None:
                if payload.get("status") != "passed" or payload.get("runId") != self.run_id or payload.get("receipt") != str(retention_path):
                    raise PatrolError("任务A卡保留采集回执不符")
                digest = sha256(retention_path)
                facts = {"cardRetentionReceipt": str(retention_path), "cardRetentionSha256": digest}
                evidence = self.verify_card_retention_acquisition(facts)
                local = working_input_dir / retention_path.name
                shutil.copy2(retention_path, local)
                if sha256(local) != digest:
                    raise PatrolError("本轮卡保留源本机副本SHA256不一致")
                phone_list = self.receipts / f"opensms_card_retention_phones_{self.run_id}.txt"
                with phone_list.open("x", encoding="utf-8") as stream:
                    stream.write("".join(phone + "\n" for phone in evidence["phones"]))
                if phone_list.read_text(encoding="utf-8").splitlines() != evidence["phones"]:
                    raise PatrolError("卡保留号码TXT独立回读不一致")
                self.record_partial_acquisition(**facts, processingCardRetention=str(local),
                    cardRetentionPhoneList=str(phone_list), cardRetentionPhoneListSha256=sha256(phone_list),
                    cardRetentionRecords=len(evidence["rows"]), cardRetentionUniquePhones=len(evidence["phones"]))

            def confirm_chatr_google_download(payload: dict[str, Any]) -> None:
                original = Path(payload.get("outputPath") or "")
                ensure_xlsx(original, "本轮Chatr日期Google源")
                began = datetime.fromisoformat(str(payload.get("startedAt") or "").replace("Z", "+00:00"))
                downloaded = datetime.fromisoformat(str(payload.get("downloadedAt") or "").replace("Z", "+00:00"))
                run_began = datetime.strptime(self.run_id[:15], "%Y%m%dT%H%M%S").replace(tzinfo=TZ)
                if (payload.get("runId") != self.run_id or not run_began <= began <= downloaded <= now_hkt()
                        or sha256(original) != payload.get("sha256")
                        or payload.get("sourceUrl") != "https://docs.google.com/spreadsheets/d/1GiA9uR_2IQ4xntP_aWvCSaWfmC6WVQ7SrnXsy8YyyvM/export?format=xlsx"):
                    raise PatrolError("本轮Chatr Google日期源的身份、新鲜度或SHA256不一致")
                archive = self.delivery / f"OpenSMS_Chatr日期Google源_{self.run_id}.xlsx"
                shutil.copy2(original, archive)
                receipt = self.copy_artifact(payload["receiptPath"], f"opensms_chatr_google_download_{self.run_id}.json")
                if read_json(receipt) != payload or sha256(archive) != payload["sha256"]:
                    raise PatrolError("本轮Chatr Google源归档独立核验失败")
                local = working_input_dir / archive.name
                shutil.copy2(archive, local)
                if sha256(local) != payload["sha256"]:
                    raise PatrolError("本轮Chatr Google源本机副本校验失败")
                self.record_partial_acquisition(
                    chatrGoogleSource=str(archive), chatrGoogleSourceSha256=payload["sha256"],
                    chatrGoogleDownloadReceipt=str(receipt), chatrGoogleDownloadReceiptSha256=sha256(receipt),
                    processingChatrGoogleSource=str(local),
                )
                snapshot = self.receipts / f"opensms_chatr_google_dates_{self.run_id}.json"
                jobs["chatr_google_date_snapshot"] = pool.submit(
                    self.command, "chatr_google_date_snapshot",
                    [PYTHON, SCRIPTS / "opensms_chatr_google_dates.py", "--source", local,
                     "--source-sha256", payload["sha256"], "--run-id", self.run_id, "--output", snapshot],
                )

            def confirm_chatr_google_snapshot(payload: dict[str, Any]) -> None:
                path = self.receipts / f"opensms_chatr_google_dates_{self.run_id}.json"
                snapshot = read_json(path)
                if (snapshot.get("status") != "passed" or snapshot.get("runId") != self.run_id
                        or snapshot.get("sourceSha256") != self.state["partialAcquisition"]["chatrGoogleSourceSha256"]
                        or snapshot.get("source") != "google_chatr_dates"):
                    raise PatrolError("本轮Chatr Google日期快照身份核验失败")
                local = working_input_dir / path.name
                shutil.copy2(path, local)
                if sha256(local) != sha256(path):
                    raise PatrolError("本轮Chatr Google日期快照本机副本校验失败")
                self.record_partial_acquisition(chatrGoogleDates=str(path), chatrGoogleDatesSha256=sha256(path),
                    processingChatrGoogleDates=str(local), chatrGoogleDateRecordCount=snapshot.get("recordCount"))

            def confirm_export(payload: dict[str, Any]) -> None:
                if payload.get("taskStatus") != "SUCCESS":
                    raise PatrolError("后台新鲜导出任务未返回SUCCESS")
                if (submission_ready is None or payload.get("taskId") != submission_ready["taskId"]
                        or payload.get("serverDate") != submission_ready["serverDate"]):
                    raise PatrolError("新鲜导出与本轮提交调度回执不一致")
                export_source = Path((payload.get("saved") or {}).get("path") or "")
                ensure_xlsx(export_source, "本轮新鲜导出")
                source_archive = self.delivery / f"OpenSMS近10日新鲜导出_{self.run_id}.xlsx"
                shutil.copy2(export_source, source_archive)
                ensure_xlsx(source_archive, "本轮归档源表")
                self.record_partial_acquisition(
                    sourceArchive=str(source_archive), sourceSha256=sha256(source_archive),
                    sourceVerified=True, taskId=payload.get("taskId"),
                    taskDurationSeconds=round(float(payload.get("elapsedMs") or 0) / 1000, 3),
                    exportPerformance=payload.get("timingsMs") or {},
                )
                export_receipt = self.copy_artifact(payload["receiptPath"], f"opensms_export_once_{self.run_id}.json")
                processing_source = working_input_dir / f"export4day_{self.run_id}.xlsx"
                shutil.copy2(export_source, processing_source)
                ensure_xlsx(processing_source, "本轮本机处理源表")
                self.record_partial_acquisition(
                    processingSource=str(processing_source), exportReceipt=str(export_receipt),
                )
                scan_cache_path = working_input_dir / f"export4day_{self.run_id}.scan.pkl"
                scan_cache_receipt_local = working_input_dir / f"export4day_{self.run_id}.scan.json"
                jobs["scan_cache"] = pool.submit(
                    self.command, "scan_cache",
                    [PYTHON, ROOT / "opensms_export_scan_cache.py", processing_source,
                     "--output", scan_cache_path, "--receipt", scan_cache_receipt_local],
                )
                jobs["server_dates"] = pool.submit(
                    self.command, "server_dates",
                    [NODE, SCRIPTS / "opensms_read_server_dates_fast_raw.mjs"], env=api_env,
                )

            def confirm_server_dates(payload: dict[str, Any]) -> None:
                dates = {
                    str(results["export"].payload.get("serverDate") or ""),
                    str((payload.get("day") or {}).get("serverDate") or ""),
                    str((payload.get("phoneConfig") or {}).get("serverDate") or ""),
                }
                if len(dates) != 1 or "" in dates:
                    raise PatrolError(f"导出、/day、/phoneConfig服务器日期不一致: {sorted(dates)}")
                day_date_receipt = self.copy_artifact(
                    payload["day"]["path"], f"opensms_server_date_day_{self.run_id}.json",
                )
                phone_date_receipt = self.copy_artifact(
                    payload["phoneConfig"]["path"], f"opensms_server_date_phoneConfig_{self.run_id}.json",
                )
                self.record_partial_acquisition(
                    serverDate=dates.pop(), serverDateVerified=True,
                    dayDateReceipt=str(day_date_receipt), phoneDateReceipt=str(phone_date_receipt),
                )

            def confirm_active(payload: dict[str, Any]) -> None:
                if not payload.get("success"):
                    raise PatrolError("后台实时运营商名称未全部唯一解析")
                receipt = self.copy_artifact(payload["path"], f"opensms_resolved_active_carriers_{self.run_id}.json")
                self.record_partial_acquisition(activeCarrierReceipt=str(receipt))

            def confirm_scan_cache(payload: dict[str, Any]) -> None:
                if payload.get("status") != "passed" or not payload.get("hashBound"):
                    raise PatrolError("本轮源表单次扫描缓存生成或SHA-256绑定核验失败")
                receipt = self.copy_artifact(payload["receipt"], f"opensms_export_scan_cache_{self.run_id}.json")
                self.record_partial_acquisition(
                    scanCache=str(working_input_dir / f"export4day_{self.run_id}.scan.pkl"),
                    scanCacheReceipt=str(receipt), scanCacheHashBound=True,
                )
                # F scope comes from this fresh export, not paginated inventory.
                # Use the existing immutable scan rather than reopen the XLSX.
                partial = self.state["partialAcquisition"]
                jobs["system_kooda"] = pool.submit(
                    self.command, "report_kooda_source",
                    [PYTHON, SCRIPTS / "opensms_build_report_kooda_source.py",
                     "--scan-cache", partial["scanCache"], "--source", partial["processingSource"],
                     "--run-id", self.run_id, "--output", system_inventory])

            def confirm_system_kooda(payload: dict[str, Any]) -> None:
                digest = sha256(system_inventory)
                facts = {"systemKoodaInventory": str(system_inventory), "systemKoodaSha256": digest}
                verified_inventory = self.verify_system_kooda_acquisition(facts)
                local_inventory = working_input_dir / system_inventory.name
                shutil.copy2(system_inventory, local_inventory)
                if sha256(local_inventory) != digest:
                    raise PatrolError("本轮系统Kooda快照本机副本SHA256不一致")
                self.record_partial_acquisition(
                    **facts, processingSystemKooda=str(local_inventory),
                    systemKoodaRows=verified_inventory["totalRows"],
                )
                if not is_report_source(verified_inventory) and not verified_inventory.get("paginationEvidencePath"):
                    raise PatrolError("本轮系统Kooda库存缺少分页诊断回执")
                if verified_inventory.get("paginationEvidencePath"):
                    evidence_path = Path(str(system_inventory) + ".pagination.json")
                    self.record_partial_acquisition(
                        systemKoodaPaginationEvidence=str(evidence_path),
                        systemKoodaPaginationEvidenceSha256=verified_inventory["paginationEvidenceSha256"],
                        systemKoodaPaginationAttemptCount=verified_inventory.get("paginationAttemptCount"),
                        systemKoodaPaginationRecovered=verified_inventory.get("paginationRecovered"),
                    )

            confirmations = {
                "export": confirm_export, "server_dates": confirm_server_dates,
                "active": confirm_active, "balance": lambda payload: None,
                "scan_cache": confirm_scan_cache, "system_kooda": confirm_system_kooda,
                "google_chatr_download": confirm_chatr_google_download,
                "chatr_google_date_snapshot": confirm_chatr_google_snapshot,
                "card_retention_report": confirm_card_retention,
            }
            consumed = set()
            # Only this coordinator thread confirms results and saves state.
            # Observe a fast failing read immediately instead of waiting behind
            # the slow export; still drain every started job and archive source.
            while True:
                release_background_reads()
                pending = {name: future for name, future in jobs.items() if name not in consumed}
                if not pending:
                    break
                done, _ = wait(pending.values(), timeout=0.1 if not background_reads_started else None,
                               return_when=FIRST_COMPLETED)
                # The atomic signal may have appeared between the preceding
                # local read and export completion. Validate it before collect.
                if not background_reads_started and jobs["export"] in done:
                    release_background_reads()
                for name, future in pending.items():
                    if future in done:
                        consumed.add(name)
                        collect(name, confirmations[name])
        if errors:
            raise errors[0][1]
        if not background_reads_started:
            raise PatrolError("未取得本轮导出确认提交回执，未启动系统统计采集")

        partial = self.state["partialAcquisition"]
        server_date = partial["serverDate"]
        balance = dict(results["balance"].payload)
        for row in balance.get("replyRates") or []:
            if row.get("statDate") and row.get("statDate") != server_date:
                row["rawReplyRatePct"] = row.get("replyRatePct")
                row["rawStatDate"] = row.get("statDate")
                row["replyRatePct"] = None
                row["statDate"] = None
                row["displayStatus"] = "unknown_due_to_date_mismatch"
        balance.update({
            "serverDate": server_date,
            "checkedAtLocal": now_hkt().isoformat(),
            "silentBrowserMode": True,
            "browserForegroundActivationAttempted": False,
            "normalization": {"rule": "reply-rate date must equal server business date; otherwise display unknown"},
        })
        balance_path = self.receipts / f"opensms_balance_final_{self.run_id}.json"
        if "carrierReplyRates" in balance:
            from opensms_carrier_reply_comparison import persist_and_compare
            balance["carrierReplyComparison"] = persist_and_compare(
                balance, self.run_id, server_date, STATE_ROOT / "carrier_reply_history",
            )
        atomic_json(balance_path, balance)
        self.record_partial_acquisition(balance=str(balance_path))
        data = {
            **{key: value for key, value in partial.items() if key not in ("runId", "status", "verifiedAt")},
            "processingUsesRunPrivateLocalCopies": True,
            "subtaskDurations": {
                "export": results["export"].duration_seconds,
                "serverDatesCombined": results["server_dates"].duration_seconds,
                "serverDateDay": round(float(results["server_dates"].payload["day"].get("durationMs") or 0) / 1000, 3),
                "serverDatePhoneConfig": round(float(results["server_dates"].payload["phoneConfig"].get("durationMs") or 0) / 1000, 3),
                "activeCarriers": results["active"].duration_seconds,
                "balance": results["balance"].duration_seconds,
                "scanCache": results["scan_cache"].duration_seconds,
                "systemKoodaInventory": results["system_kooda"].duration_seconds,
                **({"chatrGoogleDownload": results["google_chatr_download"].duration_seconds,
                    "chatrGoogleDateSnapshot": results["chatr_google_date_snapshot"].duration_seconds}
                   if self.chatr_google_date_fallback else {}),
            },
            "readConcurrency": max(1, min(self.args.io_workers, 4)),
            "acquisitionScheduling": scheduling,
            "acquireTransport": "exact-430-raw-cdp-background-targets",
            "playwrightBrowserConnectionUsed": False,
        }
        self.record_partial_acquisition(status="complete")
        return self.complete_stage("acquire", data, started)

    def verify_chatr_google_acquisition(self, acquire: dict[str, Any]) -> dict[str, Any]:
        for field, digest_field in (
            ("chatrGoogleSource", "chatrGoogleSourceSha256"),
            ("processingChatrGoogleSource", "chatrGoogleSourceSha256"),
            ("chatrGoogleDates", "chatrGoogleDatesSha256"),
            ("processingChatrGoogleDates", "chatrGoogleDatesSha256"),
            ("chatrGoogleDownloadReceipt", "chatrGoogleDownloadReceiptSha256"),
        ):
            path = Path(acquire.get(field) or "")
            if not path.is_file() or sha256(path) != acquire.get(digest_field):
                raise PatrolError(f"本轮Chatr Google输入缺失或SHA256不一致: {field}")
        payload = read_json(acquire["chatrGoogleDates"])
        if (payload.get("status") != "passed" or payload.get("runId") != self.run_id
                or payload.get("source") != "google_chatr_dates"
                or payload.get("sourceSha256") != acquire.get("chatrGoogleSourceSha256")
                or Path(payload.get("sourcePath") or "").resolve() != Path(acquire["processingChatrGoogleSource"]).resolve()
                or not isinstance(payload.get("records"), list)
                or type(payload.get("recordCount")) is not int
                or payload["recordCount"] != len(payload["records"])):
            raise PatrolError("本轮Chatr Google日期快照运行身份或来源绑定错误")
        download = read_json(acquire["chatrGoogleDownloadReceipt"])
        try:
            began = datetime.fromisoformat(str(download.get("startedAt") or "").replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(download.get("downloadedAt") or "").replace("Z", "+00:00"))
            frozen = datetime.fromisoformat(str(payload.get("createdAt") or "").replace("Z", "+00:00"))
            run_began = datetime.strptime(self.run_id[:15], "%Y%m%dT%H%M%S").replace(tzinfo=TZ)
            fresh = run_began <= began <= ended <= frozen <= now_hkt()
        except (ValueError, TypeError):
            fresh = False
        if (not fresh or download.get("runId") != self.run_id
                or download.get("sha256") != acquire["chatrGoogleSourceSha256"]
                or download.get("sourceUrl") != "https://docs.google.com/spreadsheets/d/1GiA9uR_2IQ4xntP_aWvCSaWfmC6WVQ7SrnXsy8YyyvM/export?format=xlsx"
                or download.get("silentBrowserMode") is not True
                or download.get("browserForegroundActivationAttempted") is not False
                or download.get("networkOnly") is not True):
            raise PatrolError("本轮Chatr Google下载回执的运行身份、新鲜度或来源验证失败")
        return payload

    def verify_system_kooda_acquisition(self, acquire: dict[str, Any]) -> dict[str, Any]:
        path = Path(acquire.get("systemKoodaInventory") or "")
        if not path.is_file() or sha256(path) != acquire.get("systemKoodaSha256"):
            raise PatrolError("本轮系统Kooda快照缺失或SHA256不一致")
        payload = read_json(path)
        if is_report_source(payload):
            try:
                validate_projection(payload, self.run_id)
            except (RuntimeError, ValueError, TypeError) as exc:
                raise PatrolError(str(exc)) from exc
            source = Path(payload['sourceWorkbookPath'])
            staged = (self.state.get('backgroundVerifiedInputs') or {}).get('rawExport')
            if staged:
                if (staged.get('sha256') != payload['sourceWorkbookSha256']
                        or Path(staged.get('sourcePath') or '').resolve() != source.resolve()):
                    raise PatrolError('后台F原始10日表副本绑定错误')
                source = Path(staged['path'])
            if not source.is_file() or sha256(source) != payload['sourceWorkbookSha256']:
                raise PatrolError('本轮原始10日表来源SHA256不一致')
            return payload
        if (payload.get("status") != "passed" or payload.get("runId") != self.run_id
            or not isinstance(payload.get("systemRows"), list)
            or type(payload.get("totalRows")) is not int or payload["totalRows"] < 0
            or len(payload["systemRows"]) != payload.get("totalRows")
            or payload.get("paginationTotalStable") is not True
            or payload.get("paginationBackendIdsUnique") is not True
            or payload.get("silentBrowserMode") is not True
            or payload.get("browserForegroundActivationAttempted") is not False):
            raise PatrolError("本轮系统Kooda快照完整性/运行编号/后台静默验证失败")
        if payload.get("paginationEvidencePath"):
            evidence_source = path
            # Detached F copies immutable inventory bytes. Its SHA-bound job
            # keeps the original source path; do not rewrite that archived
            # payload merely to move its adjacent evidence reference.
            staged = (self.state.get("backgroundVerifiedInputs") or {}).get("inventory")
            staged_evidence = None
            if staged:
                original = Path(staged.get("sourcePath") or "")
                if (self.state.get("backgroundTaskF") is not True
                        or Path(staged.get("path") or "").resolve() != path.resolve()
                        or staged.get("sha256") != acquire.get("systemKoodaSha256")
                        or not original.is_absolute()):
                    raise PatrolError("本轮系统Kooda后台副本与原始快照绑定不一致")
                evidence_source = original
                staged_evidence = self.state["backgroundVerifiedInputs"].get("inventoryPaginationEvidence")
                if not staged_evidence and (not original.is_file()
                        or sha256(original) != acquire.get("systemKoodaSha256")):
                    raise PatrolError("本轮系统Kooda后台副本与原始快照绑定不一致")
            original_evidence = Path(str(evidence_source) + ".pagination.json")
            evidence_path = original_evidence
            if staged_evidence:
                evidence_path = Path(staged_evidence.get("path") or "")
                if (evidence_path.resolve().parent != path.resolve().parent
                        or Path(staged_evidence.get("sourcePath") or "").resolve() != original_evidence.resolve()
                        or staged_evidence.get("sha256") != payload.get("paginationEvidenceSha256")):
                    raise PatrolError("本轮系统Kooda后台分页诊断副本绑定不一致")
            if (Path(payload["paginationEvidencePath"]).resolve() != original_evidence.resolve()
                    or not evidence_path.is_file()
                    or sha256(evidence_path) != payload.get("paginationEvidenceSha256")):
                raise PatrolError("本轮系统Kooda分页诊断路径或SHA256不一致")
            evidence = read_json(evidence_path)
            count = payload.get("paginationAttemptCount")
            if (evidence.get("status") != "passed" or evidence.get("runId") != self.run_id
                    or type(count) is not int or not 1 <= count <= 3
                    or evidence.get("attemptCount") != count
                    or type(payload.get("paginationRecovered")) is not bool
                    or payload["paginationRecovered"] != (count > 1)
                    or evidence.get("recovered") != payload["paginationRecovered"]):
                raise PatrolError("本轮系统Kooda分页诊断状态或运行身份不一致")
        elif acquire.get("systemKoodaPaginationEvidence"):
            raise PatrolError("本轮系统Kooda库存缺少已登记分页诊断回执")
        return payload

    def verify_policy(self) -> None:
        content = POLICY.read_text(encoding="utf-8")
        markers = ("后台静默", "新鲜", "独立只读回读", "任务B", "任务C", "任务D", "任务E", "任务F", "最终交付")
        missing = [marker for marker in markers if marker not in content]
        if missing:
            raise PatrolError(f"策略文件关键门禁缺失: {missing}")
        self.state["policyReadAt"] = now_hkt().isoformat()
        self.state["policySha256"] = sha256(POLICY)
        self._save_state()

    def mark_artifact_operation(self, expected_output_count: int = 2) -> None:
        if self.state.get("artifactOperationMarked"):
            return
        if os.environ.get("OPENSMS_ARTIFACT_OPERATION_ALREADY_MARKED") == "1":
            self.state["artifactOperationMarked"] = True
            self._save_state()
            return
        if ARTIFACT_MARKER.is_file():
            completed = subprocess.run(
                [
                    str(NODE), str(ARTIFACT_MARKER),
                    "--operation-kind", "create",
                    "--expected-output-count", str(expected_output_count),
                    "--output-format", "xlsx",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=self.child_environment(),
            )
            if completed.returncode:
                raise PatrolError(f"工作簿操作登记失败: {completed.stderr.strip()}")
        self.state["artifactOperationMarked"] = True
        self._save_state()

    def export_task_f_deadlines(self, database_snapshot: Path) -> dict[str, Any]:
        """Export the immutable Task F snapshot locally; never redo a lookup."""
        if self.task_a_only:
            raise PatrolError("仅任务A模式禁止任务F数据库导出")
        if not database_snapshot.is_file():
            raise PatrolError(f"任务F截止日导出缺少本轮数据库快照: {database_snapshot}")
        workbook = self.delivery / f"OpenSMS_Kooda数据库全部数据_截止日导入_{self.run_id}.xlsx"
        receipt = self.receipts / f"opensms_task_f_deadline_export_{self.run_id}.json"
        payload = self.command(
            "task_f_export_deadline_template",
            [
                PYTHON, SCRIPTS / "opensms_export_task_f_deadlines.py",
                "--db", database_snapshot, "--output", workbook,
                "--receipt", receipt, "--json",
            ],
        ).payload
        if (
            payload.get("status") != "success"
            or payload.get("verificationPassed") is not True
            or not workbook.is_file()
            or not receipt.is_file()
        ):
            raise PatrolError("任务F截止日模板未通过本地独立回读，拒绝交付")
        counts = [int(payload.get(key) or 0) for key in ("recordCount", "activationCount", "missingDateCount")]
        if any(value < 0 for value in counts) or counts[0] != counts[1] + counts[2]:
            raise PatrolError("任务F截止日模板全量计数不一致，拒绝交付")
        return {
            "deadlineWorkbook": str(workbook),
            "deadlineExportReceipt": str(receipt),
            "deadlineRecordCount": counts[0],
            "deadlineActivationCount": counts[1],
            "deadlineMissingDateCount": counts[2],
        }

    def run_task_f(self) -> dict[str, Any]:
        """Serialize DB writers across foreground F and detached F jobs."""
        if self.task_a_only:
            return self.skip_task_f()
        from opensms_task_f_background import database_writer_lock
        with database_writer_lock(TASK_F_DB):
            try:
                return self._run_task_f_locked()
            except Exception:
                # A later batch/login/network failure must not hide dates that
                # an earlier batch has already durably committed. Remain under
                # the same DB writer lock for this local incremental export.
                if self.state.get("backgroundTaskF") is True:
                    imports = sorted(self.receipts.glob(f"opensms_task_f_database_import_batch_*_{self.run_id}.json"))
                    receipt = self.receipts / f"opensms_task_f_delta_{self.run_id}.json"
                    if imports and not receipt.exists():
                        output = self.delivery / f"OpenSMS_任务F新增激活日期_{self.run_id}.xlsx"
                        args = [PYTHON, SCRIPTS / "opensms_task_f_delta.py", "--db", TASK_F_DB,
                                "--output", output, "--receipt", receipt, "--run-id", self.run_id]
                        for imported in imports:
                            args.extend(["--import-receipt", imported])
                        try:
                            delta = self.command("task_f_export_confirmed_partial_dates", args).payload
                            self.validate_task_f_delta(delta, output)
                            self.state.setdefault("stages", {}).setdefault("taskF", {}).update({
                                "incrementalWorkbook": str(output) if delta.get("generated") is True else None,
                                "incrementalReceipt": str(receipt),
                                "newActivationImportedCount": int(delta.get("recordCount") or 0),
                                "notifyIncrementalWorkbook": delta.get("generated") is True,
                            })
                        except Exception as delta_error:
                            self.state["taskFPartialIncrementalExportError"] = str(delta_error)
                        self._save_state()
                raise

    @staticmethod
    def validate_task_f_delta(payload: dict[str, Any], output: Path) -> None:
        count = payload.get("recordCount")
        generated = payload.get("generated")
        if (payload.get("status") != "success" or payload.get("verificationPassed") is not True
                or type(count) is not int or count < 0 or type(generated) is not bool
                or generated != (count > 0) or output.is_file() != generated):
            raise PatrolError("任务F新增激活日期表数量、生成条件或独立回读未通过")

    def _run_task_f_locked(self) -> dict[str, Any]:
        """Fill the local library for blank activation dates in this run's report."""
        if self.task_a_only:
            return self.skip_task_f()
        automatic_catalog_sync = not self.authorization["taskF"]
        document_stage = self.state.get("stages", {}).get("document") or {}
        document_generated_before_lookup = (
            bool(document_stage.get("completedAt"))
            and bool(document_stage.get("cardResult"))
            and Path(document_stage["cardResult"]).is_file()
        )
        post_document_supplemental = automatic_catalog_sync and document_generated_before_lookup
        report_backfill_mode = document_generated_before_lookup
        if automatic_catalog_sync and not report_backfill_mode:
            raise PatrolError("自动任务F必须先有本轮已完成的合并文档，不得退回全库补查")
        document_path = Path(document_stage["cardResult"]) if report_backfill_mode else None
        document_sha256 = sha256(document_path) if document_path else None
        if report_backfill_mode:
            proof = read_json(Path(document_stage["cardReceipt"]))
            source_review = (proof.get("verification") or {}).get("system_kooda_source_review") or {}
            proof_document = Path(proof.get("output_path") or "")
            if self.state.get("backgroundTaskF") is True:
                # A detached job retains the original verifier receipt unchanged
                # but reads only its hash-bound private workbook copy. The main
                # processing path may legitimately be archived or cleaned later.
                bound = self.state.get("backgroundVerifiedInputs") or {}
                bound_document = bound.get("document") or {}
                bound_proof = bound.get("documentReceipt") or {}
                if (str(document_path) != bound_document.get("path")
                        or document_sha256 != bound_document.get("sha256")
                        or str(document_stage["cardReceipt"]) != bound_proof.get("path")
                        or sha256(Path(document_stage["cardReceipt"])) != bound_proof.get("sha256")):
                    raise PatrolError("任务F独立文档与原始核验回执绑定不一致")
                proof_document = document_path
            document_acquire = self.state.get("stages", {}).get("acquire") or {}
            source_sha256 = document_acquire.get("systemKoodaSha256")
            if (proof.get("result") != "成功" or not proof.get("checks")
                or any(value is not True for value in proof["checks"].values())
                or proof.get("failed_checks") != []
                or source_review.get("matches") is not True
                or source_review.get("run_id") != self.run_id
                or not proof_document.is_file()
                or sha256(proof_document) != document_sha256
                or not isinstance(source_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
                or source_review.get("snapshot_sha256") != source_sha256):
                raise PatrolError("任务F仅接受本轮已独立核验的最新合并表")
        prior = self.state.get("stages", {}).get("taskF") or {}
        if prior.get("status") in {"passed", "sample_passed"} and all(
            Path(prior[key]).is_file()
            for key in (
                "inventory", "pending", "lookupReceipt", "importReceipt",
                "databaseSnapshot", "iccidLibrary", "latestIccidLibrary",
            )
        ):
            if report_backfill_mode and (
                prior.get("reportBackfillMode") is not True
                or prior.get("documentSha256") != document_sha256
                or prior.get("documentRunId") != self.run_id
            ):
                raise PatrolError("任务F旧断点未绑定当前文档空激活日期规则，请启动新轮次")
            if not all(prior.get(key) and Path(prior[key]).is_file() for key in ("deadlineWorkbook", "deadlineExportReceipt")):
                # Old successful checkpoints predate the template export. Only
                # supplement that local artifact from their own saved snapshot.
                prior.update(self.export_task_f_deadlines(Path(prior["databaseSnapshot"])))
                self._save_state()
            return prior

        started = time.monotonic()
        task_f_started_at = now_hkt().isoformat()
        self.state["taskFStartedAt"] = task_f_started_at
        self.state["taskFProgressIntervalSeconds"] = 300
        self._save_state()

        def progress_metadata() -> dict[str, Any]:
            return {
                "runId": self.run_id,
                "startedAt": task_f_started_at,
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "progressIntervalSeconds": 300,
            }

        env = {
            "BITBROWSER_CDP": self.args.cdp or "",
            "OPENSMS_FORCE_RAW_CDP": "1",
            "OPENSMS_TASK_F_POLICY_CATALOG_SYNC": "1",
            "OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY": "1",
        }
        manual_session = self.state.get("taskFUseCurrent430Session") is True
        if manual_session:
            from opensms_bitbrowser_430 import PROFILE_ID, SESSION_POLICY
            automatic_430 = (self.state.get("taskFSessionPolicy") == SESSION_POLICY
                and self.state.get("taskFBrowserProfileId") == PROFILE_ID
                and self.state.get("taskFSessionJobSha256")
                and self.state["taskFSessionJobSha256"] == self.state.get("backgroundJobSha256"))
            explicit_retry = (self.state.get("retrySourceJobSha256")
                and self.state.get("retryAuthorizationSource") == "current-user")
            if (self.state.get("backgroundTaskF") is not True
                    or not (automatic_430 or explicit_retry)):
                raise PatrolError("任务F430会话模式缺少本次已核验作业绑定")
            env.update(OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY="0",
                       OPENSMS_TASK_F_CURRENT_SESSION_ONLY="1")
        if self.authorization["taskF"]:
            env.update({
                "OPENSMS_FORCE_TASK_F": "1",
                "OPENSMS_ENABLE_TASK_F": "1",
                "OPENSMS_EXECUTE_TASK_F": "1",
            })

        catalog_receipt = self.receipts / f"opensms_task_f_catalog_import_{self.run_id}.json"
        if not TASK_F_CATALOG_SOURCE.is_file():
            raise PatrolError(f"任务F权威附件不存在: {TASK_F_CATALOG_SOURCE}")
        catalog_result = self.command(
            "task_f_catalog_import",
            [
                PYTHON, SCRIPTS / "opensms_task_f_db.py", "import-catalog",
                "--db", TASK_F_DB,
                "--catalog", TASK_F_CATALOG_SOURCE,
                "--receipt", catalog_receipt,
            ],
            env=env,
        )
        catalog_payload = catalog_result.payload

        acquired = self.state.get("stages", {}).get("acquire") or {}
        inventory_path = Path(acquired["systemKoodaInventory"]) if acquired.get("systemKoodaInventory") else self.receipts / f"opensms_task_f_kooda_inventory_{self.run_id}.json"
        inventory = self.verify_system_kooda_acquisition(acquired) if acquired.get("systemKoodaInventory") else (read_json(inventory_path) if inventory_path.is_file() else {})
        if inventory.get("status") != "passed":
            self.command(
                "task_f_kooda_inventory",
                [NODE, SCRIPTS / "opensms_collect_task_f_kooda_cards.mjs", inventory_path],
                env=env,
            )
            inventory = read_json(inventory_path)

        pending_path = self.receipts / f"opensms_task_f_pending_{self.run_id}.json"
        pending_command = [
            PYTHON, SCRIPTS / "opensms_task_f_db.py", "pending",
            "--db", TASK_F_DB, "--cards", inventory_path, "--output", pending_path,
        ]
        if report_backfill_mode:
            pending_command.extend([
                "--document", document_path, "--document-sha256", document_sha256,
                "--run-id", self.run_id,
                "--document-proof", document_stage["cardReceipt"],
                "--document-proof-sha256", sha256(Path(document_stage["cardReceipt"])),
            ])
            if self.state.get('taskFKoRefreshEnabled') is True:
                bound_cache = (self.state.get('backgroundVerifiedInputs') or {}).get('reportDateCache') or {}
                if not bound_cache.get('path') or not bound_cache.get('sha256'):
                    raise PatrolError('ko补查缺少后台作业冻结库绑定')
                pending_command.extend(['--ko-cache', bound_cache['path'],
                                        '--ko-cache-sha256', bound_cache['sha256']])
        pending_result = self.command(
            "task_f_prepare_pending",
            pending_command,
            env=env,
        )
        pending = read_json(pending_path)
        if report_backfill_mode and (
            pending.get("reportBackfillMode") is not True
            or pending.get("documentSha256") != document_sha256
            or pending.get("documentRunId") != self.run_id
        ):
            raise PatrolError("任务F待查清单未绑定本轮空激活日期文档")
        backfill_metadata = {
            key: pending.get(key) for key in (
                "reportBackfillMode", "documentPath", "documentSha256", "documentRunId",
                "documentKoodaRows", "documentMissingActivationRows",
                "documentActivationPresentRows", "documentMissingActivationIccidCount",
                "documentExcludedCount", "readyForUploadCount", "refreshExistingMissingCount",
                "documentMissingDateRows", "documentMissingDateIccidCount", "documentMissingCutoffRows",
                "documentCreationFallbackRows", "documentLookupNeededRows", "dateEligibilityMode",
                "documentProofPath", "documentProofSha256",
                "documentKoRefreshRows", "totalLookupNeededRows", "koRefresh",
            )
        }
        backfill_metadata.update({"cloudUploadAttempted": False,
                                  "databaseActivationMissingOnly": report_backfill_mode})

        full_pending_path = pending_path
        full_pending_count = int(pending.get("pendingCount") or 0)
        sample_size = int(getattr(self.args, "task_f_sample_size", 0) or 0)
        if sample_size:
            selected_rows = list(pending.get("rows") or [])[:sample_size]
            pending_path = self.receipts / f"opensms_task_f_pending_sample_{len(selected_rows)}_{self.run_id}.json"
            sample_pending = {
                **pending,
                "sampleMode": True,
                "sampleLimit": sample_size,
                "fullPendingCount": full_pending_count,
                "pendingCount": len(selected_rows),
                "rows": selected_rows,
            }
            atomic_json(pending_path, sample_pending)
            pending = sample_pending

        pending_rows = list(pending.get("rows") or [])
        login_receipt = self.receipts / f"grabandgo_task_f_login_{self.run_id}.json"
        if pending_rows:
            if manual_session and self.state.get("taskFSessionPolicy") == "existing-bitbrowser-430":
                from opensms_bitbrowser_430 import resolve_current_430_cdp
                env["BITBROWSER_CDP"] = resolve_current_430_cdp()
                self.args.cdp = env["BITBROWSER_CDP"]
                self.state["taskFResolvedCdpEndpoint"] = self.args.cdp
                self._save_state()
            login_payload = read_json(login_receipt) if login_receipt.is_file() else {}
            if login_payload.get("status") not in {"authenticated", "passed"}:
                if login_receipt.is_file():
                    login_receipt = self.receipts / f"grabandgo_task_f_login_retry_{self.run_id}_{int(time.time())}.json"
                login = self.command(
                    "task_f_login_check",
                    [
                        NODE,
                        SCRIPTS / "grabandgo_task_f_lookup.mjs",
                        "--check-login" if manual_session else "--check-independent-logins",
                        login_receipt,
                        *([] if manual_session else ["1"]),
                    ],
                    env=env,
                    allow_codes=(0, 4),
                )
                login_payload = login.payload
            if (manual_session and login_payload.get('status') == 'login_required'
                    and login_payload.get('autoLoginAttempted') is not True
                    and login_payload.get('authenticationFailureConfirmed') is True):
                policy = read_json(SCRIPTS / 'grabandgo_login_policy.json')
                if (policy.get('enabled') is True
                        and policy.get('authorization') == 'current-user-standing-grab-login-on-authentication-failure'
                        and policy.get('maxRecoveryLoginPreflight') == 1
                        and policy.get('queryBeforeVerifiedLogin') is False
                        and policy.get('replayFailedJobs') is False):
                    # A confirmed login-page result may use the newly authorized
                    # isolated login. Network/403/protocol exceptions do not.
                    env.update(OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY='1',
                               OPENSMS_TASK_F_CURRENT_SESSION_ONLY='0')
                    login_receipt = self.receipts / f'grabandgo_task_f_authorized_login_{self.run_id}.json'
                    login = self.command('task_f_authorized_login_check',
                        [NODE, SCRIPTS / 'grabandgo_task_f_lookup.mjs', '--check-independent-logins', login_receipt, '1'],
                        env=env, allow_codes=(0, 4))
                    login_payload = login.payload
                    self.state['taskFAuthorizedLoginRecovery'] = {
                        'attempted': True, 'passed': login_payload.get('status') == 'passed',
                        'receipt': str(login_receipt), 'credentialsWrittenToReceipt': False}
                    self._save_state()
            if login_payload.get("status") not in {"authenticated", "passed"}:
                data = {
                    **backfill_metadata,
                    "executed": True,
                    "automaticCatalogSync": automatic_catalog_sync,
                    "postDocumentSupplemental": post_document_supplemental,
                    "documentGeneratedBeforeLookup": document_generated_before_lookup,
                    "resultsApplyNextExport": True,
                    "documentDateSource": "system_then_frozen_local_cache",
                    "resultsWriteBackToDocument": False,
                    "status": "login_required",
                    "loginRequired": True,
                    "loginUrl": login_payload.get("customerSimUrl") or login_payload.get("accountUrl"),
                    "loginInstruction": login_payload.get("loginInstruction"),
                    "loginReceipt": str(login_receipt),
                    "catalogReceipt": str(catalog_receipt),
                    "browser": login_payload.get("browser") or "BitBrowser 430",
                    "transportPriority": login_payload.get("transportPriority") or ["same-origin-http", "background-page-javascript"],
                    "sensitiveIccidSubmittedCount": 0,
                }
                self.complete_stage("taskF", data, started)
                raise PatrolError("任务F增量补查未执行：Grab&Go独立登录未通过，请检查钥匙串凭据或登录验证")
        else:
            login_payload = {
                "status": "not_required_no_missing_iccid",
                "loginStatus": "not_required",
                "silentBrowserMode": True,
                "browserForegroundActivationAttempted": False,
                "browser": "本地权威附件与SQLite（无需访问Grab&Go）",
                "transportPriority": ["same-origin-http", "background-page-javascript"],
            }
            atomic_json(login_receipt, login_payload)

        lookup_path = self.receipts / f"grabandgo_task_f_lookup_{self.run_id}.json"
        progress_path = self.receipts / f"grabandgo_task_f_progress_{self.run_id}.json"
        import_receipt = self.receipts / f"opensms_task_f_database_import_{self.run_id}.json"
        batch_size = (
            len(pending_rows)
            if sample_size and pending_rows
            else max(25, int(os.environ.get("OPENSMS_TASK_F_BATCH_SIZE", "250")))
            if pending_rows
            else 0
        )
        batches = [] if not pending_rows else [
            pending_rows[index:index + batch_size]
            for index in range(0, len(pending_rows), batch_size)
        ]
        lookup_payloads: list[dict[str, Any]] = []
        import_payloads: list[dict[str, Any]] = []
        aggregate_results: list[dict[str, Any]] = []
        aggregate_status_counts: dict[str, int] = {}
        atomic_json(progress_path, {
            **progress_metadata(),
            "event": "task_f_batched_lookup_started",
            "status": "running",
            "updatedAt": now_hkt().isoformat(),
            "totalInputCount": len(pending_rows),
            "completedCount": 0,
            "remainingCount": len(pending_rows),
            "statusCounts": {},
            "requestedWorkers": int(self.args.task_f_workers),
            "effectiveWorkers": 0,
            "batchSize": batch_size,
            "batchCount": len(batches),
            "completedBatchCount": 0,
            "databaseCheckpointAfterEveryBatch": True,
        })
        if not batches:
            stats_result = self.command(
                "task_f_database_stats_no_gap",
                [PYTHON, SCRIPTS / "opensms_task_f_db.py", "stats", "--db", TASK_F_DB],
                env=env,
            )
            stats = stats_result.payload
            no_gap_lookup = {
                "status": "passed",
                "createdAt": now_hkt().isoformat(),
                "mode": "task-f-local-catalog-no-network-gap",
                "browser": "本地权威附件与SQLite（无需访问Grab&Go）",
                "loginStatus": "not_required",
                "silentBrowserMode": True,
                "browserForegroundActivationAttempted": False,
                "transportPriority": ["same-origin-http", "background-page-javascript"],
                "sameOriginHttpRequests": 0,
                "backgroundJavascriptFallbackRequests": 0,
                "requestedWorkers": int(self.args.task_f_workers),
                "effectiveWorkers": 0,
                "independentAspNetSessions": False,
                "uniqueAspNetSessionCount": 0,
                "workerSessionMode": "not_required_no_missing_iccid",
                "adaptiveConcurrency": True,
                "sharedSessionForcesSerial": True,
                "requestedWorkersSatisfied": True,
                "independentFormLoginAttempted": False,
                "independentFormLoginSucceeded": False,
                "strictIdentityGuard": "queryIccid_equals_listSimNumber_equals_detailSimIccid",
                "identityMismatchStoredAsTerminal": False,
                "sharedSessionNotFoundRequiresSerialConfirmation": True,
                "sharedSessionSerialConfirmationCount": 0,
                "sharedSessionSerialConfirmationFailureCount": 0,
                "perWorkerDelayMs": 1000,
                "progressIntervalSeconds": 300,
                "recordSchemaVersion": int(stats.get("recordSchemaVersion") or 0),
                "recordedWebFields": stats.get("recordedWebFields") or [],
                "fullSourceDataIncluded": True,
                "inputCount": 0,
                "statusCounts": {},
                "results": [],
            }
            no_gap_import = {
                "status": "passed",
                "createdAt": now_hkt().isoformat(),
                "database": str(TASK_F_DB),
                "resultCount": 0,
                "terminalImportedCount": 0,
                "foundImportedCount": 0,
                "alreadyTerminalKeptCount": 0,
                "schemaUpgradedCount": 0,
                "transientErrorCount": 0,
                "databaseTerminalCount": int(stats.get("terminalLookupCount") or 0),
                "databaseCurrentSchemaCount": int(stats.get("currentRecordSchemaCount") or 0),
                "recordSchemaVersion": int(stats.get("recordSchemaVersion") or 0),
                "recordedWebFields": stats.get("recordedWebFields") or [],
                "fullSourceDataIncluded": True,
                "cutoffRule": "activation_date_plus_29_days_inclusive_30_day_period",
                "rows": [],
                "transientErrors": [],
            }
            lookup_payloads.append(no_gap_lookup)
            import_payloads.append(no_gap_import)
            atomic_json(progress_path, {
                **progress_metadata(),
                "event": "task_f_catalog_sync_completed_no_network_gap",
                "status": "lookup_completed",
                "updatedAt": now_hkt().isoformat(),
                "totalInputCount": 0,
                "completedCount": 0,
                "remainingCount": 0,
                "statusCounts": {},
                "requestedWorkers": int(self.args.task_f_workers),
                "effectiveWorkers": 0,
                "batchSize": 0,
                "batchCount": 0,
                "completedBatchCount": 0,
                "databaseCheckpointAfterEveryBatch": True,
                "databaseTerminalCount": int(stats.get("terminalLookupCount") or 0),
            })
        for batch_index, batch_rows in enumerate(batches, start=1):
            batch_pending_path = self.receipts / f"opensms_task_f_pending_batch_{batch_index:03d}_{self.run_id}.json"
            batch_lookup_path = self.receipts / f"grabandgo_task_f_lookup_batch_{batch_index:03d}_{self.run_id}.json"
            batch_progress_path = self.receipts / f"grabandgo_task_f_progress_batch_{batch_index:03d}_{self.run_id}.json"
            batch_import_path = self.receipts / f"opensms_task_f_database_import_batch_{batch_index:03d}_{self.run_id}.json"
            atomic_json(batch_pending_path, {
                **pending,
                "batchMode": True,
                "batchIndex": batch_index,
                "batchCount": len(batches),
                "batchSize": batch_size,
                "pendingCount": len(batch_rows),
                "rows": batch_rows,
            })
            atomic_json(progress_path, {
                **progress_metadata(),
                "event": "task_f_batched_lookup_progress",
                "status": "running",
                "updatedAt": now_hkt().isoformat(),
                "totalInputCount": len(pending_rows),
                "completedCount": len(aggregate_results),
                "remainingCount": max(0, len(pending_rows) - len(aggregate_results)),
                "statusCounts": aggregate_status_counts,
                "requestedWorkers": int(self.args.task_f_workers),
                "effectiveWorkers": 0 if not lookup_payloads else int(lookup_payloads[-1].get("effectiveWorkers") or 0),
                "batchSize": batch_size,
                "batchCount": len(batches),
                "completedBatchCount": batch_index - 1,
                "currentBatchIndex": batch_index,
                "currentBatchInputCount": len(batch_rows),
                "currentBatchProgress": str(batch_progress_path),
                "databaseCheckpointAfterEveryBatch": True,
            })
            lookup_payload = self.task_f_lookup_batch(
                batch_index, batch_pending_path, batch_lookup_path,
                batch_progress_path, env,
            )
            if lookup_payload.get("status") == "login_required":
                raise PatrolError(f"任务F第{batch_index}批查询前Grab&Go会话失效")
            self.command(
                f"task_f_database_import_batch_{batch_index:03d}",
                [
                    PYTHON,
                    SCRIPTS / "opensms_task_f_db.py",
                    "import",
                    "--db",
                    TASK_F_DB,
                    "--lookup",
                    batch_lookup_path,
                    "--receipt",
                    batch_import_path,
                ],
                env=env,
            )
            batch_import = read_json(batch_import_path)
            lookup_payloads.append(lookup_payload)
            import_payloads.append(batch_import)
            batch_results = [row for row in lookup_payload.get("results") or [] if isinstance(row, dict)]
            aggregate_results.extend(batch_results)
            for status, count in (lookup_payload.get("statusCounts") or {}).items():
                aggregate_status_counts[str(status)] = aggregate_status_counts.get(str(status), 0) + int(count or 0)
            atomic_json(progress_path, {
                **progress_metadata(),
                "event": "task_f_batched_lookup_progress",
                "status": "running" if batch_index < len(batches) else "lookup_completed",
                "updatedAt": now_hkt().isoformat(),
                "totalInputCount": len(pending_rows),
                "completedCount": len(aggregate_results),
                "remainingCount": max(0, len(pending_rows) - len(aggregate_results)),
                "statusCounts": aggregate_status_counts,
                "requestedWorkers": int(self.args.task_f_workers),
                "effectiveWorkers": int(lookup_payload.get("effectiveWorkers") or 0),
                "uniqueAspNetSessionCount": int(lookup_payload.get("uniqueAspNetSessionCount") or 0),
                "workerSessionMode": lookup_payload.get("workerSessionMode") or "",
                "batchSize": batch_size,
                "batchCount": len(batches),
                "completedBatchCount": batch_index,
                "currentBatchProgress": str(batch_progress_path),
                "databaseCheckpointAfterEveryBatch": True,
                "databaseTerminalCount": int(batch_import.get("databaseTerminalCount") or 0),
            })

        last_lookup = lookup_payloads[-1]
        lookup_payload = {
            **last_lookup,
            "runId": self.run_id,
            "reportBackfillMode": report_backfill_mode,
            "databaseMissingOnly": not report_backfill_mode,
            "databaseActivationMissingOnly": report_backfill_mode,
            "cloudUploadAttempted": False,
            "status": "passed_with_transient_errors" if aggregate_status_counts.get("error") else "passed",
            "createdAt": now_hkt().isoformat(),
            "inputCount": len(pending_rows),
            "statusCounts": aggregate_status_counts,
            "results": aggregate_results,
            "batchMode": True,
            "batchSize": batch_size,
            "batchCount": len(batches),
            "completedBatchCount": len(batches),
            "databaseCheckpointAfterEveryBatch": True,
            "sameOriginHttpRequests": sum(int(item.get("sameOriginHttpRequests") or 0) for item in lookup_payloads),
            "backgroundJavascriptFallbackRequests": sum(int(item.get("backgroundJavascriptFallbackRequests") or 0) for item in lookup_payloads),
        }
        atomic_json(lookup_path, lookup_payload)
        last_import = import_payloads[-1]
        import_data = {
            **last_import,
            "runId": self.run_id,
            "documentRunId": self.run_id if report_backfill_mode else None,
            "status": "passed" if sum(int(item.get("transientErrorCount") or 0) for item in import_payloads) == 0 else "passed_with_transient_errors",
            "createdAt": now_hkt().isoformat(),
            "lookupReceipt": str(lookup_path),
            "lookupSha256": sha256(lookup_path),
            "resultCount": sum(int(item.get("resultCount") or 0) for item in import_payloads),
            "terminalImportedCount": sum(int(item.get("terminalImportedCount") or 0) for item in import_payloads),
            "foundImportedCount": sum(int(item.get("foundImportedCount") or 0) for item in import_payloads),
            "activationDateUpdatedCount": sum(int(item.get("activationDateUpdatedCount") or 0) for item in import_payloads),
            "refreshedMissingActivationCount": sum(int(item.get("refreshedMissingActivationCount") or 0) for item in import_payloads),
            "refreshedBlankRecordCount": sum(int(item.get("refreshedBlankRecordCount") or 0) for item in import_payloads),
            "alreadyTerminalKeptCount": sum(int(item.get("alreadyTerminalKeptCount") or 0) for item in import_payloads),
            "schemaUpgradedCount": sum(int(item.get("schemaUpgradedCount") or 0) for item in import_payloads),
            "transientErrorCount": sum(int(item.get("transientErrorCount") or 0) for item in import_payloads),
            "rows": [row for item in import_payloads for row in item.get("rows") or []],
            "transientErrors": [row for item in import_payloads for row in item.get("transientErrors") or []],
            "batchMode": True,
            "batchSize": batch_size,
            "batchCount": len(batches),
            "databaseCheckpointAfterEveryBatch": True,
        }
        atomic_json(import_receipt, import_data)

        iccid_library = self.delivery / f"OpenSMS_Kooda_ICCID最新库_{self.run_id}.json"
        library_result = self.command(
            "task_f_export_latest_iccid_library",
            [
                PYTHON, SCRIPTS / "opensms_task_f_db.py", "export-catalog",
                "--db", TASK_F_DB,
                "--output", iccid_library,
                "--latest", TASK_F_LATEST_LIBRARY,
            ],
            env=env,
        ).payload
        # The live F database and evidence stay local; a bulky derived snapshot
        # follows the detached worker's configured external artifact directory.
        snapshot_directory = self.delivery if self.state.get("backgroundTaskF") is True else self.receipts
        database_snapshot = snapshot_directory / f"task_f_kooda_activation_{self.run_id}.sqlite3"
        self.command(
            "task_f_database_snapshot",
            [
                PYTHON, SCRIPTS / "opensms_task_f_db.py", "snapshot",
                "--db", TASK_F_DB, "--output", database_snapshot,
            ],
            env=env,
        )
        deadline_export = (
            {} if self.state.get("backgroundTaskF") is True
            else self.export_task_f_deadlines(database_snapshot)
        )
        delta_workbook = self.delivery / f"OpenSMS_任务F新增激活日期_{self.run_id}.xlsx"
        delta_receipt = self.receipts / f"opensms_task_f_delta_{self.run_id}.json"
        delta = self.command("task_f_export_incremental_dates", [
            PYTHON, SCRIPTS / "opensms_task_f_delta.py", "--db", TASK_F_DB,
            "--import-receipt", import_receipt, "--output", delta_workbook,
            "--receipt", delta_receipt, "--run-id", self.run_id,
        ]).payload
        self.validate_task_f_delta(delta, delta_workbook)
        data = {
            **deadline_export,
            "incrementalWorkbook": str(delta_workbook) if delta.get("generated") is True else None,
            "incrementalReceipt": str(delta_receipt),
            "newActivationImportedCount": int(delta.get("recordCount") or 0),
            "notifyIncrementalWorkbook": delta.get("generated") is True,
            **backfill_metadata,
            "executed": True,
            "automaticCatalogSync": automatic_catalog_sync,
            "postDocumentSupplemental": post_document_supplemental,
            "documentGeneratedBeforeLookup": document_generated_before_lookup,
            "resultsApplyNextExport": True,
            "documentDateSource": "system_then_frozen_local_cache",
            "resultsWriteBackToDocument": False,
            "catalogReceipt": str(catalog_receipt),
            "catalogSource": str(TASK_F_CATALOG_SOURCE),
            "catalogSourceSha256": catalog_payload.get("sourceSha256"),
            "catalogSourceRowCount": int(catalog_payload.get("sourceRowCount") or 0),
            "catalogFoundCount": int(catalog_payload.get("foundCount") or 0),
            "catalogActivationMissingCount": int(catalog_payload.get("activationMissingCount") or 0),
            "catalogAlreadyImported": catalog_payload.get("alreadyImported") is True,
            "iccidLibrary": str(iccid_library),
            "latestIccidLibrary": str(TASK_F_LATEST_LIBRARY),
            "iccidLibraryRecordCount": int(library_result.get("recordCount") or 0),
            "iccidLibraryFoundCount": int(library_result.get("foundCount") or 0),
            "iccidLibraryActivationMissingCount": int(library_result.get("activationMissingCount") or 0),
            "iccidLibraryNotFoundCount": int(library_result.get("notFoundCount") or 0),
            "onlineLookupTriggered": bool(pending_rows),
            "status": (
                "sample_passed"
                if sample_size and int(import_data.get("transientErrorCount") or 0) == 0
                else "passed"
                if int(import_data.get("transientErrorCount") or 0) == 0
                else "partial_transient_error"
            ),
            "loginRequired": False,
            "loginReceipt": str(login_receipt),
            "inventory": str(inventory_path),
            "inventoryCount": int(inventory.get("uniqueIccidCount") or 0),
            "inventoryTotalRows": int(inventory.get("totalRows") or 0),
            "missingCardStoreCount": int(inventory.get("missingCardStoreCount") or 0),
            "invalidCardStoreCount": int(inventory.get("invalidCardStoreCount") or 0),
            "alreadyQueriedCount": int(pending.get("alreadyQueriedCount") or 0),
            "schemaUpgradePendingCount": int(pending.get("schemaUpgradePendingCount") or 0),
            "pending": str(pending_path),
            "pendingCount": int(pending.get("pendingCount") or 0),
            "refreshedMissingActivationCount": int(import_data.get("refreshedMissingActivationCount") or 0),
            "refreshedBlankRecordCount": int(import_data.get("refreshedBlankRecordCount") or 0),
            "fullPending": str(full_pending_path),
            "fullPendingCount": full_pending_count,
            "sampleMode": bool(sample_size),
            "sampleLimit": sample_size,
            "lookupReceipt": str(lookup_path),
            "progress": str(progress_path),
            "progressIntervalSeconds": int(lookup_payload.get("progressIntervalSeconds") or 300),
            "databaseMissingOnly": not report_backfill_mode,
            "lookupStatusCounts": lookup_payload.get("statusCounts") or {},
            "requestedWorkers": int(self.args.task_f_workers),
            "effectiveWorkers": int(lookup_payload.get("effectiveWorkers") or 0),
            "independentAspNetSessions": lookup_payload.get("independentAspNetSessions") is True,
            "uniqueAspNetSessionCount": int(lookup_payload.get("uniqueAspNetSessionCount") or 0),
            "workerSessionMode": lookup_payload.get("workerSessionMode") or "",
            "sharedAspNetSessionConcurrentWorkers": lookup_payload.get("sharedAspNetSessionConcurrentWorkers") is True,
            "adaptiveConcurrency": lookup_payload.get("adaptiveConcurrency") is True,
            "sharedSessionForcesSerial": lookup_payload.get("sharedSessionForcesSerial") is True,
            "requestedWorkersSatisfied": lookup_payload.get("requestedWorkersSatisfied") is True,
            "independentFormLoginAttempted": lookup_payload.get("independentFormLoginAttempted") is True,
            "independentFormLoginSucceeded": lookup_payload.get("independentFormLoginSucceeded") is True,
            "fullRequestedConcurrencyRequired": lookup_payload.get("fullRequestedConcurrencyRequired") is True,
            "strictIdentityGuard": lookup_payload.get("strictIdentityGuard") or "",
            "identityMismatchStoredAsTerminal": lookup_payload.get("identityMismatchStoredAsTerminal") is True,
            "sharedSessionNotFoundRequiresSerialConfirmation": lookup_payload.get("sharedSessionNotFoundRequiresSerialConfirmation") is True,
            "sharedSessionSerialConfirmationCount": int(lookup_payload.get("sharedSessionSerialConfirmationCount") or 0),
            "sharedSessionSerialConfirmationFailureCount": int(lookup_payload.get("sharedSessionSerialConfirmationFailureCount") or 0),
            "perWorkerDelayMs": int(lookup_payload.get("perWorkerDelayMs") or 0),
            "batchMode": lookup_payload.get("batchMode") is True,
            "batchSize": int(lookup_payload.get("batchSize") or 0),
            "batchCount": int(lookup_payload.get("batchCount") or 0),
            "completedBatchCount": int(lookup_payload.get("completedBatchCount") or 0),
            "databaseCheckpointAfterEveryBatch": lookup_payload.get("databaseCheckpointAfterEveryBatch") is True,
            "importReceipt": str(import_receipt),
            "terminalImportedCount": int(import_data.get("terminalImportedCount") or 0),
            "foundImportedCount": int(import_data.get("foundImportedCount") or 0),
            "schemaUpgradedCount": int(import_data.get("schemaUpgradedCount") or 0),
            "databaseCurrentSchemaCount": int(import_data.get("databaseCurrentSchemaCount") or 0),
            "recordSchemaVersion": int(import_data.get("recordSchemaVersion") or 0),
            "recordedWebFields": import_data.get("recordedWebFields") or [],
            "fullSourceDataIncluded": import_data.get("fullSourceDataIncluded") is True,
            "transientErrorCount": int(import_data.get("transientErrorCount") or 0),
            "database": str(TASK_F_DB),
            "databaseSnapshot": str(database_snapshot),
            "cutoffRule": "activation_date_plus_29_days_inclusive_30_day_period",
            "browser": lookup_payload.get("browser") or "BitBrowser 430 OpenSMS inventory + isolated local HTTP login containers",
            "openSmsInventoryBrowser": "BitBrowser 430",
            "cdpEndpoint": lookup_payload.get("cdpEndpoint"),
            "transportPriority": lookup_payload.get("transportPriority") or ["same-origin-http", "background-page-javascript"],
            "sameOriginHttpRequests": int(lookup_payload.get("sameOriginHttpRequests") or 0),
            "backgroundJavascriptFallbackRequests": int(lookup_payload.get("backgroundJavascriptFallbackRequests") or 0),
            "firecrawlSensitiveLookupUsed": False,
        }
        self.complete_stage("taskF", data, started)
        if data["transientErrorCount"]:
            raise PatrolError(f"任务F仍有{data['transientErrorCount']}个瞬时查询失败，已保存成功结果并拒绝宣称完整完成")
        return data

    def repair_task_f_lookup(self) -> dict[str, Any] | None:
        if self.task_a_only:
            raise PatrolError("仅任务A模式禁止任务F数据库修复")
        lookup = getattr(self.args, "task_f_repair_lookup", None)
        if not lookup:
            return None
        started = time.monotonic()
        before_snapshot = self.receipts / f"task_f_before_repair_{self.run_id}.sqlite3"
        self.command(
            "task_f_database_before_repair_snapshot",
            [PYTHON, SCRIPTS / "opensms_task_f_db.py", "snapshot", "--db", TASK_F_DB, "--output", before_snapshot],
        )
        receipt = self.receipts / f"opensms_task_f_database_retract_{self.run_id}.json"
        result = self.command(
            "task_f_database_retract",
            [
                PYTHON, SCRIPTS / "opensms_task_f_db.py", "retract",
                "--db", TASK_F_DB, "--lookup", Path(lookup), "--receipt", receipt,
            ],
        )
        data = {
            "status": "passed",
            "lookupReceipt": str(Path(lookup).resolve()),
            "beforeSnapshot": str(before_snapshot),
            "receipt": str(receipt),
            "deletedCount": int(result.payload.get("deletedCount") or 0),
            "remainingTerminalCount": int(result.payload.get("remainingTerminalCount") or 0),
            "exactStoredReceiptMatchRequired": True,
        }
        return self.complete_stage("taskFRepair", data, started)

    def finish_task_f_sample(self, task_f: dict[str, Any]) -> dict[str, Any]:
        """Finish a bounded Task F preview without running unrelated patrol stages."""
        lookup = read_json(task_f["lookupReceipt"])
        imported = read_json(task_f["importReceipt"])
        import_by_iccid = {
            str(row.get("queryIccid") or ""): row
            for row in imported.get("rows") or []
            if isinstance(row, dict)
        }
        sample_rows: list[dict[str, Any]] = []
        for row in lookup.get("results") or []:
            query_iccid = str(row.get("queryIccid") or "")
            imported_row = import_by_iccid.get(query_iccid) or {}
            sample_rows.append({
                "phone": str(row.get("phone") or ""),
                "slot": str(row.get("slot") or ""),
                "opr": str(row.get("opr") or ""),
                "carrierName": str(row.get("carrierName") or ""),
                "sourceIccid": str(row.get("sourceIccid") or ""),
                "queryIccid": query_iccid,
                "status": str(row.get("status") or ""),
                "purchaseDate": str(row.get("purchaseDateRaw") or row.get("purchaseDate") or ""),
                "actRequestDate": str(row.get("actRequestDate") or ""),
                "activationDate": str(row.get("activationDate") or ""),
                "activationDateRaw": str(row.get("activationDateRaw") or ""),
                "simNumber": str(row.get("simNumber") or ""),
                "sku": str(row.get("sku") or ""),
                "cutoffDate": str(imported_row.get("cutoffDate") or ""),
                "detailSimIccid": str(row.get("detailSimIccid") or ""),
                "sourceData": row.get("sourceData") if isinstance(row.get("sourceData"), dict) else {},
                "sourceOccurrences": row.get("sourceOccurrences") if isinstance(row.get("sourceOccurrences"), list) else [],
                "workerId": int(row.get("workerId") or 0),
                "error": str(row.get("error") or ""),
            })

        excel_input = self.receipts / f"opensms_task_f_excel_input_{self.run_id}.json"
        atomic_json(excel_input, {
            "runId": self.run_id,
            "generatedAt": now_hkt().isoformat(),
            "browser": task_f.get("browser") or "BitBrowser 430（后台静默）",
            "requestedWorkers": int(task_f.get("requestedWorkers") or 0),
            "effectiveWorkers": int(task_f.get("effectiveWorkers") or 0),
            "independentAspNetSessions": task_f.get("independentAspNetSessions") is True,
            "uniqueAspNetSessionCount": int(task_f.get("uniqueAspNetSessionCount") or 0),
            "workerSessionMode": task_f.get("workerSessionMode") or "",
            "adaptiveConcurrency": task_f.get("adaptiveConcurrency") is True,
            "sharedSessionForcesSerial": task_f.get("sharedSessionForcesSerial") is True,
            "concurrencyLimitedReason": task_f.get("concurrencyLimitedReason") or "",
            "strictIdentityGuard": task_f.get("strictIdentityGuard") or "",
            "inventoryCount": int(task_f.get("inventoryCount") or 0),
            "alreadyQueriedCount": int(task_f.get("alreadyQueriedCount") or 0),
            "fullPendingCount": int(task_f.get("fullPendingCount") or 0),
            "statusCounts": task_f.get("lookupStatusCounts") or {},
            "terminalImportedCount": int(task_f.get("terminalImportedCount") or 0),
            "foundImportedCount": int(task_f.get("foundImportedCount") or 0),
            "recordSchemaVersion": int(task_f.get("recordSchemaVersion") or 0),
            "sampleRows": sample_rows,
        })
        workbook = self.delivery / f"OpenSMS_任务F_100条查询结果_{self.run_id}.xlsx"
        workbook_receipt = self.receipts / f"opensms_task_f_excel_{self.run_id}.json"
        preview_dir = self.receipts / f"opensms_task_f_excel_previews_{self.run_id}"
        xlsx_runtime = self.run_dir / "task_f_xlsx_runtime"
        xlsx_runtime.mkdir(parents=True, exist_ok=True)
        runtime_script = xlsx_runtime / "opensms_task_f_export_xlsx.mjs"
        shutil.copy2(SCRIPTS / "opensms_task_f_export_xlsx.mjs", runtime_script)
        node_modules_link = xlsx_runtime / "node_modules"
        if not node_modules_link.exists():
            node_modules_link.symlink_to(NODE_MODULES, target_is_directory=True)
        self.mark_artifact_operation(expected_output_count=1)
        self.command(
            "task_f_sample_excel",
            [NODE, runtime_script, excel_input, workbook, preview_dir, workbook_receipt],
            cwd=xlsx_runtime,
        )
        ensure_xlsx(workbook, "任务F 100条查询结果")
        excel_receipt = read_json(workbook_receipt)
        if excel_receipt.get("status") != "passed" or int(excel_receipt.get("resultRowCount") or 0) != len(sample_rows):
            raise PatrolError("任务F样例Excel生成或行数核验失败")
        task_f.update({
            "sampleWorkbook": str(workbook),
            "sampleWorkbookReceipt": str(workbook_receipt),
            "sampleWorkbookPreviewDir": str(preview_dir),
        })
        self.state["stages"]["taskF"].update(task_f)
        self._save_state()

        report = self.delivery / f"OpenSMS_任务F_样例查询_{self.run_id}.md"
        lines = [
            "# OpenSMS 任务F样例查询报告",
            "",
            f"- 运行编号：{self.run_id}",
            "- 浏览器：BitBrowser 430（后台静默）",
            f"- 全部数据库未完成数：{int(task_f.get('fullPendingCount') or 0)}",
            f"- 本次样例查询数：{len(sample_rows)}",
            f"- 实际线程数：{int(task_f.get('effectiveWorkers') or 0)}",
            f"- 状态分布：{json.dumps(task_f.get('lookupStatusCounts') or {}, ensure_ascii=False)}",
            "",
            "| 手机号 | 卡槽 | OPR | 原始ICCID | Purchase Date | Act. Request Date | Activation Date | SIM Number | SKU | 截止日 | 状态 | 线程 | 异常 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
        for row in sample_rows:
            values = [
                row["phone"], row["slot"], row["opr"], row["sourceIccid"],
                row["purchaseDate"], row["actRequestDate"], row["activationDateRaw"] or row["activationDate"],
                row["simNumber"], row["sku"], row["cutoffDate"], row["status"],
                str(row["workerId"]), row["error"],
            ]
            lines.append("| " + " | ".join(value.replace("|", "\\|").replace("\n", " ") for value in values) + " |")
        lines.extend(["", "## 完整OpenSMS源数据", ""])
        for row in sample_rows:
            lines.extend([
                f"### {row['queryIccid']}",
                "",
                "```json",
                json.dumps(row["sourceOccurrences"] or [row["sourceData"]], ensure_ascii=False, indent=2),
                "```",
                "",
            ])
        lines.extend([
            "",
            "## 回执",
            "",
            f"- Kooda完整库存：{task_f.get('inventory')}",
            f"- 完整待查清单：{task_f.get('fullPending')}",
            f"- 本次样例清单：{task_f.get('pending')}",
            f"- 查询回执：{task_f.get('lookupReceipt')}",
            f"- 数据库导入回执：{task_f.get('importReceipt')}",
            f"- 数据库快照：{task_f.get('databaseSnapshot')}",
            f"- 100条查询Excel：{workbook}",
            f"- Excel生成与结构校验回执：{workbook_receipt}",
            f"- 数据库全量截止日模板：{task_f.get('deadlineWorkbook')}",
            f"- 截止日模板核验回执：{task_f.get('deadlineExportReceipt')}",
            "",
            "本模式只验证任务F样例，不运行普通巡检及任务B、C、D、E，也不生成部分日期的业务工作簿。",
        ])
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = {
            "status": "success",
            **execution_contract(),
            "runId": self.run_id,
            "mode": "task-f-sample-preview",
            "taskF": task_f,
            "sampleCount": len(sample_rows),
            "sampleRows": sample_rows,
            "taskFWorkbook": str(workbook),
            "taskFWorkbookReceipt": str(workbook_receipt),
            "taskFWorkbookPreviewDir": str(preview_dir),
            "report": str(report),
            "database": str(TASK_F_DB),
            "databaseSnapshot": task_f.get("databaseSnapshot"),
            "state": str(self.state_path),
        }
        summary_path = self.delivery / f"OpenSMS_任务F_样例查询_摘要_{self.run_id}.json"
        atomic_json(summary_path, result)
        result["summaryPath"] = str(summary_path)
        return result

    def finish_task_f_only(self, task_f: dict[str, Any]) -> dict[str, Any]:
        """Finish an authorized full Task F run without unrelated patrol stages."""
        report = self.delivery / f"OpenSMS_任务F_全量查询_{self.run_id}.md"
        lines = [
            "# OpenSMS 任务F全量查询报告",
            "",
            f"- 运行编号：{self.run_id}",
            f"- Kooda唯一ICCID：{int(task_f.get('inventoryCount') or 0)}",
            f"- 数据库已查跳过：{int(task_f.get('alreadyQueriedCount') or 0)}",
            f"- 本轮待查/完成：{int(task_f.get('pendingCount') or 0)}/{sum(int(v or 0) for v in (task_f.get('lookupStatusCounts') or {}).values())}",
            f"- 状态分布：{json.dumps(task_f.get('lookupStatusCounts') or {}, ensure_ascii=False)}",
            f"- 请求/实际线程：{int(task_f.get('requestedWorkers') or 0)}/{int(task_f.get('effectiveWorkers') or 0)}",
            f"- 独立ASP.NET会话：{int(task_f.get('uniqueAspNetSessionCount') or 0)}",
            f"- 会话模式：{task_f.get('workerSessionMode') or '未知'}",
            f"- 数据库分批断点：每批{int(task_f.get('batchSize') or 0)}条，完成{int(task_f.get('completedBatchCount') or 0)}/{int(task_f.get('batchCount') or 0)}批，每批完成立即入库。",
            f"- 本轮导入终态/其中found/瞬时失败：{int(task_f.get('terminalImportedCount') or 0)}/{int(task_f.get('foundImportedCount') or 0)}/{int(task_f.get('transientErrorCount') or 0)}",
            f"- 截止日模板总记录/有日期/空日期：{int(task_f.get('deadlineRecordCount') or 0)}/{int(task_f.get('deadlineActivationCount') or 0)}/{int(task_f.get('deadlineMissingDateCount') or 0)}；列顺序固定为ICCID、截止日、激活日期。",
            "",
            "## 文件",
            "",
            f"- Kooda库存：{task_f.get('inventory')}",
            f"- 本轮待查清单：{task_f.get('pending')}",
            f"- 查询回执：{task_f.get('lookupReceipt')}",
            f"- 数据库导入回执：{task_f.get('importReceipt')}",
            f"- 数据库快照：{task_f.get('databaseSnapshot')}",
            f"- 进度回执：{task_f.get('progress')}",
            f"- 数据库全量截止日模板：{task_f.get('deadlineWorkbook')}",
            f"- 截止日模板核验回执：{task_f.get('deadlineExportReceipt')}",
            "",
            "本模式只执行任务F，不运行普通巡检及任务B、C、D、E。",
        ]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = {
            "status": "success",
            **execution_contract(),
            "runId": self.run_id,
            "mode": "task-f-full-only",
            "taskF": task_f,
            "finalWorkbook": task_f.get("deadlineWorkbook"),
            "report": str(report),
            "database": str(TASK_F_DB),
            "databaseSnapshot": task_f.get("databaseSnapshot"),
            "state": str(self.state_path),
        }
        summary_path = self.delivery / f"OpenSMS_任务F_全量查询_摘要_{self.run_id}.json"
        atomic_json(summary_path, result)
        result["summaryPath"] = str(summary_path)
        return result

    def run_task_f_debug(self, query_iccid: str) -> dict[str, Any]:
        if self.task_a_only:
            raise PatrolError("仅任务A模式禁止任务F诊断")
        output_dir = self.receipts / f"task_f_debug_{query_iccid}_{self.run_id}"
        result = self.command(
            "task_f_structured_debug",
            [NODE, SCRIPTS / "grabandgo_task_f_lookup.mjs", "--debug-one", query_iccid, output_dir],
            env={
                "BITBROWSER_CDP": self.args.cdp,
                "OPENSMS_FORCE_TASK_F": "1",
                "OPENSMS_ENABLE_TASK_F": "1",
                "OPENSMS_EXECUTE_TASK_F": "1",
            },
        )
        payload = {
            "status": "success",
            **execution_contract(),
            "runId": self.run_id,
            "mode": "task-f-structured-debug",
            "taskFDebug": result.payload,
            "state": str(self.state_path),
        }
        summary_path = self.delivery / f"OpenSMS_任务F_单条诊断_{query_iccid}_{self.run_id}.json"
        atomic_json(summary_path, payload)
        payload["summaryPath"] = str(summary_path)
        return payload

    def skip_task_f(self) -> dict[str, Any]:
        if self.task_a_only:
            return self.complete_stage("taskF", self.task_a_excluded_stage(), time.monotonic())
        prior = self.state.get("stages", {}).get("taskF")
        if prior:
            return prior
        return self.complete_stage(
            "taskF",
            {
                "executed": False,
                "status": "skipped_not_authorized",
                "loginChecked": False,
                "inventoryRead": False,
                "databaseWritten": False,
                "sensitiveIccidSubmittedCount": 0,
            },
            time.monotonic(),
        )

    def verify_card_retention_acquisition(self, acquire: dict[str, Any]) -> dict[str, Any]:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from services.opensms_document_api.pipeline.card_retention_report import load_source
        try:
            evidence = load_source(acquire.get("cardRetentionReceipt", ""), acquire.get("cardRetentionSha256", ""), self.run_id)
        except (ValueError, OSError) as exc:
            raise PatrolError(f"本轮任务A卡保留源未通过核验：{exc}") from exc
        local = acquire.get("processingCardRetention")
        if local and sha256(Path(local)) != evidence["sha256"]:
            raise PatrolError("卡保留本机输入SHA256不一致")
        return evidence

    def start_document(self, acquire: dict[str, Any]) -> None:
        self.verify_card_retention_acquisition(acquire)
        previous = self.reusable_stage("document", ("cardResult", "cardReceipt", "dispatchReceipt"))
        if previous:
            if self.chatr_google_date_fallback and not previous.get("googleSystemMissingSheet"):
                raise PatrolError("旧文档断点没有恢复后的Google差集证明，请启动新轮次")
            if previous.get("systemKoodaSha256") != acquire.get("systemKoodaSha256"):
                raise PatrolError("旧文档断点未绑定本轮系统Kooda快照，请启动新轮次")
            if (previous.get("cardRetentionReport") or {}).get("source_sha256") != acquire.get("cardRetentionSha256"):
                raise PatrolError("旧文档断点未绑定本轮卡保留记录")
            if previous.get("googleAssociationEnabled") is not False:
                raise PatrolError("旧文档断点未证明Google依赖已关闭，请启动新轮次")
            if self.chatr_google_date_fallback:
                self.verify_chatr_google_acquisition(acquire)
                proof = previous.get("googleChatrDateFallback") or {}
                if (previous.get("googleChatrDateFallbackEnabled") is not True
                        or proof.get("run_id") != self.run_id
                        or proof.get("snapshot_sha256") != acquire.get("chatrGoogleDatesSha256")
                        or proof.get("source_sha256") != acquire.get("chatrGoogleSourceSha256")):
                    raise PatrolError("旧文档断点未绑定本轮Chatr Google日期快照，请启动新轮次")
            return
        self.verify_system_kooda_acquisition(acquire)
        if self.chatr_google_date_fallback:
            self.verify_chatr_google_acquisition(acquire)
        date_cache = self.freeze_report_date_cache()
        self.mark_artifact_operation()
        output_dir = ROOT / "outputs" / f"opensms_patrol_{self.run_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            str(PYTHON), str(SCRIPTS / "opensms_process_document.py"),
            "--export", acquire["processingSource"],
            "--no-google",
            "--server-date", acquire["serverDate"],
            "--output-dir", str(output_dir),
            "--idempotency-key", self.run_id,
            "--force-local",
        ]
        log_path = self.run_dir / "document.log"
        self.document_log_handle = log_path.open("w", encoding="utf-8")
        self.document_started_monotonic = time.monotonic()
        self.state["documentStartedAt"] = now_hkt().isoformat()
        self.document_process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=self.child_environment({
                **({"OPENSMS_KO_UNACTIVATED_REQUIRED": "1"} if self.state.get("koUnactivatedSheetEnabled") is True else {}),
                "OPENSMS_SYSTEM_KOODA_PATH": acquire["processingSystemKooda"],
                "OPENSMS_SYSTEM_KOODA_SHA256": acquire["systemKoodaSha256"],
                "OPENSMS_REPORT_DATE_CACHE_PATH": date_cache["path"],
                "OPENSMS_REPORT_DATE_CACHE_SHA256": date_cache["sha256"],
                "OPENSMS_CARD_RETENTION_REPORT_REQUIRED": "1",
                "OPENSMS_CARD_RETENTION_REPORT_PATH": acquire["processingCardRetention"],
                "OPENSMS_CARD_RETENTION_REPORT_SHA256": acquire["cardRetentionSha256"],
                **({"OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED": "1",
                    "OPENSMS_CHATR_GOOGLE_DATES_PATH": acquire["processingChatrGoogleDates"],
                    "OPENSMS_CHATR_GOOGLE_DATES_SHA256": acquire["chatrGoogleDatesSha256"]}
                   if self.chatr_google_date_fallback else {}),
            }),
            stdout=self.document_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.state["documentLog"] = str(log_path)
        self._save_state()

    def freeze_report_date_cache(self) -> dict[str, Any]:
        """One read-only SQLite snapshot before this run starts document work.

        A-only may consume this frozen local input, but never starts F workers.
        The document process never opens a DB that a background F may update.
        """
        prior = self.state.get("reportDateCache")
        if prior:
            if not Path(prior["path"]).is_file() or sha256(Path(prior["path"])) != prior["sha256"]:
                raise PatrolError("本轮日期缓存已缺失或变化，拒绝重新冻结混用日期")
            return prior
        path = self.receipts / f"opensms_report_date_cache_{self.run_id}.json"
        self.command("freeze_report_date_cache", [
            PYTHON, SCRIPTS / "opensms_report_date_cache.py", "freeze",
            "--db", TASK_F_DB, "--output", path, "--run-id", self.run_id,
        ])
        frozen = read_json(path)
        if (frozen.get("schemaVersion") != 1 or frozen.get("status") != "passed"
                or frozen.get("runId") != self.run_id
                or not isinstance(frozen.get("records"), list)
                or frozen.get("recordCount") != len(frozen["records"])):
            raise PatrolError("本轮只读日期缓存格式或运行编号核验失败")
        data = {"path": str(path), "sha256": sha256(path), "runId": self.run_id,
                "recordCount": frozen["recordCount"], "frozenBeforeDocument": True,
                "readOnly": True, "sourceDatabase": str(TASK_F_DB)}
        self.state["reportDateCache"] = data
        self._save_state()
        return data

    def start_background_task_f(self, document: dict[str, Any], acquire: dict[str, Any]) -> dict[str, Any]:
        """Publish a verified independent job; never join it before main delivery."""
        from opensms_task_f_background import prepare_and_start
        started = time.monotonic()
        prior = (self.state.get("stages") or {}).get("taskF") or {}
        if prior.get("background") is True:
            job = Path(prior.get("backgroundJob") or "")
            if not job.is_file() or sha256(job) != prior.get("backgroundJobSha256"):
                raise PatrolError("后台任务F已有任务清单校验失败，禁止重复派发")
            return prior
        try:
            data = prepare_and_start(self, document, acquire)
        except Exception as exc:
            # A local F launch failure cannot invalidate verified main data.
            failure_receipt = self.receipts / f"opensms_background_dispatch_failure_{self.run_id}.json"
            failure = {"schemaVersion": 1, "runId": self.run_id,
                       "createdAt": now_hkt().isoformat(), "status": "failed",
                       "reason": "background_launch_failed", "error": str(exc),
                       "executed": False, "workerSpawnConfirmed": False,
                       "mainReportIndependent": True, "cloudUploadAttempted": False,
                       "resultsWriteBackToDocument": False,
                       "documentPath": document.get("cardResult"),
                       "documentSha256": sha256(Path(document["cardResult"])),
                       "reportDateCacheSha256": (self.state.get("reportDateCache") or {}).get("sha256")}
            from opensms_task_f_background import publish_json
            publish_json(failure_receipt, failure)
            data = {"status": "failed", "background": True, "executed": False,
                    "mainReportIndependent": True, "error": str(exc),
                    "backgroundLaunchReceipt": str(failure_receipt),
                    "backgroundLaunchReceiptSha256": sha256(failure_receipt),
                    "resultsApplyNextExport": True, "resultsWriteBackToDocument": False,
                    "cloudUploadAttempted": False, "reason": "background_launch_failed"}
        return self.complete_stage("taskF", data, started)

    def verify_ko_unactivated_document(self, receipt: dict[str, Any]) -> dict[str, Any]:
        proof = (receipt.get("process") or {}).get("ko_unactivated_sheet") or {}
        if self.state.get("koUnactivatedSheetEnabled") is not True and not proof:
            return {}
        review = (receipt.get("verification") or {}).get("ko_unactivated_sheet_review") or {}
        cache = self.state.get("reportDateCache") or {}
        acquire = self.state.get("stages", {}).get("acquire") or {}
        if (proof.get("enabled") is not True or proof.get("status") != "passed"
                or proof.get("scope") != ("task_f_activation_missing_in_10day_export"
                    if acquire.get("systemKoodaInventory") and is_report_source(read_json(acquire["systemKoodaInventory"]))
                    else "task_f_activation_missing_in_current_system")
                or proof.get("sheet") != "ko系统有-未激活"
                or proof.get("slot_source") != ("current_10day_export.卡槽"
                    if proof.get("scope") == "task_f_activation_missing_in_10day_export"
                    else "current_system_inventory.slotIdentif")
                or proof.get("run_id") != self.run_id or not cache.get("sha256")
                or proof.get("snapshot_path") != cache.get("path")
                or proof.get("snapshot_sha256") != cache.get("sha256")
                or not acquire.get("processingSystemKooda") or not acquire.get("systemKoodaSha256")
                or proof.get("system_path") != str(Path(acquire["processingSystemKooda"]).resolve())
                or proof.get("system_sha256") != acquire["systemKoodaSha256"]
                or review.get("matches") is not True
                or any(review.get(key) != value for key, value in proof.items())):
            raise PatrolError("ko系统有-未激活缺少本轮F冻结库与系统交集独立核验，禁止业务写入")
        return proof

    def finish_document(self) -> dict[str, Any]:
        reused = self.reusable_stage("document", ("cardResult", "cardReceipt", "dispatchReceipt"))
        if reused:
            if reused.get("googleAssociationEnabled") is not False:
                raise PatrolError("旧文档断点未证明Google依赖已关闭，请启动新轮次")
            if self.chatr_google_date_fallback:
                acquire = self.state["stages"].get("acquire") or {}
                self.verify_chatr_google_acquisition(acquire)
                receipt = read_json(reused["cardReceipt"])
                receipt = receipt.get("cardSlotReport") or receipt
                proof = (receipt.get("process") or {}).get("google_chatr_date_fallback") or {}
                review = (receipt.get("verification") or {}).get("google_chatr_date_fallback_review") or {}
                expected = {"enabled": True, "status": "passed", "run_id": self.run_id,
                    "snapshot_sha256": acquire["chatrGoogleDatesSha256"],
                    "source_sha256": acquire["chatrGoogleSourceSha256"],
                    "scope": "chatr_blank_activation_and_cutoff_only"}
                if (reused.get("googleChatrDateFallbackEnabled") is not True
                        or any(proof.get(key) != value or review.get(key) != value for key, value in expected.items())
                        or reused.get("googleChatrDateFallback") != proof or review.get("matches") is not True):
                    raise PatrolError("旧文档断点缺少本轮Chatr日期独立核验，禁止进入业务写入")
                difference = (receipt.get("process") or {}).get("google_system_missing_sheet") or {}
                difference_review = (receipt.get("verification") or {}).get("google_system_missing_sheet_review") or {}
                if (not difference or reused.get("googleSystemMissingSheet") != difference
                        or difference.get("enabled") is not True or difference.get("status") != "passed"
                        or difference.get("scope") != "google_slots_absent_from_current_system_export"
                        or difference.get("run_id") != self.run_id
                        or difference.get("source_sha256") != acquire.get("chatrGoogleSourceSha256")
                        or difference.get("snapshot_sha256") != acquire.get("chatrGoogleDatesSha256")
                        or difference_review.get("matches") is not True
                        or any(difference_review.get(key) != value for key, value in difference.items())):
                    raise PatrolError("旧文档断点缺少本轮Google差集独立核验，禁止进入业务写入")
            acquire = self.state["stages"].get("acquire") or {}
            self.verify_card_retention_acquisition(acquire)
            receipt = read_json(reused["cardReceipt"])
            receipt = receipt.get("cardSlotReport") or receipt
            self.verify_ko_unactivated_document(receipt)
            proof = (receipt.get("process") or {}).get("card_retention_report") or {}
            review = (receipt.get("verification") or {}).get("card_retention_report_review") or {}
            if (proof.get("run_id") != self.run_id
                    or proof.get("source_sha256") != acquire.get("cardRetentionSha256")
                    or reused.get("cardRetentionReport") != proof or review.get("matches") is not True
                    or any(review.get(key) != value for key, value in proof.items())):
                raise PatrolError("旧文档断点缺少本轮卡保留独立核验，禁止进入业务写入")
            return reused
        if not self.document_process:
            raise PatrolError("文档进程未启动")
        started = time.monotonic()
        return_code = self.document_process.wait()
        self.document_log_handle.close()
        log_path = Path(self.state["documentLog"])
        text = log_path.read_text(encoding="utf-8")
        try:
            payload = parse_last_json(text)
        except (ValueError, PatrolError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        measured_start = getattr(self, "document_started_monotonic", None)
        duration = round(time.monotonic() - measured_start, 3) if measured_start is not None else payload.get("duration_seconds")
        timing = {"command": "document", "startedAt": self.state.get("documentStartedAt"),
                  "durationSeconds": duration, "exitCode": return_code,
                  "documentProcessSeconds": payload.get("duration_seconds"),
                  "documentJoinSeconds": round(time.monotonic() - started, 3)}
        self.state["documentProcessTiming"] = timing
        self.state.setdefault("commandTimings", []).append(timing)
        if return_code or payload.get("result") != "成功":
            self.state["documentFailure"] = {
                "exitCode": return_code, "error": payload.get("error"),
                "localFailure": payload.get("local_failure"),
                "dispatchReceipt": payload.get("dispatch_receipt_path"), "log": str(log_path),
            }
        self._save_state()
        if return_code:
            detail = payload.get("error") or "未返回有效结构化错误"
            raise PatrolError(f"文档生成失败，退出码{return_code}；{detail}；日志: {log_path}")
        if payload.get("result") != "成功":
            raise PatrolError(f"文档生成未成功: {payload.get('error') or payload.get('result')}")
        receipt_payload = read_json(payload["receipt_path"])
        receipt_payload = receipt_payload.get("cardSlotReport") or receipt_payload
        google_dependency = (receipt_payload.get("process") or {}).get("google_dependency") or {}
        expected_google_dependency = {
            "enabled": False, "status": "skipped", "reason": "user_disabled",
            "unused_port_device_scope": "system_source_only",
        }
        if (google_dependency.get("enabled") is not False or any(
                google_dependency.get(key) != value for key, value in expected_google_dependency.items()
        )):
            raise PatrolError("文档回执未证明Google依赖已关闭，禁止进入业务写入")
        chatr_dates = (receipt_payload.get("process") or {}).get("google_chatr_date_fallback") or {}
        if self.chatr_google_date_fallback:
            acquire = self.state["stages"]["acquire"]
            review = (receipt_payload.get("verification") or {}).get("google_chatr_date_fallback_review") or {}
            expected = {"enabled": True, "status": "passed", "run_id": self.run_id,
                "snapshot_sha256": acquire["chatrGoogleDatesSha256"],
                "source_sha256": acquire["chatrGoogleSourceSha256"],
                "scope": "chatr_blank_activation_and_cutoff_only"}
            if (any(chatr_dates.get(key) != value or review.get(key) != value for key, value in expected.items())
                    or review.get("matches") is not True):
                raise PatrolError("Chatr日期补全未通过独立来源核验，禁止进入业务写入")
        card_source = Path(payload["output_path"])
        ko_unactivated = self.verify_ko_unactivated_document(receipt_payload)
        difference = (receipt_payload.get("process") or {}).get("google_system_missing_sheet") or {}
        if self.chatr_google_date_fallback:
            difference_review = (receipt_payload.get("verification") or {}).get("google_system_missing_sheet_review") or {}
            if (difference.get("run_id") != self.run_id
                    or difference.get("enabled") is not True or difference.get("status") != "passed"
                    or difference.get("scope") != "google_slots_absent_from_current_system_export"
                    or difference.get("source_sha256") != acquire.get("chatrGoogleSourceSha256")
                    or difference.get("snapshot_sha256") != acquire.get("chatrGoogleDatesSha256")
                    or difference_review.get("matches") is not True
                    or any(difference_review.get(key) != value for key, value in difference.items())):
                raise PatrolError("Google系统差集未通过独立来源及逐行核验，禁止进入业务写入")
        retention_proof = (receipt_payload.get("process") or {}).get("card_retention_report") or {}
        retention_review = (receipt_payload.get("verification") or {}).get("card_retention_report_review") or {}
        acquire = self.state["stages"]["acquire"]
        if (retention_proof.get("source_sha256") != acquire.get("cardRetentionSha256")
                or retention_proof.get("run_id") != self.run_id
                or retention_review.get("matches") is not True
                or any(retention_review.get(key) != value for key, value in retention_proof.items())):
            raise PatrolError("任务A需要保留时间未通过独立来源及逐行核验")
        ensure_xlsx(card_source, "卡槽关联结果")
        card_result = self.delivery / f"OpenSMS近10日报表巡检_生成结果_{self.run_id}.xlsx"
        shutil.copy2(card_source, card_result)
        card_receipt = self.copy_artifact(payload["receipt_path"], f"opensms_card_slot_report_{self.run_id}.json")
        dispatch_receipt = self.copy_artifact(payload["dispatch_receipt_path"], f"opensms_document_dispatch_{self.run_id}.json")
        data = {
            "cardResult": str(card_result),
            "cardReceipt": str(card_receipt),
            "dispatchReceipt": str(dispatch_receipt),
            "processingMode": payload.get("processing_mode"),
            "durationSeconds": payload.get("duration_seconds"),
            "documentProcessSeconds": payload.get("duration_seconds"),
            "documentJoinSeconds": round(time.monotonic() - started, 3),
            "systemKoodaSha256": (self.state["stages"].get("acquire") or {}).get("systemKoodaSha256"),
            "reportDateCachePath": (self.state.get("reportDateCache") or {}).get("path"),
            "reportDateCacheSha256": (self.state.get("reportDateCache") or {}).get("sha256"),
            "googleAssociationEnabled": False,
            "googleDisabledReason": "user_disabled",
            "googleDependency": google_dependency,
            "googleChatrDateFallbackEnabled": self.chatr_google_date_fallback,
            "googleChatrDateFallback": chatr_dates,
            "googleSystemMissingSheet": difference,
            "koUnactivatedSheet": ko_unactivated,
            "cardRetentionReport": retention_proof,
        }
        return self.complete_stage("document", data, started)

    def analyze_and_candidates(self, acquire: dict[str, Any]) -> dict[str, Any]:
        reused = self.reusable_stage("candidates", ("analysis", "routingManifest", "eRecoverySummary"))
        if reused:
            return reused
        started = time.monotonic()
        source = acquire["processingSource"]
        scan_cache = acquire["scanCache"]
        active = read_json(acquire["activeCarrierReceipt"])["resolved"]
        names = {group: row["name"] for group, row in active.items()}
        run_time = datetime.strptime(self.run_id[:15], "%Y%m%dT%H%M%S").replace(tzinfo=TZ).isoformat()
        main_argv: list[str | Path] = [
            PYTHON, ROOT / "opensms_generate_opr_zero_candidates.py", source,
            "--out-dir", self.receipts,
            "--a1-carrier", names["A1"], "--a11-carrier", names["A11"],
            "--a12-carrier", names["A12"], "--a3-carrier", names["A3"],
            "--a21-carrier", names["A21"], "--b1-carrier", names["B1"],
            "--b11-carrier", names["B11"], "--b12-carrier", names["B12"],
            "--b1v-carrier", names["B1V"], "--b11v-carrier", names["B11V"],
            "--b12v-carrier", names["B12V"],
            "--c2-carrier", names["C2"], "--e-carrier", names["E-T"],
            "--e-c-carrier", names["E-C"], "--c2-daily-state-dir", C2_STATE_ROOT,
            "--c2-statistics-receipt", acquire["balance"],
            "--now-bj", run_time,
            "--scan-cache", scan_cache,
        ]
        previous = sorted(SNAPSHOTS.glob("opensms_c2_previous_day_reply_rate_lt5_to_a11_*_candidates.json"))
        if previous:
            main_argv.extend(["--c2-reconcile-from-candidate", previous[-1]])
        routing_manifest = self.receipts / f"opensms_carrier_routing_{self.run_id}_manifest.json"
        canonical_aliases = (
            "A1", "A11", "A12", "A3",
            "B1", "B11", "B12", "B1V", "B11V", "B12V", "C2",
        )
        e_summary = self.receipts / f"opensms_e_reply_recovery_{self.run_id}.json"
        e_argv: list[str | Path] = [
            PYTHON, ROOT / "opensms_generate_e_reply_recovery.py", source,
            "--tag", self.run_id, "--out", e_summary,
            "--e-carrier", names["E-T"], "--e-carrier", names["E-C"],
            "--scan-cache", scan_cache,
        ]
        for alias in canonical_aliases:
            e_argv.extend(["--canonical-carrier", f"{alias}={names[alias]}"])

        def main_job() -> CommandResult:
            if routing_manifest.is_file():
                return CommandResult(read_json(routing_manifest), "", "", 0.0)
            return self.command("main_candidates", main_argv)

        # These are CPU/read-only tasks over the same immutable fresh export.
        # A-zone latest-day split is part of the single main-routing workbook
        # pass, so analysis, all carrier candidates and E recovery can run in
        # parallel without a second full Excel scan.
        from opensms_local_jobs import run_local_jobs
        from opensms_scope_comparison import build as build_scope_comparison
        local_workers = max(1, min(self.args.analysis_workers, 4, os.cpu_count() or 1))
        local_results, scheduling = run_local_jobs({
            "mainRouting": main_job,
            "eRecovery": lambda: self.command("e_recovery", e_argv),
            "analysis": lambda: self.command("analysis",
                [
                    PYTHON, ROOT / "opensms_analyze_current.py", source,
                    "--export-method", "后台静默新鲜导出", "--fast",
                    "--scan-cache", scan_cache,
                    "--skip-legacy-same-time",
                ]),
            # Scope statistics use only the bound export/cache/date receipt,
            # not analysis/candidates or any mutable workbook. Its independent
            # re-read/recalculation remains inside build_scope_comparison.
            "scopeComparison": lambda: build_scope_comparison(
                scan_cache, source, acquire["exportReceipt"], self.run_id,
                acquire["serverDate"], ARTIFACTS / "state" / "scope_reply_history"),
        }, workers=local_workers)
        main_result = local_results["mainRouting"]
        routing_manifest = Path(main_result.payload.get("manifest") or routing_manifest)
        routing = read_json(routing_manifest)
        analysis = local_results["analysis"]
        e_recovery = local_results["eRecovery"]

        analysis_path = self.copy_artifact(analysis.payload["summary"], f"opensms_current_analysis_{self.run_id}.json")
        summary_data = read_json(analysis_path)
        if summary_data.get("latestDate") != acquire["serverDate"][5:]:
            raise PatrolError("源报表最新统计日与后台服务器日期不一致")
        summary_data["scopeComparison"] = local_results["scopeComparison"]
        atomic_json(analysis_path, summary_data)
        e_groups = []
        for group in e_recovery.payload.get("groups") or []:
            copied = self.copy_artifact(group["path"])
            e_groups.append({**group, "path": str(copied)})

        c2_state = routing.get("c2DailyExecutionReceipt")
        if c2_state and Path(c2_state).is_file():
            self.copy_artifact(c2_state, Path(c2_state).name)
        data = {
            "analysis": str(analysis_path),
            "routingManifest": str(routing_manifest),
            "aSplitManifest": str(routing_manifest),
            "eRecoverySummary": str(e_summary),
            "eRecoveryGroups": e_groups,
            "subtaskDurations": {
                "analysis": analysis.duration_seconds,
                "mainRouting": main_result.duration_seconds,
                "aZoneSplit": 0.0,
                "eRecovery": e_recovery.duration_seconds,
            },
            "analysisConcurrency": local_workers,
            "localJobScheduling": scheduling,
            "candidateWorkbookPasses": 1,
            "candidateProcessWorkbookReopens": 0,
            "sharedScanCacheUsed": True,
            "sharedScanCache": scan_cache,
            "documentProcessingConcurrentWithAnalysis": True,
            "vZoneCandidatesMergedIntoMainWorkbookPass": True,
            "liveCarrierWritesRemainSerial": True,
        }
        return self.complete_stage("candidates", data, started)

    def candidate_specs(self, candidates: dict[str, Any]) -> list[dict[str, Any]]:
        routing = read_json(candidates["routingManifest"])
        specs = []
        for route in routing.get("routePriority") or []:
            item = (routing.get("outputs") or {}).get(route)
            if item:
                specs.append({"route": route, "candidate": item["path"], "target": item["target"]})
        for index, group in enumerate(candidates.get("eRecoveryGroups") or [], start=1):
            specs.append({
                "route": f"e_reply_recovery_{index:02d}",
                "candidate": group["path"],
                "target": group["target"],
                "title": f"E-T/E-C休息区有回复后恢复到{group['target']}",
            })
        return specs

    def run_write(self, spec: dict[str, Any]) -> dict[str, Any]:
        key = spec["route"]
        if key.startswith("task_d_retain_") or re.match(r"^B14(?:-|$)", str(spec.get("target") or "").strip(), re.I):
            raise PatrolError("转入B14保留区工作已删除，禁止复用旧分组或提交运营商修改")
        prior = self.state["writeGroups"].get(key)
        if prior and all(Path(prior[name]).is_file() for name in ("candidate",) if prior.get(name)):
            if prior.get("result") == "no_candidate":
                return prior
            if all(Path(prior[name]).is_file() for name in ("validated", "write", "verify")):
                return prior
        candidate = Path(spec["candidate"]).resolve()
        count = count_candidate(candidate)
        base = {
            "route": key,
            "title": spec.get("title") or TITLE_BY_ROUTE.get(key, key),
            "candidate": str(candidate),
            "target": spec["target"],
            "candidateCount": count,
        }
        if count == 0:
            result = {**base, "result": "no_candidate", "backendValid": 0, "actualWrite": 0}
            self.state["writeGroups"][key] = result
            self._save_state()
            return result
        label = re.sub(r"[^0-9A-Za-z_.-]+", "_", f"{self.run_id}_{key}")
        env = {"BITBROWSER_CDP": self.args.cdp, "OPENSMS_FORCE_RAW_CDP": "1"}
        if key == "restore_unexpired_to_a11":
            if not getattr(self.args, "restore_unexpired_to_a11", False):
                raise PatrolError("本轮未明确授权未到期恢复A11")
            env["OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE"] = "1"
        if key.startswith("task_b_"):
            env["OPENSMS_FORCE_TASK_B"] = "1"
        if key.startswith("task_c_"):
            env["OPENSMS_FORCE_TASK_C"] = "1"
        if key.startswith("task_d_"):
            env["OPENSMS_FORCE_TASK_D"] = "1"
            env["OPENSMS_ENABLE_TASK_D"] = "1"
            env["OPENSMS_EXECUTE_TASK_D"] = "1"
        written = self.command(
            f"set_{label}",
            [NODE, ROOT / "opensms_set_carrier_candidates.mjs", candidate, spec["target"], label],
            env=env,
        )
        write_path = self.copy_artifact(written.payload["path"])
        validated_path = self.copy_artifact(written.payload["validatedCandidatePath"])
        if written.payload.get("originBackupPath"):
            self.copy_artifact(written.payload["originBackupPath"])
        before = written.payload.get("before") or {}
        after = written.payload.get("after") or {}
        if int(before.get("missingCount") or 0) or int(before.get("duplicateBackendCount") or 0):
            raise PatrolError(f"{key}后台记录缺失或重复，已停止")
        if int(after.get("missingCount") or 0) or int(after.get("duplicateBackendCount") or 0):
            raise PatrolError(f"{key}写后后台记录缺失或重复，已停止")
        verified = self.command(
            f"verify_{label}",
            [NODE, ROOT / "opensms_verify_carrier_candidates_readonly.mjs", validated_path, spec["target"], label],
            env=env,
        )
        verify_path = self.copy_artifact(verified.payload["path"])
        no_write = written.payload.get("noWrite") is True
        no_write_verified = (
            no_write
            and int(before.get("eligibleCount") or 0) == 0
            and written.payload.get("actualWriteCount") == 0
            and written.payload.get("updateResponses") == []
            and written.payload.get("after") is None
            and written.payload.get("independentReadbackRequired") is False
            and written.payload.get("independentReadbackPerformed") is False
            and count_candidate(validated_path) == 0
            and verified.payload.get("total") == 0
            and verified.payload.get("passed") is None
            and verified.payload.get("verificationStatus") == "not_required"
            and verified.payload.get("status") == "no_candidate"
            and verified.payload.get("independentReadbackPerformed") is False
        )
        if (no_write and not no_write_verified) or (not no_write and verified.payload.get("passed") is not True):
            raise PatrolError(f"{key}独立回读未通过")
        result = {
            **base,
            "result": "passed",
            "validated": str(validated_path),
            "write": str(write_path),
            "verify": str(verify_path),
            "backendValid": int(before.get("eligibleCount") or 0),
            "actualWrite": int(before.get("eligibleCount") or 0),
            "noWrite": no_write,
            "independentReadbackPerformed": not no_write,
            "stageReadbackRequired": no_write,
            "stageReadbackVerified": False,
            "alreadyTargetPhones": [row.get("phone") for row in before.get("alreadyTarget") or [] if row.get("phone")],
            "nonSourcePhones": [row.get("phone") for row in before.get("nonSource") or [] if row.get("phone")],
        }
        self.state["writeGroups"][key] = result
        self._save_state()
        from opensms_v_swap_daily import record_verified_group
        record_verified_group(STATE_ROOT / "v_group_swap_daily", self.run_id, result)
        return result

    def run_unexpired_restore(self, document: dict[str, Any], acquire: dict[str, Any]) -> None:
        from opensms_unexpired_a11_restore import build_candidate, ROUTE
        started = time.monotonic()
        target = read_json(acquire["activeCarrierReceipt"])["resolved"]["A11"]["name"]
        dates = self.command("restore_server_dates", [NODE, SCRIPTS / "opensms_read_server_dates_fast_raw.mjs"],
                             env={"BITBROWSER_CDP": self.args.cdp, "OPENSMS_FORCE_RAW_CDP": "1"}).payload
        if dates.get("success") is not True or any(
            (dates.get(key) or {}).get("serverDate") != acquire["serverDate"] for key in ("day", "phoneConfig")
        ):
            raise PatrolError("恢复前服务器日期跨日或读取不一致；未写入，需新鲜导出")
        paths = [self.copy_artifact(dates[key]["path"], f"restore_date_{key}_{self.run_id}.json")
                 for key in ("day", "phoneConfig")]
        candidate = self.receipts / f"opensms_{ROUTE}_{self.run_id}_candidates.json"
        plan = build_candidate(document, acquire, self.run_id, target, paths, candidate)
        self.state["unexpiredRestorePlan"] = {"candidate": str(candidate), "count": plan["count"],
                                              "bySource": plan["bySource"], "excludedCount": plan["excludedCount"]}
        self._save_state()
        result = self.run_write({"route": ROUTE, "candidate": str(candidate), "target": target,
                                 "title": "明确授权：隔离/过期区未到截止日恢复A11"})
        self.complete_stage("unexpiredRestore", {**result, "bySource": plan["bySource"],
                            "excludedCount": plan["excludedCount"], "normalWritesSkipped": True}, started)
        self.complete_stage("standardWrites", {"groupCount": 0, "candidateCount": 0, "actualWrite": 0,
                            "status": "skipped", "reason": "explicit_unexpired_a11_correction_only"}, time.monotonic())

    def run_standard_writes(self, candidates: dict[str, Any]) -> list[dict[str, Any]]:
        started = time.monotonic()
        results = [self.run_write(spec) for spec in self.candidate_specs(candidates)]
        self.complete_stage(
            "standardWrites",
            {
                "groupCount": len(results),
                "candidateCount": sum(item["candidateCount"] for item in results),
                "actualWrite": sum(item.get("actualWrite", 0) for item in results),
            },
            started,
        )
        return results

    def run_task_b(self, document: dict[str, Any], acquire: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.authorization["taskB"]:
            return []
        reused = self.reusable_stage("taskB")
        if reused:
            return []
        started = time.monotonic()
        env = {
            "BITBROWSER_CDP": self.args.cdp,
            "OPENSMS_FORCE_RAW_CDP": "1",
            "OPENSMS_FORCE_TASK_B": "1",
        }
        manifest_path = self.receipts / f"opensms_task_b_{self.run_id}_manifest.json"
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
        else:
            for stem in (
                "opensms_task_b_expired_to_offline_c",
                "opensms_task_b_expired_to_offline_t",
                "opensms_task_b_tomorrow_fast_to_isolation_c",
                "opensms_task_b_tomorrow_fast_to_isolation_t",
            ):
                partial = self.receipts / f"{stem}_{self.run_id}_candidates.json"
                if partial.is_file():
                    partial.unlink()
            manifest = self.command(
                "task_b_candidates",
                [
                    PYTHON, SCRIPTS / "opensms_generate_task_b_candidates.py", document["cardResult"],
                    "--server-date", acquire["serverDate"], "--tag", self.run_id, "--output-dir", self.receipts,
                ],
                env=env,
            ).payload
        if (manifest.get("sourceCarrierScope") != "all_carriers_v1"
                or manifest.get("sourceCarrierExclusions") != []
                or manifest.get("sourceCarrierCoveragePassed") is not True
                or manifest.get("unconfiguredCarrierPolicy") != "skip_no_business_write_v1"):
            raise PatrolError("任务B断点仍为旧来源保护范围或缺逐运营商覆盖证明；停止，不重放旧清单")
        from opensms_parallel_audit import PathResolver, review_task_b_source_coverage
        try:
            errors, _ = review_task_b_source_coverage(
                {"serverDate": acquire["serverDate"], "taskBInputWorkbook": document["cardResult"]}, manifest,
                [{"candidate": path} for path in manifest.get("candidatePaths") or []],
                PathResolver(manifest_path, self.receipts),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PatrolError(f"任务B写前独立全来源覆盖核验失败：{exc}") from exc
        if errors:
            raise PatrolError("任务B写前独立全来源覆盖核验失败：" + "；".join(errors[:10]))
        results = []
        for candidate in manifest.get("candidatePaths") or []:
            payload = read_json(candidate)
            results.append(self.run_write({
                "route": payload["route"], "candidate": candidate,
                "target": payload["targetCarrierName"],
            }))
        self.complete_stage(
            "taskB",
            {
                "executed": True,
                "manifest": str(manifest_path),
                "inputWorkbook": document["cardResult"],
                "inputWorkbookSha256": sha256(Path(document["cardResult"])),
                "groupCount": len(results),
                "actualWrite": sum(item.get("actualWrite", 0) for item in results),
                "writeExecutionSerial": True,
                "sourceCarrierScope": manifest.get("sourceCarrierScope"),
                "sourceCarrierCoverage": manifest.get("sourceCarrierCoverage"),
                "sourceCarrierCoveragePassed": manifest.get("sourceCarrierCoveragePassed"),
                "unconfiguredCarrierSkippedCount": manifest.get("unconfiguredCarrierSkippedCount"),
                **anomaly_summary(manifest),
                "retainedSourceCarrierNames": manifest.get("retainedSourceCarrierNames") or [],
                "retainedSourceExcludedCount": manifest.get("retainedSourceExcludedCount"),
                "retainedSourceExcludedCounts": manifest.get("retainedSourceExcludedCounts") or {},
                "expiredRetainedSourceExcludedCount": manifest.get("expiredRetainedSourceExcludedCount"),
                "tomorrowRetainedSourceExcludedCount": manifest.get("tomorrowRetainedSourceExcludedCount"),
            },
            started,
        )
        return results

    def run_task_c(self, document: dict[str, Any], acquire: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.authorization["taskC"]:
            return []
        reused = self.reusable_stage("taskC")
        if reused:
            return []
        started = time.monotonic()
        env = {
            "BITBROWSER_CDP": self.args.cdp,
            "OPENSMS_FORCE_RAW_CDP": "1",
            "OPENSMS_FORCE_TASK_C": "1",
        }
        manifest_path = self.receipts / f"opensms_task_c_{self.run_id}_manifest.json"
        if manifest_path.is_file():
            task_c_manifest = read_json(manifest_path)
        else:
            for stem in (
                "opensms_task_c_kooda_4day_lt5_to_swap",
                "opensms_task_c_chatr_unchanged_4day_lt4_to_swap",
            ):
                partial = self.receipts / f"{stem}_{self.run_id}_candidates.json"
                if partial.is_file():
                    partial.unlink()
            target = self.command("task_c_target", [NODE, SCRIPTS / "opensms_resolve_task_c_target.mjs"], env=env)
            if not target.payload.get("success"):
                raise PatrolError("任务C换号区C/换号区T目标未分别唯一解析")
            targets = target.payload["targets"]
            task_c_manifest = self.command(
                "task_c_candidates",
                [
                    PYTHON, SCRIPTS / "opensms_generate_task_c_candidates.py", document["cardResult"],
                    "--target-carrier-c", targets["C"]["name"],
                    "--target-carrier-t", targets["T"]["name"],
                    "--server-date", acquire["serverDate"],
                    "--tag", self.run_id, "--output-dir", self.receipts,
                ],
                env=env,
            ).payload
        results = []
        for candidate in (task_c_manifest["koodaCandidatePath"], task_c_manifest["chatrCandidatePath"]):
            payload = read_json(candidate)
            results.append(self.run_write({
                "route": payload["route"], "candidate": candidate,
                "target": payload["targetCarrierName"],
            }))
        self.complete_stage(
            "taskC",
            {
                "executed": True,
                "manifest": str(manifest_path),
                "inputWorkbook": document["cardResult"],
                "inputWorkbookSha256": sha256(Path(document["cardResult"])),
                "groupCount": len(results),
                "actualWrite": sum(item.get("actualWrite", 0) for item in results),
                "writeExecutionSerial": True,
                "unconfiguredCarrierSkippedCount": task_c_manifest.get("unconfiguredCarrierSkippedCount"),
                **anomaly_summary(task_c_manifest),
            },
            started,
        )
        return results

    def task_d_preparation_location(self, checkpoint: dict[str, Any]) -> tuple[Path, Path, str]:
        """Explicit pre-submission resume preserves every failed preparation file."""
        filename = f"opensms_task_d_{self.run_id}_manifest.json"
        canonical = self.receipts / filename
        recorded = checkpoint.get("manifest")
        if recorded:
            manifest = Path(recorded).resolve()
            root = self.state_path.parent if getattr(self.args, "continue_task_d", False) else self.receipts.resolve()
            if manifest.name != filename or not manifest.is_relative_to(root):
                raise PatrolError("任务D准备断点路径不属于本轮，拒绝续跑")
            if manifest.is_file():
                return manifest, manifest.parent, "task_d_prepare"
            raise PatrolError("任务D已登记准备清单缺失，拒绝重新准备或提交")
        if canonical.is_file():
            return canonical, self.receipts, "task_d_prepare"
        failed_path = self.receipts / f"opensms_task_d_mapping_{self.run_id}_receipt.json"
        if not failed_path.is_file():
            return canonical, self.receipts, "task_d_prepare"
        failed = read_json(failed_path)
        if (not getattr(self.args, "resume_state", None)
                or getattr(self.args, "authorization_source", None) != "current-user"
                or not self.authorization.get("taskD")
                or failed.get("runId") != self.run_id or failed.get("task") != "D"
                or failed.get("readOnly") is not True or failed.get("status") != "failed"
                or any(checkpoint.get(key) for key in (
                    "swapExpiryReceipt", "retentionExpiryReceipt", "businessCompleted", "carrierRoutes"))
                or any(self.receipts.glob(f"opensms_task_d_*_{self.run_id}_progress.json"))
                or any(self.receipts.glob(f"opensms_task_d_swap_zero_{self.run_id}_receipt.json"))
                or any(self.receipts.glob(f"opensms_task_d_retention_verified_{self.run_id}_receipt.json"))):
            raise PatrolError("任务D已有准备证据，只有明确授权且证明尚未提交通知的失败断点可重新准备")
        attempt = str(time.time_ns())
        directory = self.receipts / "task_d_preparation_attempts" / attempt
        directory.mkdir(parents=True, exist_ok=False)
        checkpoint.setdefault("preparationResumeHistory", []).append({
            "createdAt": now_hkt().isoformat(), "readOnly": True,
            "businessWrites": 0, "notificationSubmitted": False,
            "failedMappingReceipt": str(failed_path), "failedMappingSha256": sha256(failed_path),
            "outputDirectory": str(directory), "command": f"task_d_prepare_resume_{attempt}",
        })
        self._save_state()
        return directory / filename, directory, f"task_d_prepare_resume_{attempt}"

    def run_task_d(self, document: dict[str, Any], acquire: dict[str, Any]) -> dict[str, Any]:
        if not self.authorization["taskD"]:
            raise PatrolError("任务D缺少当前真实用户双重授权，拒绝执行")
        if any(str(route).startswith("task_d_retain_") for route in self.state.get("writeGroups", {})):
            raise PatrolError("任务D旧断点含转入保留区分组；功能已删除，不得继续或重放")
        reused = self.reusable_stage("taskD", ("manifest", "swapExpiryReceipt", "retentionExpiryReceipt"))
        if reused:
            if (reused.get("retentionCarrierTransferEnabled") is not False
                    or not population_statistics_removed(reused)
                    or reused.get("retentionPopulationReadExecuted") is not False
                    or (reused.get("notificationSummary") or {}).get("status") != "passed"
                    or any(reused.get(key) is not None for key in (
                        "retentionSummary", "retentionPopulationReceipt", "retentionSummaryReceipt", "retentionGroupsReceipt",
                    ))):
                raise PatrolError("旧任务D断点不符合已删除保留区统计契约，禁止重放写入")
            return reused
        started = time.monotonic()
        checkpoint = self.state.setdefault("taskDCheckpoint", {})
        env = {
            "BITBROWSER_CDP": self.args.cdp,
            "OPENSMS_FORCE_RAW_CDP": "1",
            "OPENSMS_FORCE_TASK_D": "1",
            "OPENSMS_ENABLE_TASK_D": "1",
            "OPENSMS_EXECUTE_TASK_D": "1",
            "OPENSMS_PYTHON": str(PYTHON),
        }
        manifest_path, preparation_dir, preparation_label = self.task_d_preparation_location(checkpoint)
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
        else:
            manifest = self.command(
                preparation_label,
                [
                    NODE,
                    SCRIPTS / "opensms_prepare_task_d.mjs",
                    document["cardResult"],
                    acquire["serverDate"],
                    self.run_id,
                    preparation_dir,
                ],
                env=env,
            ).payload
        if (
            manifest.get("task") != "D"
            or manifest.get("forceGate") is not True
            or manifest.get("automationGate") is not True
        ):
            raise PatrolError("任务D准备清单门禁无效")
        if (manifest.get("retentionCarrierTransferEnabled") is not False
                or manifest.get("retentionCarrierTransferStatus") != "removed_by_user"
                or any(manifest.get(key) for key in (
                    "chatrCandidatePath", "koodaCandidatePath", "carrierCandidateCount",
                    "chatrCandidateCount", "koodaCandidateCount"))
                or checkpoint.get("carrierRoutes")):
            raise PatrolError("任务D转入保留区工作已删除，拒绝旧运营商候选或断点；不得重放写入")
        continuation_proof = (self.state.get('taskDContinuationAttempts') or [{}])[-1].get('preflight', {})
        if continuation_proof.get('preparationOnly') is True:
            approved = {(row['cardNo'], row['backendId']) for row in continuation_proof['approvedAnomalyRows']}
            actual = {(row['cardNo'], row['backendId']) for row in manifest.get('dataAnomalySkippedRows') or []}
            if manifest.get('dataAnomalyPolicy') != 'skip_row_data_anomalies_v1' or not actual.issubset(approved):
                raise PatrolError('TASK_D_PREPARATION_CONTINUATION_EXCLUSION_SCOPE_CHANGED: 未提交通知')
        checkpoint["manifest"] = str(manifest_path)
        checkpoint["retentionPartition"] = {
            key: manifest[key] for key in TASK_D_RETENTION_PARTITION_FIELDS if key in manifest
        }
        checkpoint["retentionPartition"]["retentionReceipt"] = manifest["retentionReceiptPath"]
        self._save_state()

        def execute_expiry(phase: str, notification_path: str) -> dict[str, Any]:
            resume_pre_submit = False
            continuation = (self.state.get("taskDContinuationAttempts") or [{}])[-1]
            continuing = getattr(self.args, "continue_task_d", False)
            continuing_submissions = continuing and continuation['preflight'].get('preparationOnly') is not True
            if continuing_submissions and phase == "swap_zero":
                # Preflight bound and independently checked this completed branch.
                return {**read_json(checkpoint["swapExpiryReceipt"]), "path": checkpoint["swapExpiryReceipt"]}
            if continuing and continuation["preflight"].get("businessCompleted") is True:
                # A local summary repair must never enter the submission code.
                path = checkpoint["retentionExpiryReceipt"]
                return {**read_json(path), "path": path}
            receipt_path = self.receipts / f"opensms_task_d_{phase}_{self.run_id}_receipt.json"
            progress_path = self.receipts / f"opensms_task_d_{phase}_{self.run_id}_progress.json"
            if receipt_path.is_file():
                receipt = read_json(receipt_path)
                if receipt.get("status") in {"passed", "no_candidate", "recovered_transactional_receipt"}:
                    return receipt
            if progress_path.is_file():
                progress = read_json(progress_path)
                resume_pre_submit = bool(getattr(self.args, "resume_state", None)) and (
                    self.args.authorization_source == "current-user"
                    and progress.get("status") == "failed"
                    and progress.get("task") == "D" and progress.get("phase") == phase
                    and progress.get("batches") == [] and progress.get("pendingBatch") is None
                    and progress.get("validation") is None
                    and progress.get("uncertainSubmission") is not True
                    and not progress.get("uncertainBatchCount")
                )
                if not resume_pre_submit:
                    raise PatrolError(
                    f"任务D {phase}存在未确认断点，禁止自动重复提交；请先核对: {progress_path}；状态={progress.get('status')}"
                    )
            continuation_args = []
            if continuing_submissions and phase == "retention_verified":
                continuation_args = ["--continue-progress", continuation["preflight"]["progress"],
                    "--continue-progress-sha256", continuation["preflight"]["progressSha256"],
                    "--authorization-source", "current-user"]
            result = self.command(
                f"task_d_expiry_{phase}",
                [
                    NODE,
                    SCRIPTS / "opensms_execute_task_d_expiry.mjs",
                    notification_path,
                    self.run_id,
                    self.receipts,
                ] + (["--resume-pre-submit"] if resume_pre_submit else []) + continuation_args,
                env={**env, **({"OPENSMS_TASK_D_CONTINUE_AUTHORIZED": "1"} if continuing else {})},
            ).payload
            if result.get("status") not in {"passed", "no_candidate"}:
                raise PatrolError(f"任务D {phase}到期标记未确认完成")
            if result.get("businessReceiptPassed") is not True:
                raise PatrolError(f"任务D {phase}业务回执未通过")
            verification_passed = (
                result.get("independentReadbackPassed") is True
                or (
                    result.get("transactionalVerificationPassed") is True
                    and result.get("verificationMode") == "synchronous_transactional_business_receipt"
                    and result.get("independentReadbackAvailable") is False
                )
            )
            if not verification_passed:
                raise PatrolError(f"任务D {phase}到期标记事务校验未通过")
            return result

        # 只保留两个通知分支；用户已删除转入B14保留区工作。
        swap_expiry = execute_expiry("swap_zero", manifest["swapNotificationPath"])
        checkpoint["swapExpiryReceipt"] = swap_expiry.get("path") or str(
            self.receipts / f"opensms_task_d_swap_zero_{self.run_id}_receipt.json"
        )
        self._save_state()

        carrier_results = []
        checkpoint["carrierRoutes"] = []
        checkpoint["retentionCarrierTransferEnabled"] = False
        self._save_state()

        retention_expiry = execute_expiry("retention_verified", manifest["retentionNotificationPath"])
        checkpoint["retentionExpiryReceipt"] = retention_expiry.get("path") or str(
            self.receipts / f"opensms_task_d_retention_verified_{self.run_id}_receipt.json"
        )
        if not (getattr(self.args, "continue_task_d", False)
                and (self.state.get("taskDContinuationAttempts") or [{}])[-1].get("preflight", {}).get("businessCompleted") is True):
            checkpoint["businessCompletedAt"] = now_hkt().isoformat()
        checkpoint["businessCompleted"] = True
        self._save_state()
        # Only reconcile the two confirmed notification receipts. Deleted B14
        # carriers must never be looked up, nor gate notification completion.
        checkpoint.update(removed_population_fields())
        try:
            notification_summary = self.collect_task_d_notification_summary(manifest_path, checkpoint)
        except Exception as exc:
            checkpoint["notificationSummaryStatus"] = "failed"
            checkpoint["notificationSummaryError"] = str(exc)
            self._save_state()
            raise
        checkpoint["notificationSummaryStatus"] = "passed"
        self._save_state()
        data = {
            "executed": True,
            "status": "passed",
            "inputWorkbook": document["cardResult"],
            "inputWorkbookSha256": sha256(Path(document["cardResult"])),
            "manifest": str(manifest_path),
            "retentionReceipt": manifest["retentionReceiptPath"],
            "swapExpiryReceipt": checkpoint["swapExpiryReceipt"],
            "retentionExpiryReceipt": checkpoint["retentionExpiryReceipt"],
            "swapMetricCandidateCount": int(manifest.get("swapMetricCandidateCount") or 0),
            "swapMetricUnconfiguredCarrierSkippedCount": int(manifest.get("swapMetricUnconfiguredCarrierSkippedCount") or 0),
            "swapNotificationCount": int(manifest.get("swapNotificationCount") or 0),
            "retentionUniqueCardCount": int(manifest.get("retentionUniqueCardCount") or 0),
            **checkpoint["retentionPartition"],
            "retentionCarrierTransferEnabled": False,
            "retentionCarrierTransferStatus": "removed_by_user",
            "carrierCandidateCount": 0,
            "carrierActualWrite": 0,
            "swapExpireDate": manifest["swapExpireDate"],
            "retentionExpireDate": manifest["retentionExpireDate"],
            "readPreparationConcurrent": manifest.get("readOnlyPreparationConcurrent") is True,
            "writeExecutionSerial": True,
            "notificationSummary": notification_summary,
            **removed_population_fields(),
        }
        return self.complete_stage("taskD", data, started)

    def collect_task_d_notification_summary(self, manifest_path: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
        if not self.authorization.get("taskD") or checkpoint.get("businessCompleted") is not True:
            raise PatrolError("任务D未授权或业务未完成，不得汇总为通知成功")
        from opensms_task_d_notification_summary import build_from_files

        result = build_from_files(self.run_id, manifest_path, checkpoint["swapExpiryReceipt"],
                                  checkpoint["retentionExpiryReceipt"])
        receipt = self.receipts / f"opensms_task_d_notification_summary_{self.run_id}.json"
        atomic_json(receipt, result)
        result["receipt"] = str(receipt)
        result["receiptSha256"] = sha256(receipt)
        return result

    def collect_task_d_retention_summary(self, carrier_results: list[dict[str, Any]],
                                         env: dict[str, str]) -> dict[str, Any]:
        # Compatibility trap for stale callers; no browser or file side effects.
        raise PatrolError("B14运营商已删除，保留区存量查询及统计入口已停用")

    def skip_task_d(self) -> dict[str, Any]:
        reused = self.reusable_stage("taskD")
        if reused:
            return reused
        started = time.monotonic()
        return self.complete_stage(
            "taskD",
            {
                "executed": False,
                "status": "skipped_no_current_user_authorization",
                "authorizationRequired": True,
                "authorizationPhrase": "强制执行任务D",
                "candidateGenerationExecuted": False,
                "retentionReadExecuted": False,
                "carrierWriteExecuted": False,
                "expiryMarkerWriteExecuted": False,
                "swapMetricCandidateCount": 0,
                "swapNotificationCount": 0,
                "retentionUniqueCardCount": 0,
                "carrierCandidateCount": 0,
                "carrierActualWrite": 0,
                "retentionCarrierTransferEnabled": False,
                "retentionCarrierTransferStatus": "removed_by_user",
                **removed_population_fields(),
                "writeExecutionSerial": True,
            },
            started,
        )

    def run_task_e(self, document: dict[str, Any], acquire: dict[str, Any]) -> dict[str, Any]:
        if not self.authorization["taskE"]:
            raise PatrolError("任务E缺少当前真实用户双重授权，拒绝执行")
        reused = self.reusable_stage("taskE", ("manifest", "receipt"))
        if reused:
            return reused
        started = time.monotonic()
        env = {
            "BITBROWSER_CDP": self.args.cdp,
            "OPENSMS_FORCE_RAW_CDP": "1",
            "OPENSMS_FORCE_TASK_E": "1",
            "OPENSMS_ENABLE_TASK_E": "1",
            "OPENSMS_EXECUTE_TASK_E": "1",
        }
        manifest_path = self.receipts / f"opensms_task_e_{self.run_id}_manifest.json"
        receipt_path = self.receipts / f"opensms_task_e_{self.run_id}_receipt.json"
        progress_path = self.receipts / f"opensms_task_e_{self.run_id}_progress.json"
        recovery_proof = None
        if self.args.task_e_recovery_progress:
            recovery_proof = self.copy_artifact(
                self.args.task_e_recovery_progress,
                f"opensms_task_e_recovery_proof_{self.run_id}.json",
            )
        task_d = self.state.get("stages", {}).get("taskD") or {}
        reused_task_d_retention = task_d.get("executed") is True and task_d.get("retentionReceipt")
        retention_receipt_path = Path(task_d["retentionReceipt"]) if reused_task_d_retention else (
            self.receipts / f"opensms_task_e_retention_{self.run_id}_receipt.json"
        )
        if not retention_receipt_path.is_file():
            self.command(
                "task_e_retention_readonly",
                [
                    NODE,
                    SCRIPTS / "opensms_collect_task_e_card_retention.mjs",
                    self.run_id,
                    retention_receipt_path,
                ],
                env=env,
            )
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
        else:
            manifest = self.command(
                "task_e_candidates",
                [
                    PYTHON,
                    SCRIPTS / "opensms_generate_task_e_candidates.py",
                    document["cardResult"],
                    "--server-date",
                    acquire["serverDate"],
                    "--tag",
                    self.run_id,
                    "--output",
                    manifest_path,
                    "--ledger",
                    TASK_E_LEDGER,
                    "--retention-receipt",
                    retention_receipt_path,
                ],
                env=env,
            ).payload
        if (
            manifest.get("task") != "E"
            or manifest.get("forceGate") is not True
            or manifest.get("automationGate") is not True
            or manifest.get("ledgerDedupeKey") != "phone"
        ):
            raise PatrolError("任务E候选清单门禁或永久去重键无效")
        if receipt_path.is_file():
            receipt = read_json(receipt_path)
        else:
            if progress_path.is_file():
                progress = read_json(progress_path)
                retryable_recovery_validation_failure = (
                    recovery_proof is not None
                    and progress.get("status") == "failed"
                    and progress.get("automaticRetryBlocked") is False
                    and (
                        (progress.get("pendingBatch") or {}).get("recoveryRetry") is True
                        or (
                            "业务回执不完整" in str(progress.get("error") or "")
                            and ((progress.get("pendingBatch") or {}).get("response") or {}).get("code") == 200
                        )
                    )
                )
                if not retryable_recovery_validation_failure:
                    raise PatrolError(
                        "任务E存在未确认断点，禁止自动重复提交；"
                        f"请先核对: {progress_path}；状态={progress.get('status')}"
                    )
            execute_args = [
                NODE,
                SCRIPTS / "opensms_execute_task_e_expiry.mjs",
                manifest_path,
                self.run_id,
                self.receipts,
                TASK_E_LEDGER,
            ]
            if recovery_proof:
                execute_args.extend(["--recovery-progress", recovery_proof])
            if progress_path.is_file():
                execute_args.extend(["--resume-progress", progress_path])
            receipt = self.command(
                "task_e_expiry",
                execute_args,
                env=env,
            ).payload
        if receipt.get("status") not in {"passed", "passed_with_not_found", "no_candidate"}:
            raise PatrolError("任务E到期标记未确认完成")
        if receipt.get("businessReceiptPassed") is not True:
            raise PatrolError("任务E到期标记业务回执未通过")
        if receipt.get("transactionalVerificationPassed") is not True:
            raise PatrolError("任务E到期标记事务校验未通过")
        if receipt.get("ledgerVerificationPassed") is not True:
            raise PatrolError("任务E永久去重台账校验未通过")
        processed = int(receipt.get("inputCount") or 0)
        submitted = int(receipt.get("submittedCount") if receipt.get("submittedCount") is not None else receipt.get("matchedCardCount") or 0)
        not_written = int(receipt.get("notWrittenCount") or 0)
        if processed != int(manifest.get("candidateCount") or 0):
            raise PatrolError("任务E处理数与去重后候选数不一致")
        if submitted + not_written != processed:
            raise PatrolError("任务E成功写入数、未写入数与处理数无法核对")
        ledger_snapshot = None
        if TASK_E_LEDGER.is_file():
            ledger_snapshot = self.copy_artifact(
                TASK_E_LEDGER,
                f"opensms_task_e_ledger_after_{self.run_id}.json",
            )
        if submitted and ledger_snapshot is None:
            raise PatrolError("任务E有成功提交但永久去重台账缺失")
        data = {
            "executed": True,
            "status": receipt.get("status"),
            "inputWorkbook": document["cardResult"],
            "inputWorkbookSha256": sha256(Path(document["cardResult"])),
            "manifest": str(manifest_path),
            "receipt": str(receipt_path),
            "ledger": str(TASK_E_LEDGER),
            "ledgerSnapshot": str(ledger_snapshot) if ledger_snapshot else None,
            "ledgerDedupeKey": "phone",
            "retentionReceipt": str(retention_receipt_path),
            "retentionReceiptReusedFromTaskD": bool(reused_task_d_retention),
            "retentionReadExecuted": True,
            "retentionPageCount": int(manifest.get("retentionPageCount") or 0),
            "retentionTotalRecordCount": int(manifest.get("retentionTotalRecordCount") or 0),
            "retentionFetchedRecordCount": int(manifest.get("retentionFetchedRecordCount") or 0),
            "retentionUniqueCardCount": int(manifest.get("retentionUniqueCardCount") or 0),
            "retentionWorkbookPhoneFoundCount": int(manifest.get("retentionWorkbookPhoneFoundCount") or 0),
            "retentionWorkbookDeadlineKnownCount": int(manifest.get("retentionWorkbookDeadlineKnownCount") or 0),
            "retentionWorkbookMissingPhoneCount": int(manifest.get("retentionWorkbookMissingPhoneCount") or 0),
            "retentionWorkbookMissingDeadlineCount": int(manifest.get("retentionWorkbookMissingDeadlineCount") or 0),
            "retentionInvalidNotificationDateRecordCount": int(manifest.get("retentionInvalidNotificationDateRecordCount") or 0),
            "retentionDeadlineMatchPhoneCount": int(manifest.get("retentionDeadlineMatchPhoneCount") or 0),
            "retentionDeadlineMatchRecordCount": int(manifest.get("retentionDeadlineMatchRecordCount") or 0),
            "retentionDeadlineMatches": manifest.get("retentionDeadlineMatches") or [],
            "eligibleWorkbookRowCount": int(manifest.get("eligibleWorkbookRowCount") or 0),
            "candidateCount": int(manifest.get("candidateCount") or 0),
            "alreadySubmittedExcludedCount": int(manifest.get("alreadySubmittedExcludedCount") or 0),
            "neverProcessSourceExcludedCount": int(manifest.get("neverProcessSourceExcludedCount") or 0),
            "unconfiguredCarrierSkippedCount": int(manifest.get("unconfiguredCarrierSkippedCount") or 0),
            **anomaly_summary(manifest),
            "duplicateSameDeadlineCollapsedCount": int(manifest.get("duplicateSameDeadlineCollapsedCount") or 0),
            "dateGroupCount": int(manifest.get("dateGroupCount") or 0),
            "expireDates": [group.get("expireDate") for group in manifest.get("groups") or []],
            "processedCount": processed,
            "submittedCount": submitted,
            "notWrittenCount": not_written,
            "notWrittenCards": receipt.get("notWrittenCards") or [],
            "matchedNoChangeCount": int(receipt.get("matchedNoChangeCount") or 0),
            "matchedNoChangeCards": receipt.get("matchedNoChangeCards") or [],
            "recoveryProof": str(recovery_proof) if recovery_proof else None,
            "recoveryCandidateCount": int(receipt.get("recoveryCandidateCount") or 0),
            "recoveryRetriedCount": int(receipt.get("recoveryRetriedCount") or 0),
            "idempotentRecoveryNoChangeCount": int(receipt.get("idempotentRecoveryNoChangeCount") or 0),
            "writeExecutionSerial": True,
            "independentReadbackAvailable": receipt.get("independentReadbackAvailable") is True,
            "transactionalVerificationPassed": receipt.get("transactionalVerificationPassed") is True,
            "ledgerVerificationPassed": receipt.get("ledgerVerificationPassed") is True,
        }
        return self.complete_stage("taskE", data, started)

    def add_task_e_unwritten_sheet(self, document: dict[str, Any]) -> dict[str, Any]:
        task_e = self.state.get("stages", {}).get("taskE") or {}
        if not self.authorization["taskE"] or task_e.get("executed") is not True:
            return document
        # No old E receipts and no synthetic zero/missing-outcome rows in A-only runs.
        if not task_e.get("notWrittenCount"):
            return document
        output = self.delivery / f"OpenSMS近10日报表巡检_任务E后状态_{self.run_id}.xlsx"
        verification = self.receipts / f"opensms_task_e_unwritten_sheet_{self.run_id}.json"
        result = self.command("task_e_unwritten_sheet", [
            PYTHON, SCRIPTS / "opensms_task_e_unwritten_sheet.py",
            "--input", document["cardResult"], "--manifest", task_e["manifest"],
            "--receipt", task_e["receipt"], "--run-id", self.run_id,
            "--output", output, "--verify-receipt", verification,
            "--runtime", self.run_dir / "task_e_sheet_runtime",
        ]).payload
        if not result.get("passed") or result.get("rowCount") != task_e["notWrittenCount"]:
            raise PatrolError("任务E未写入sheet与本轮回执无法核对")
        task_e["notWrittenSheetReceipt"] = str(verification)
        task_e["notWrittenSheet"] = result["sheet"]
        self._save_state()
        # Adding a sheet invalidates the previous workbook-byte scan cache.
        return {**document, "cardResult": str(output), "verifiedCachePath": None, "verifiedCacheSha256": None}

    def skip_task_e(self) -> dict[str, Any]:
        reused = self.reusable_stage("taskE")
        if reused:
            return reused
        started = time.monotonic()
        return self.complete_stage(
            "taskE",
            {
                "executed": False,
                "status": "skipped_no_current_user_authorization",
                "authorizationRequired": True,
                "authorizationPhrase": "强制执行任务E",
                "candidateGenerationExecuted": False,
                "ledgerReadExecuted": False,
                "backendPhoneQueryExecuted": False,
                "retentionReadExecuted": False,
                "expiryMarkerWriteExecuted": False,
                "eligibleWorkbookRowCount": 0,
                "candidateCount": 0,
                "alreadySubmittedExcludedCount": 0,
                "neverProcessSourceExcludedCount": 0,
                "dateGroupCount": 0,
                "submittedCount": 0,
                "retentionPageCount": 0,
                "retentionTotalRecordCount": 0,
                "retentionFetchedRecordCount": 0,
                "retentionUniqueCardCount": 0,
                "retentionDeadlineMatchPhoneCount": 0,
                "retentionDeadlineMatchRecordCount": 0,
                "retentionDeadlineMatches": [],
                "writeExecutionSerial": True,
            },
            started,
        )

    def run_task_g(self, acquire: dict[str, Any], before_g: dict[str, Any] | None) -> dict[str, Any]:
        """Last synchronous business stage; independent G ledger never weakens V guards."""
        started = time.monotonic()
        if self.task_a_only or getattr(self.args, "restore_unexpired_to_a11", False):
            return self.complete_stage("taskG", {"status": "skipped", "executed": False,
                "reason": "explicit_scope"}, started)
        from opensms_task_g import SETTINGS, audit
        settings = read_json(SETTINGS)
        if settings.get("enabled") is False:
            return self.complete_stage("taskG", {"status": "skipped", "executed": False,
                "reason": "explicit_scope_or_disabled"}, started)
        previous = self.state.get("stages", {}).get("taskG")
        if previous:
            if previous.get("status") != "passed":
                raise PatrolError("任务G已有未解决意图或失败回执，禁止自动重试")
            audit(previous["input"], previous["receipt"])
            return previous
        if not acquire.get("serverDateVerified") or not before_g:
            raise PatrolError("任务G平台日期或其它任务后的独立状态未确认")
        config_path = self.receipts / f"opensms_task_g_prepare_{self.run_id}.json"
        input_path = self.receipts / f"opensms_task_g_input_{self.run_id}.json"
        receipt_path = self.receipts / f"opensms_task_g_receipt_{self.run_id}.json"
        proof_path = self.receipts / f"opensms_task_g_independent_audit_{self.run_id}.json"
        atomic_json(config_path, {"runId": self.run_id, "serverDate": acquire["serverDate"],
            "source": acquire["processingSource"], "scanCache": acquire["scanCache"],
            "postBusinessWorkbook": before_g["finalWorkbook"],
            "postBusinessReadback": before_g["finalStateReceipt"]})
        self.command("task_g_prepare", [PYTHON, SCRIPTS / "opensms_task_g.py", "prepare", config_path, input_path])
        operation = self.command("task_g_execute", [NODE, SCRIPTS / "opensms_execute_task_g.mjs", input_path, receipt_path],
            env={"OPENSMS_TASK_G_RUN_ID": self.run_id, "OPENSMS_TASK_G_INPUT_SHA256": sha256(input_path),
                 "BITBROWSER_CDP": self.args.cdp, "OPENSMS_FORCE_RAW_CDP": "1"}, allow_codes=(0, 1))
        data = {**operation.payload, "executed": True, "input": str(input_path), "receipt": str(receipt_path),
                "independentAudit": str(proof_path)}
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            data.update({key: receipt.get(key) for key in ("status", "error", "enteredCount", "restoredCount",
                "restingCount", "alreadyHandledCount", "sourceChangedCount", "detachedCount", "threshold",
                "unknownMetricRowCount", "sourceKiniExcludedCount", "sourceUnknownOprCount",
                "liveKiniExcludedCount", "liveUnknownOprCount", "byOriginalCarrier", "refreshGroups", "ledgerPath",
                "candidateCount", "verifiedBatchCount")})
        execution_passed = data.get("status") == "passed"
        if execution_passed:
            data["status"] = "pending_independent_audit"
        self.complete_stage("taskG", data, started)
        if not execution_passed:
            raise PatrolError(f"任务G未完整确认，禁止重试：{data.get('error') or '查看本轮回执'}")
        proof = audit(input_path, receipt_path)
        atomic_json(proof_path, proof)
        data.update({"status": "passed", "independentReadbackPassed": True, "independentAuditSha256": sha256(proof_path)})
        self.state["stages"]["taskG"].update(data)
        self._save_state()
        return data

    def refresh_workbook(
        self,
        document: dict[str, Any],
        *,
        stage_name: str = "refresh",
        output_label: str = "最终状态",
    ) -> dict[str, Any]:
        refresh_logic_version = "normalized-phone-plus-supplement-v4-verified-zero-delta"
        reused = self.reusable_stage(stage_name, ("finalWorkbook", "finalStateReceipt", "refreshReceipt", "verifyReceipt"))
        if reused and reused.get("refreshLogicVersion") == refresh_logic_version:
            return reused
        started = time.monotonic()
        groups = []
        for result in self.state["writeGroups"].values():
            if result.get("result") != "passed":
                continue
            if count_candidate(result["validated"]):
                groups.append({
                    "mode": "expected", "candidate": result["validated"],
                    "target": result["target"], "route": result["route"],
                })
            if result.get("alreadyTargetPhones"):
                groups.append({
                    "mode": "expected", "phones": result["alreadyTargetPhones"],
                    "target": result["target"], "route": result["route"],
                })
            if result.get("nonSourcePhones"):
                groups.append({
                    "mode": "live", "phones": result["nonSourcePhones"],
                    "route": result["route"], "sourceReceipt": result["write"],
                })
        # G is the final explicit exception; it has its own proof/ledger rather
        # than being disguised as an ordinary V transfer in writeGroups.
        task_g = self.state.get("stages", {}).get("taskG") or {}
        if task_g.get("status") == "passed" and task_g.get("independentReadbackPassed") is True:
            groups.extend(task_g.get("refreshGroups") or [])
        spec_path = self.receipts / f"opensms_{stage_name}_carrier_state_spec_{self.run_id}.json"
        atomic_json(
            spec_path,
            {
                "createdAt": now_hkt().isoformat(),
                "orderedStageSupersession": True,
                "stageOrder": list(STAGE_ORDER),
                "groups": groups,
            },
        )
        final_state = self.receipts / f"opensms_{stage_name}_carrier_state_{self.run_id}.json"
        common_env = {"BITBROWSER_CDP": self.args.cdp, "OPENSMS_FORCE_RAW_CDP": "1"}
        final = self.command(
            f"{stage_name}_carrier_state",
            [NODE, SCRIPTS / "opensms_collect_final_carrier_state.mjs", spec_path, final_state],
            env=common_env,
        )
        if not final.payload.get("passed"):
            raise PatrolError("运营商最终批量回读未通过")

        final_workbook = self.delivery / f"OpenSMS近10日报表巡检_{output_label}_{self.run_id}.xlsx"
        artifact_receipt = self.receipts / f"opensms_{stage_name}_workbook_artifact_refresh_{self.run_id}.json"
        verify_receipt = self.receipts / f"opensms_{stage_name}_workbook_independent_verify_{self.run_id}.json"
        refresh_receipt = self.receipts / f"opensms_{stage_name}_workbook_carrier_refresh_{self.run_id}.json"
        readback_receipt = self.receipts / f"opensms_{stage_name}_workbook_backend_readback_{self.run_id}.json"
        if self.args.refresh_engine == "targeted-ooxml":
            cache_args = [
                "--run-id", self.run_id,
                "--verified-cache-output", self.receipts / f"opensms_{stage_name}_verified_workbook_cache_{self.run_id}.json",
            ]
            if document.get("verifiedCachePath") and document.get("verifiedCacheSha256"):
                cache_args.extend([
                    "--verified-cache-input", document["verifiedCachePath"],
                    "--verified-cache-sha256", document["verifiedCacheSha256"],
                ])
            refreshed = self.command(
                f"{stage_name}_refresh_workbook_fast",
                [
                    PYTHON, SCRIPTS / "opensms_refresh_workbook_carriers_fast.py",
                    document["cardResult"], final_state, final_workbook,
                    artifact_receipt, verify_receipt, refresh_receipt, readback_receipt,
                    *cache_args,
                ],
            )
        else:
            runtime_dir = self.run_dir / "artifact_refresh_runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            runtime_script = runtime_dir / "opensms_refresh_workbook_carriers.mjs"
            shutil.copy2(SCRIPTS / "opensms_refresh_workbook_carriers.mjs", runtime_script)
            link = runtime_dir / "node_modules"
            if not link.exists():
                link.symlink_to(NODE_MODULES, target_is_directory=True)
            refreshed = self.command(
                f"{stage_name}_refresh_workbook_artifact_tool",
                [
                    NODE, runtime_script, document["cardResult"], final_state, final_workbook,
                    artifact_receipt, verify_receipt, refresh_receipt, readback_receipt,
                ],
                cwd=runtime_dir,
            )
        if not refreshed.payload.get("passed"):
            raise PatrolError("最终工作簿运营商刷新复核未通过")
        ensure_xlsx(final_workbook, "最终状态工作簿")
        data = {
            "finalWorkbook": str(final_workbook),
            "finalWorkbookSha256": sha256(final_workbook),
            "finalStateReceipt": str(final_state),
            "artifactReceipt": str(artifact_receipt),
            "verifyReceipt": str(verify_receipt),
            "refreshReceipt": str(refresh_receipt),
            "readbackReceipt": str(readback_receipt),
            "liveRefreshCount": int(final.payload.get("candidateCount") or 0),
            "changedCellCount": int(refreshed.payload.get("changed") or 0),
            "refreshEngine": self.args.refresh_engine,
            "refreshLogicVersion": refresh_logic_version,
            "workbookRefreshDurationSeconds": refreshed.duration_seconds,
            "workbookRefreshMode": refreshed.payload.get("workbookRefreshMode"),
            "zeroDeltaConfirmed": refreshed.payload.get("zeroDeltaConfirmed") is True,
            "verifiedCacheReused": refreshed.payload.get("verifiedCacheReused") is True,
            "verifiedCachePath": refreshed.payload.get("verifiedCachePath"),
            "verifiedCacheSha256": refreshed.payload.get("verifiedCacheSha256"),
        }
        # No-write groups omit an empty per-group session, never the fresh
        # stage read. Retain the first subsequent stage proof: later tasks may
        # legitimately supersede the group's target.
        for result in self.state["writeGroups"].values():
            if result.get("stageReadbackRequired") and not result.get("stageReadbackVerified"):
                result.update({
                    "stageReadbackVerified": True,
                    "stageReadbackReceipt": str(final_state),
                    "stageReadbackSha256": sha256(final_state),
                })
        return self.complete_stage(stage_name, data, started)

    def refreshed_document(self, document: dict[str, Any], refresh: dict[str, Any]) -> dict[str, Any]:
        return {
            **document, "cardResult": refresh["finalWorkbook"],
            "verifiedCachePath": refresh.get("verifiedCachePath"),
            "verifiedCacheSha256": refresh.get("verifiedCacheSha256"),
        }

    def build_finalize_config(
        self,
        acquire: dict[str, Any],
        candidates: dict[str, Any],
        document: dict[str, Any],
        refresh: dict[str, Any],
        audit_receipt: Path,
        run_status: str,
    ) -> Path:
        routing = read_json(candidates["routingManifest"])
        groups = []
        for result in self.state["writeGroups"].values():
            group = {
                "title": result["title"], "candidate": result["candidate"],
                "backendValid": result.get("backendValid", 0),
                "result": "无候选" if result.get("result") == "no_candidate" else "通过",
            }
            if result.get("result") == "passed":
                group.update({"validated": result["validated"], "write": result["write"], "verify": result["verify"]})
                if result.get("noWrite"):
                    group.update({
                        "noWrite": True,
                        "stageReadbackReceipt": result.get("stageReadbackReceipt"),
                        "stageReadbackSha256": result.get("stageReadbackSha256"),
                    })
                if result.get("nonSourcePhones"):
                    group["notes"] = [f"{len(result['nonSourcePhones'])}个号码在提交前已离开允许来源，按最终后台实时状态刷新文档"]
            groups.append(group)

        by_route = self.state["writeGroups"]
        value = lambda route, field="candidateCount": int((by_route.get(route) or {}).get(field) or 0)
        safety_path = self.receipts / f"opensms_safety_guards_{self.run_id}.json"
        task_b_status = "本轮已获当前真实用户授权并执行" if self.authorization["taskB"] else "无当前真实用户授权，本轮关闭"
        task_c_status = "本轮已获当前真实用户授权并执行" if self.authorization["taskC"] else "无当前真实用户授权，本轮关闭"
        task_d = self.state.get("stages", {}).get("taskD") or {}
        task_e = self.state.get("stages", {}).get("taskE") or {}
        task_f = self.state.get("stages", {}).get("taskF") or {}
        card_receipt_payload = read_json(document["cardReceipt"])
        card_receipt_payload = card_receipt_payload.get("cardSlotReport") or card_receipt_payload
        task_f_red_review = (
            (card_receipt_payload.get("verification") or {}).get("task_f_missing_activation_red_review")
            or {}
        )
        task_b = self.state.get("stages", {}).get("taskB") or {}
        task_c = self.state.get("stages", {}).get("taskC") or {}
        if not self.authorization["taskD"]:
            task_d_status = "无当前真实用户授权，本轮关闭且未触碰任务D"
        elif task_d.get("status") == "passed":
            task_d_status = "本轮已获当前真实用户授权；到期标记事务回执通过，卡保留运营商写入独立回读通过"
        else:
            task_d_status = "本轮已获授权但未执行完成或未确认"
        if not self.authorization["taskE"]:
            task_e_status = "无当前真实用户授权，本轮关闭且未触碰任务E"
        elif task_e.get("status") in {"passed", "passed_with_not_found", "no_candidate"}:
            task_e_status = (
                "本轮已获当前真实用户授权；按截止日分组提交并写入永久去重台账；"
                f"未写入{int(task_e.get('notWrittenCount') or 0)}个；"
                f"卡保留新到期日与文档截止日一致提醒{int(task_e.get('retentionDeadlineMatchPhoneCount') or 0)}个"
            )
        else:
            task_e_status = "本轮已获授权但未执行完成或未确认"
        if self.task_a_only:
            task_b_status = task_c_status = "仅任务A模式，完整跳过"
            task_d_status = task_e_status = "仅任务A模式，完整跳过"
            task_f_status = "仅任务A模式，任务F完整跳过"
        elif task_f.get("background") is True:
            task_f_status = "任务F已独立后台派发；主报表无需等待补查，新增日期下轮生效" if task_f.get("status") == "running" else "任务F后台启动失败；已核验主报表不受影响"
        elif task_f.get("reason") in {"no_missing_activation_after_fallback", "no_missing_authoritative_dates"}:
            task_f_status = ("本轮10日表与F库均缺激活日期的有效ICCID为0，未启动F查询、未生成F新增文件"
                if task_f.get('dateEligibilityMode') == 'raw_10day_activation_missing_and_local_activation_missing'
                else "主表日期无待补缺口，未启动F查询、未生成F新增文件")
        elif task_f.get("status") == "passed":
            task_f_status = (
                f"文档生成后后台静默目录同步完成；Kooda ICCID {int(task_f.get('inventoryCount') or 0)}个，"
                f"库内已有{int(task_f.get('alreadyQueriedCount') or 0)}个，联网增量{int(task_f.get('pendingCount') or 0)}个；"
                "补查只更新独立ICCID库；主报表始终使用本轮系统日期"
            )
        elif task_f.get("status") == "login_required":
            task_f_status = "默认自动目录同步发现缺失ICCID，但Grab&Go独立登录失败，已在提交ICCID前停止"
        else:
            task_f_status = "默认自动目录同步未完整完成"
        safety = {
            "createdAt": now_hkt().isoformat(), "runId": self.run_id,
            "taskSelection": "A" if self.task_a_only else "default", "taskAOnly": self.task_a_only,
            "silentBrowser": {"required": True, "silentBrowserMode": True, "browserForegroundActivationAttempted": False, "passed": True},
            "taskAuthorization": {"taskBExecuted": self.authorization["taskB"], "taskBStatus": task_b_status, "taskCExecuted": self.authorization["taskC"], "taskCStatus": task_c_status, "taskDExecuted": task_d.get("executed") is True, "taskDStatus": task_d_status, "taskDCurrentUserAuthorized": self.authorization["taskD"], "taskEExecuted": task_e.get("executed") is True, "taskEStatus": task_e_status, "taskECurrentUserAuthorized": self.authorization["taskE"], "taskFExecuted": task_f.get("executed") is True, "taskFStatus": task_f_status, "taskFCurrentUserAuthorized": self.authorization["taskF"], "taskFAutomaticCatalogSync": task_f.get("automaticCatalogSync") is True, "passed": True},
            "protectedSources": {"groups": ["A21", "A2"], "excludedCount": int(routing.get("protectedSourceExcludedCount") or 0), "writeCount": 0, "passed": True},
            "forbiddenTargets": {"groups": ["B13"], "candidateCount": 0, "writeCount": 0, "passed": True},
            "offlineRecovery": {"offlineRecoveryEnabled": False, "candidateCount": 0, "backendValidCount": 0, "writeCount": 0, "successCount": 0, "failureCount": 0, "retryCount": 0, "independentVerificationExecuted": False, "passed": True},
            "preview": {"generated": False, "opened": False, "passed": True},
        }
        safety["taskAuthorization"].update({
            "taskSelection": "A" if self.task_a_only else "default", "taskAOnly": self.task_a_only,
            "taskFStageStatus": task_f.get("status"), "taskFSkippedReason": task_f.get("reason"),
        })
        safety['expiredCardProbe'] = self.state.get('stages', {}).get('expiredCardProbe')
        atomic_json(safety_path, safety)
        report = self.delivery / f"OpenSMS_10日报表巡检_{self.run_id}.md"
        extra_receipts = [
            {"label": "任务A卡保留全部分页及独立回读", "path": acquire["cardRetentionReceipt"]},
            {"label": "任务A卡保留去重号码TXT", "path": acquire["cardRetentionPhoneList"]},
            {"label": "后台导出回执", "path": acquire["exportReceipt"]},
            {"label": "新鲜源表单次扫描缓存回执", "path": acquire["scanCacheReceipt"]},
            {"label": "本轮10日表Kooda ICCID与日期来源（非全平台库存）", "path": acquire["systemKoodaInventory"]},
            {"label": "后台登录预检与恢复回执", "path": acquire["loginReceipt"]},
            {"label": "文档调度回执", "path": document["dispatchReceipt"]},
            {"label": "phoneConfig服务器日期回执", "path": acquire["phoneDateReceipt"]},
            {"label": "day服务器日期回执", "path": acquire["dayDateReceipt"]},
            {"label": "统一运营商候选清单（含A区4%分流）", "path": candidates["routingManifest"]},
            {"label": "休息区回复恢复汇总", "path": candidates["eRecoverySummary"]},
            {"label": "运营商最终批量回读回执", "path": refresh["finalStateReceipt"]},
            {"label": "运营商文档刷新回执", "path": refresh["refreshReceipt"]},
            {"label": "运营商文档独立复核回执", "path": refresh["verifyReceipt"]},
            {"label": "本轮安全门禁回执", "path": str(safety_path)},
        ]
        task_g = self.state.get("stages", {}).get("taskG") or {}
        for key, label in (("input", "任务G昨日发送量与处理后来源"), ("receipt", "任务G转入恢复台账与批量回读"),
                           ("independentAudit", "任务G独立复核")):
            if task_g.get(key):
                extra_receipts.append({"label": label, "path": task_g[key]})
        if self.chatr_google_date_fallback:
            extra_receipts.extend([
                {"label": "本轮Chatr日期补全Google源", "path": acquire["chatrGoogleSource"]},
                {"label": "Chatr日期Google新鲜下载回执", "path": acquire["chatrGoogleDownloadReceipt"]},
                {"label": "Chatr专用冻结日期快照", "path": acquire["chatrGoogleDates"]},
            ])
        date_cache = self.state.get("reportDateCache") or {}
        if acquire.get("systemKoodaPaginationEvidence"):
            extra_receipts.append({
                "label": "系统Kooda分页核验及有限重读证据"
                         f"（采集{acquire.get('systemKoodaPaginationAttemptCount', '未知')}次，"
                         f"异常恢复{'是' if acquire.get('systemKoodaPaginationRecovered') else '否'}）",
                "path": acquire["systemKoodaPaginationEvidence"],
            })
        for index, coverage_path in enumerate(acquire.get("systemKoodaCoverageReceipts") or [], 1):
            extra_receipts.append({"label": f"系统库存与新鲜导出身份覆盖核验第{index}次", "path": coverage_path})
        if date_cache.get("path"):
            extra_receipts.append({"label": "主报表冻结日期快照", "path": date_cache["path"]})
        if task_f.get("backgroundJob"):
            extra_receipts.append({"label": "任务F独立后台任务绑定清单", "path": task_f["backgroundJob"]})
        if task_d.get("executed") is True:
            extra_receipts.extend([
                {"label": "任务D完整准备清单", "path": task_d["manifest"]},
                {"label": "任务D卡保留全部分页回执", "path": task_d["retentionReceipt"]},
                {"label": "任务D换号区到期标记回执", "path": task_d["swapExpiryReceipt"]},
                {"label": "任务D卡保留到期标记回执", "path": task_d["retentionExpiryReceipt"]},
            ])
            for label, key in (("任务D执行后两保留区全量回读", "retentionPopulationReceipt"),
                               ("任务D新转入与保留区存量独立统计", "retentionSummaryReceipt"),
                               ("任务D本轮运营商证据分组", "retentionGroupsReceipt")):
                if task_d.get(key):
                    extra_receipts.append({"label": label, "path": task_d[key]})
        if task_e.get("executed") is True:
            extra_receipts.extend([
                {"label": "任务E卡保留全部分页只读回执", "path": task_e["retentionReceipt"]},
                {"label": "任务E按截止日分组候选", "path": task_e["manifest"]},
                {"label": "任务E到期标记事务与去重台账回执", "path": task_e["receipt"]},
            ])
            if task_e.get("ledgerSnapshot"):
                extra_receipts.append({"label": "任务E永久去重台账本轮快照", "path": task_e["ledgerSnapshot"]})
        if task_f.get("executed") is True:
            for label, key in (
                ("任务F权威附件导入回执", "catalogReceipt"),
                ("任务F Grab&Go登录检查", "loginReceipt"),
                ("任务F Kooda全量卡存储清单", "inventory"),
                ("任务F数据库去重后待查清单", "pending"),
                ("任务F五分钟进度", "progress"),
                ("任务F Grab&Go查询回执", "lookupReceipt"),
                ("任务F数据库导入回执", "importReceipt"),
                ("任务F本轮最新ICCID库", "iccidLibrary"),
                ("任务F固定最新ICCID库", "latestIccidLibrary"),
                ("任务F数据库本轮快照", "databaseSnapshot"),
                (f"任务F数据库全量截止日模板（总记录/有日期/空日期：{int(task_f.get('deadlineRecordCount') or 0)}/{int(task_f.get('deadlineActivationCount') or 0)}/{int(task_f.get('deadlineMissingDateCount') or 0)}）", "deadlineWorkbook"),
                ("任务F截止日模板独立核验回执", "deadlineExportReceipt"),
            ):
                if task_f.get(key):
                    extra_receipts.append({"label": label, "path": task_f[key]})
        for stage_name, label in (
            ("refreshAfterStandard", "普通规则后文档刷新回执"),
            ("refreshAfterTaskB", "任务B后文档刷新回执"),
            ("refreshAfterTaskC", "任务C后文档刷新回执"),
            ("refreshAfterTaskD", "任务D后文档刷新回执"),
            ("refreshBeforeTaskG", "任务G前最终同步任务状态回读"),
        ):
            stage = self.state.get("stages", {}).get(stage_name) or {}
            if stage.get("refreshReceipt"):
                extra_receipts.append({"label": label, "path": stage["refreshReceipt"]})
        optimization_notes = SCRIPTS / "OPENSMS_PERFORMANCE_V50.md"
        if optimization_notes.is_file():
            notes_copy = self.copy_artifact(optimization_notes)
            extra_receipts.append({"label": "v50程序并发优化与验证说明", "path": str(notes_copy)})
        extra_receipts.append({"label": "本轮自动恢复记录", "path": str(self.automatic_recovery_report())})
        reliability_notes = SCRIPTS / "OPENSMS_RELIABILITY_V53.md"
        if reliability_notes.is_file():
            notes_copy = self.copy_artifact(reliability_notes)
            extra_receipts.append({"label": "v53后台自动恢复规则", "path": str(notes_copy)})
        config = {
            "runType": "scheduled_patrol", "runId": self.run_id,
            "unexpiredRestore": self.state.get("stages", {}).get("unexpiredRestore"),
            "runStartedAt": self.state.get("createdAt"),
            "taskSelection": "A" if self.task_a_only else "default", "taskAOnly": self.task_a_only,
            "taskFStageStatus": task_f.get("status"), "taskFSkippedReason": task_f.get("reason"),
            "safetyReceipt": str(safety_path),
            "runTime": datetime.strptime(self.run_id[:15], "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M:%S"),
            "runStatus": run_status, "taskId": acquire.get("taskId"),
            "taskDuration": f"{acquire.get('taskDurationSeconds', 0)}秒", "serverDate": acquire["serverDate"],
            "exportWorkflow": "后台静默创建全部号码近10日统计任务，确认成功后仅下载并校验本轮新文件",
            "sourceArchive": acquire["sourceArchive"], "sourceSha256": acquire["sourceSha256"],
            "association": None, "googleSha256": None,
            "googleAssociationEnabled": False, "googleDisabledReason": "user_disabled",
            "googleChatrDateFallbackEnabled": self.chatr_google_date_fallback,
            "googleChatrDateFallbackScope": "chatr_blank_activation_and_cutoff_only",
            "chatrDocumentDateSource": "current_run_system_then_fresh_google" if self.chatr_google_date_fallback else "current_run_system_only",
            "googleChatrDateFallback": document.get("googleChatrDateFallback") or {},
            "googleSystemMissingSheetEnabled": self.chatr_google_date_fallback,
            "googleSystemMissingSheet": document.get("googleSystemMissingSheet") or {},
            "koUnactivatedSheetEnabled": self.state.get("koUnactivatedSheetEnabled") is True,
            "koUnactivatedSheet": document.get("koUnactivatedSheet") or {},
            "koUnactivatedSystemPath": acquire.get("processingSystemKooda"),
            "koUnactivatedSystemSha256": acquire.get("systemKoodaSha256"),
            **{key: acquire.get(key) for key in (
                "chatrGoogleSource", "chatrGoogleSourceSha256", "processingChatrGoogleSource",
                "chatrGoogleDownloadReceipt", "chatrGoogleDownloadReceiptSha256",
                "chatrGoogleDates", "chatrGoogleDatesSha256", "processingChatrGoogleDates",
            )},
            "google": {"enabled": False, "status": "skipped", "reason": "user_disabled"},
            "unusedPortDeviceScope": "system_source_only",
            "cardRetentionReport": document.get("cardRetentionReport"),
            "cardRetentionReceipt": acquire.get("cardRetentionReceipt"),
            "cardRetentionSha256": acquire.get("cardRetentionSha256"),
            "cardResult": refresh["finalWorkbook"], "cardResultSha256": refresh["finalWorkbookSha256"],
            "portSubset": self.state.get("portSubset"),
            "vGroupSwapDaily": self.state.get("vGroupSwapDaily"),
            "systemMissingFDates": self.state.get("systemMissingFDates"),
            "fDateImport": self.state.get("fDateImport"),
            "expiredCardProbe": self.state.get("stages", {}).get("expiredCardProbe"),
            "taskG": self.state.get("stages", {}).get("taskG"),
            "reportDetailMode": "summary",
            "cardReceipt": document["cardReceipt"], "analysis": candidates["analysis"], "balance": acquire["balance"],
            "carrierRoutingManifest": candidates["routingManifest"], "parallelAuditReceipt": str(audit_receipt),
            "scanCacheReceipt": acquire["scanCacheReceipt"],
            "processingUsesRunPrivateLocalCopies": acquire.get("processingUsesRunPrivateLocalCopies") is True,
            "exportPerformance": acquire.get("exportPerformance") or {},
            "programConcurrency": {
                "carrierConfigPages": 4,
                "finalPhoneReadBatches": 4,
                "taskDERetainReadRequests": 8,
                "taskFInventoryPages": 4,
                "taskFRequestedIsolatedSessions": 0 if self.task_a_only else 8,
                "deliveryHashWorkers": 4,
                "liveWriteWorkers": 1,
            },
            "candidateProcessWorkbookReopens": int(candidates.get("candidateProcessWorkbookReopens") or 0),
            "carrierRefreshReceipt": refresh["refreshReceipt"], "carrierRefreshReadbackReceipt": refresh["readbackReceipt"],
            "offlineRecoveryEnabled": False, "offline": {"status": "disabled", "receipt": str(safety_path)},
            "taskBExecuted": self.authorization["taskB"], "taskBStatus": task_b_status,
            "taskBManifest": task_b.get("manifest"),
            "taskBInputWorkbook": task_b.get("inputWorkbook"),
            "taskCExecuted": self.authorization["taskC"], "taskCStatus": task_c_status,
            "taskCManifest": task_c.get("manifest"),
            "taskCInputWorkbook": task_c.get("inputWorkbook"),
            "taskCRecentDailyReplyGuardRequired": True,
            "taskDExecuted": task_d.get("executed") is True, "taskDStatus": task_d_status,
            "taskDRetentionCarrierTransferEnabled": False,
            "taskDRetentionCarrierTransferStatus": "removed_by_user",
            "taskDRetentionSummaryContractVersion": 2,
            "taskDRetentionPopulationEnabled": False,
            "taskDRetentionPopulationStatus": "removed_by_user",
            "taskDRetentionPopulationReadExecuted": False,
            "taskDRetentionSummary": task_d.get("retentionSummary"),
            "taskDNotificationSummaryContractVersion": 1,
            "taskDNotificationSummary": task_d.get("notificationSummary"),
            "taskDRetentionPopulationReceipt": task_d.get("retentionPopulationReceipt"),
            "taskDRetentionSummaryReceipt": task_d.get("retentionSummaryReceipt"),
            "taskDRetentionGroupsReceipt": task_d.get("retentionGroupsReceipt"),
            "taskDInputWorkbook": task_d.get("inputWorkbook"),
            "taskDCurrentUserAuthorized": self.authorization["taskD"],
            "taskDManifest": task_d.get("manifest"),
            "taskDRetentionReceipt": task_d.get("retentionReceipt"),
            "taskDSwapExpiryReceipt": task_d.get("swapExpiryReceipt"),
            "taskDRetentionExpiryReceipt": task_d.get("retentionExpiryReceipt"),
            "taskDSwapMetricCandidateCount": int(task_d.get("swapMetricCandidateCount") or 0),
            "taskDSwapMetricUnconfiguredCarrierSkippedCount": int(task_d.get("swapMetricUnconfiguredCarrierSkippedCount") or 0),
            "taskDSwapNotificationCount": int(task_d.get("swapNotificationCount") or 0),
            "taskDRetentionUniqueCardCount": int(task_d.get("retentionUniqueCardCount") or 0),
            **{"taskD" + key[0].upper() + key[1:]: task_d[key]
               for key in TASK_D_RETENTION_PARTITION_FIELDS if key in task_d},
            "taskDCarrierCandidateCount": int(task_d.get("carrierCandidateCount") or 0),
            "taskDCarrierActualWrite": int(task_d.get("carrierActualWrite") or 0),
            "taskDSwapExpireDate": task_d.get("swapExpireDate"),
            "taskDRetentionExpireDate": task_d.get("retentionExpireDate"),
            "taskEExecuted": task_e.get("executed") is True, "taskEStatus": task_e_status,
            "taskEInputWorkbook": task_e.get("inputWorkbook"),
            "taskECurrentUserAuthorized": self.authorization["taskE"],
            "taskEManifest": task_e.get("manifest"),
            "taskEReceipt": task_e.get("receipt"),
            "taskELedger": task_e.get("ledger"),
            "taskELedgerSnapshot": task_e.get("ledgerSnapshot"),
            "taskELedgerDedupeKey": task_e.get("ledgerDedupeKey"),
            "taskERetentionReceipt": task_e.get("retentionReceipt"),
            "taskERetentionReadExecuted": task_e.get("retentionReadExecuted") is True,
            "taskERetentionReceiptReusedFromTaskD": task_e.get("retentionReceiptReusedFromTaskD") is True,
            "taskERetentionPageCount": int(task_e.get("retentionPageCount") or 0),
            "taskERetentionTotalRecordCount": int(task_e.get("retentionTotalRecordCount") or 0),
            "taskERetentionFetchedRecordCount": int(task_e.get("retentionFetchedRecordCount") or 0),
            "taskERetentionUniqueCardCount": int(task_e.get("retentionUniqueCardCount") or 0),
            "taskERetentionWorkbookPhoneFoundCount": int(task_e.get("retentionWorkbookPhoneFoundCount") or 0),
            "taskERetentionWorkbookDeadlineKnownCount": int(task_e.get("retentionWorkbookDeadlineKnownCount") or 0),
            "taskERetentionWorkbookMissingPhoneCount": int(task_e.get("retentionWorkbookMissingPhoneCount") or 0),
            "taskERetentionWorkbookMissingDeadlineCount": int(task_e.get("retentionWorkbookMissingDeadlineCount") or 0),
            "taskERetentionInvalidNotificationDateRecordCount": int(task_e.get("retentionInvalidNotificationDateRecordCount") or 0),
            "taskERetentionDeadlineMatchPhoneCount": int(task_e.get("retentionDeadlineMatchPhoneCount") or 0),
            "taskERetentionDeadlineMatchRecordCount": int(task_e.get("retentionDeadlineMatchRecordCount") or 0),
            "taskERetentionDeadlineMatches": task_e.get("retentionDeadlineMatches") or [],
            "taskEEligibleWorkbookRowCount": int(task_e.get("eligibleWorkbookRowCount") or 0),
            "taskECandidateCount": int(task_e.get("candidateCount") or 0),
            "taskEAlreadySubmittedExcludedCount": int(task_e.get("alreadySubmittedExcludedCount") or 0),
            "taskENeverProcessSourceExcludedCount": int(task_e.get("neverProcessSourceExcludedCount") or 0),
            "taskEUnconfiguredCarrierSkippedCount": int(task_e.get("unconfiguredCarrierSkippedCount") or 0),
            "taskEDuplicateSameDeadlineCollapsedCount": int(task_e.get("duplicateSameDeadlineCollapsedCount") or 0),
            "taskEDateGroupCount": int(task_e.get("dateGroupCount") or 0),
            "taskEExpireDates": task_e.get("expireDates") or [],
            "taskEProcessedCount": int(task_e.get("processedCount") or 0),
            "taskESubmittedCount": int(task_e.get("submittedCount") or 0),
            "taskENotWrittenCount": int(task_e.get("notWrittenCount") or 0),
            "taskENotWrittenCards": task_e.get("notWrittenCards") or [],
            "taskENotWrittenSheetRequired": task_e.get("executed") is True and int(task_e.get("notWrittenCount") or 0) > 0,
            "taskENotWrittenSheetReceipt": task_e.get("notWrittenSheetReceipt"),
            "taskEMatchedNoChangeCount": int(task_e.get("matchedNoChangeCount") or 0),
            "taskEMatchedNoChangeCards": task_e.get("matchedNoChangeCards") or [],
            "taskERecoveryProof": task_e.get("recoveryProof"),
            "taskERecoveryCandidateCount": int(task_e.get("recoveryCandidateCount") or 0),
            "taskERecoveryRetriedCount": int(task_e.get("recoveryRetriedCount") or 0),
            "taskEIdempotentRecoveryNoChangeCount": int(task_e.get("idempotentRecoveryNoChangeCount") or 0),
            "taskFExecuted": task_f.get("executed") is True, "taskFStatus": task_f_status,
            "taskFBackground": task_f.get("background") is True,
            "taskFMainReportIndependent": task_f.get("mainReportIndependent") is True,
            "taskFBackgroundJob": task_f.get("backgroundJob"),
            "taskFBackgroundJobSha256": task_f.get("backgroundJobSha256"),
            "taskFBackgroundState": task_f.get("backgroundState"),
            "taskFBackgroundReport": task_f.get("backgroundReport"),
            "taskFBackgroundDirectory": task_f.get("backgroundDirectory"),
            "taskFBackgroundLaunchReceipt": task_f.get("backgroundLaunchReceipt"),
            "taskFBackgroundLaunchReceiptSha256": task_f.get("backgroundLaunchReceiptSha256"),
            "taskFBackgroundLaunchError": task_f.get("error") if task_f.get("reason") == "background_launch_failed" else None,
            "taskFIncrementalWorkbook": task_f.get("incrementalWorkbook"),
            "taskFIncrementalReceipt": task_f.get("incrementalReceipt"),
            "taskFNewActivationImportedCount": task_f.get("newActivationImportedCount"),
            "taskFNotifyIncrementalWorkbook": task_f.get("notifyIncrementalWorkbook") is True,
            "reportDateCachePath": date_cache.get("path"),
            "reportDateCacheSha256": date_cache.get("sha256"),
            "taskFCurrentUserAuthorized": self.authorization["taskF"],
            "taskFAutomaticCatalogSync": task_f.get("automaticCatalogSync") is True,
            "taskFPostDocumentSupplemental": task_f.get("postDocumentSupplemental") is True,
            "taskFDocumentGeneratedBeforeLookup": task_f.get("documentGeneratedBeforeLookup") is True,
            "taskFResultsApplyNextExport": task_f.get("resultsApplyNextExport") is True,
            "taskFDocumentDateSource": "system_then_frozen_local_cache",
            "taskFResultsWriteBackToDocument": False,
            "taskFReportBackfillMode": task_f.get("reportBackfillMode") is True,
            "taskFKoRefreshEnabled": task_f.get("koRefreshEnabled") is True,
            "taskFDocumentKoRefreshRows": task_f.get("documentKoRefreshRows"),
            "taskFDocumentPath": task_f.get("documentPath"),
            "taskFDocumentSha256": task_f.get("documentSha256"),
            "taskFDocumentRunId": task_f.get("documentRunId"),
            "taskFDocumentReceipt": document.get("cardReceipt"),
            "taskFReadyForUploadCount": int(task_f.get("readyForUploadCount") or 0),
            "taskFDocumentExcludedCount": int(task_f.get("documentExcludedCount") or 0),
            "taskFDocumentMissingActivationIccidCount": int(task_f.get("documentMissingActivationIccidCount") or 0),
            "taskFDocumentMissingDateRows": task_f.get("documentMissingDateRows"),
            "taskFDocumentCreationFallbackRows": task_f.get("documentCreationFallbackRows"),
            "taskFDocumentLookupNeededRows": task_f.get("documentLookupNeededRows"),
            "taskFDateEligibilityMode": task_f.get("dateEligibilityMode"),
            "taskFDocumentMissingDateIccidCount": task_f.get("documentMissingDateIccidCount"),
            "taskFDocumentMissingCutoffRows": task_f.get("documentMissingCutoffRows"),
            "taskFRefreshExistingMissingCount": int(task_f.get("refreshExistingMissingCount") or 0),
            "taskFRefreshedMissingActivationCount": int(task_f.get("refreshedMissingActivationCount") or 0),
            "taskFRefreshedBlankRecordCount": int(task_f.get("refreshedBlankRecordCount") or 0),
            "taskFDocumentActivationPresentRows": int(task_f.get("documentActivationPresentRows") or 0),
            "taskFCloudUploadAttempted": False,
            "taskFDatabaseActivationMissingOnly": task_f.get("databaseActivationMissingOnly") is True,
            "systemKoodaSource": "本轮10日表Kooda来源；不读取全平台库存",
            "systemKoodaPath": acquire["systemKoodaInventory"],
            "systemKoodaSha256": acquire["systemKoodaSha256"],
            "systemKoodaRows": acquire.get("systemKoodaRows", 0),
            "systemKoodaMatchedRows": ((card_receipt_payload.get("verification") or {}).get("system_kooda_source_review") or {}).get("matched_rows", 0),
            "systemKoodaMissingActivationRows": int(task_f_red_review.get("expected_rows") or 0),
            "taskFDocumentMissingActivationRedRows": int(task_f_red_review.get("expected_rows") or 0),
            "taskFDocumentFullRedRows": int(task_f_red_review.get("actual_full_red_rows") or 0),
            "taskFDocumentRedReviewPassed": task_f_red_review.get("matches") is True,
            "taskFMissingActivationFillRgb": task_f_red_review.get("fill_rgb") or "FFC7CE",
            "taskFCatalogReceipt": task_f.get("catalogReceipt"),
            "taskFCatalogSource": task_f.get("catalogSource"),
            "taskFCatalogSourceSha256": task_f.get("catalogSourceSha256"),
            "taskFCatalogSourceRowCount": int(task_f.get("catalogSourceRowCount") or 0),
            "taskFCatalogFoundCount": int(task_f.get("catalogFoundCount") or 0),
            "taskFCatalogActivationMissingCount": int(task_f.get("catalogActivationMissingCount") or 0),
            "taskFCatalogAlreadyImported": task_f.get("catalogAlreadyImported") is True,
            "taskFOnlineLookupTriggered": task_f.get("onlineLookupTriggered") is True,
            "taskFIccidLibrary": task_f.get("iccidLibrary"),
            "taskFLatestIccidLibrary": task_f.get("latestIccidLibrary"),
            "taskFIccidLibraryRecordCount": int(task_f.get("iccidLibraryRecordCount") or 0),
            "taskFIccidLibraryFoundCount": int(task_f.get("iccidLibraryFoundCount") or 0),
            "taskFIccidLibraryActivationMissingCount": int(task_f.get("iccidLibraryActivationMissingCount") or 0),
            "taskFIccidLibraryNotFoundCount": int(task_f.get("iccidLibraryNotFoundCount") or 0),
            "taskFDeadlineWorkbook": task_f.get("deadlineWorkbook"),
            "taskFDeadlineExportReceipt": task_f.get("deadlineExportReceipt"),
            "taskFDeadlineRecordCount": int(task_f.get("deadlineRecordCount") or 0),
            "taskFDeadlineActivationCount": int(task_f.get("deadlineActivationCount") or 0),
            "taskFDeadlineMissingDateCount": int(task_f.get("deadlineMissingDateCount") or 0),
            "taskFLoginRequired": task_f.get("loginRequired") is True,
            "taskFLoginUrl": task_f.get("loginUrl"),
            "taskFLoginInstruction": task_f.get("loginInstruction"),
            "taskFLoginReceipt": task_f.get("loginReceipt"),
            "taskFInventoryReceipt": task_f.get("inventory"),
            "taskFPendingReceipt": task_f.get("pending"),
            "taskFLookupReceipt": task_f.get("lookupReceipt"),
            "taskFImportReceipt": task_f.get("importReceipt"),
            "taskFInventoryCount": int(task_f.get("inventoryCount") or 0),
            "taskFInventoryTotalRows": int(task_f.get("inventoryTotalRows") or 0),
            "taskFMissingCardStoreCount": int(task_f.get("missingCardStoreCount") or 0),
            "taskFInvalidCardStoreCount": int(task_f.get("invalidCardStoreCount") or 0),
            "taskFAlreadyQueriedCount": int(task_f.get("alreadyQueriedCount") or 0),
            "taskFPendingCount": int(task_f.get("pendingCount") or 0),
            "taskFProgress": task_f.get("progress"),
            "taskFProgressIntervalSeconds": int(task_f.get("progressIntervalSeconds") or 0),
            "taskFDatabaseMissingOnly": task_f.get("databaseMissingOnly") is True,
            "taskFFoundImportedCount": int(task_f.get("foundImportedCount") or 0),
            "taskFSchemaUpgradePendingCount": int(task_f.get("schemaUpgradePendingCount") or 0),
            "taskFSchemaUpgradedCount": int(task_f.get("schemaUpgradedCount") or 0),
            "taskFDatabaseCurrentSchemaCount": int(task_f.get("databaseCurrentSchemaCount") or 0),
            "taskFRecordSchemaVersion": int(task_f.get("recordSchemaVersion") or 0),
            "taskFRecordedWebFields": task_f.get("recordedWebFields") or [],
            "taskFFullSourceDataIncluded": task_f.get("fullSourceDataIncluded") is True,
            "taskFTransientErrorCount": int(task_f.get("transientErrorCount") or 0),
            "taskFRequestedWorkers": int(task_f.get("requestedWorkers") or 0),
            "taskFEffectiveWorkers": int(task_f.get("effectiveWorkers") or 0),
            "taskFIndependentAspNetSessions": task_f.get("independentAspNetSessions") is True,
            "taskFUniqueAspNetSessionCount": int(task_f.get("uniqueAspNetSessionCount") or 0),
            "taskFWorkerSessionMode": task_f.get("workerSessionMode") or "",
            "taskFSharedAspNetSessionConcurrentWorkers": task_f.get("sharedAspNetSessionConcurrentWorkers") is True,
            "taskFAdaptiveConcurrency": task_f.get("adaptiveConcurrency") is True,
            "taskFSharedSessionForcesSerial": task_f.get("sharedSessionForcesSerial") is True,
            "taskFRequestedWorkersSatisfied": task_f.get("requestedWorkersSatisfied") is True,
            "taskFIndependentFormLoginAttempted": task_f.get("independentFormLoginAttempted") is True,
            "taskFIndependentFormLoginSucceeded": task_f.get("independentFormLoginSucceeded") is True,
            "taskFFullRequestedConcurrencyRequired": task_f.get("fullRequestedConcurrencyRequired") is True,
            "taskFStrictIdentityGuard": task_f.get("strictIdentityGuard") or "",
            "taskFIdentityMismatchStoredAsTerminal": task_f.get("identityMismatchStoredAsTerminal") is True,
            "taskFSharedSessionNotFoundRequiresSerialConfirmation": task_f.get("sharedSessionNotFoundRequiresSerialConfirmation") is True,
            "taskFSharedSessionSerialConfirmationCount": int(task_f.get("sharedSessionSerialConfirmationCount") or 0),
            "taskFSharedSessionSerialConfirmationFailureCount": int(task_f.get("sharedSessionSerialConfirmationFailureCount") or 0),
            "taskFPerWorkerDelayMs": int(task_f.get("perWorkerDelayMs") or 0),
            "taskFBatchMode": task_f.get("batchMode") is True,
            "taskFBatchSize": int(task_f.get("batchSize") or 0),
            "taskFBatchCount": int(task_f.get("batchCount") or 0),
            "taskFCompletedBatchCount": int(task_f.get("completedBatchCount") or 0),
            "taskFDatabaseCheckpointAfterEveryBatch": task_f.get("databaseCheckpointAfterEveryBatch") is True,
            "taskFBrowser": task_f.get("browser"),
            "taskFTransportPriority": task_f.get("transportPriority") or [],
            "taskFSameOriginHttpRequests": int(task_f.get("sameOriginHttpRequests") or 0),
            "taskFBackgroundJavascriptFallbackRequests": int(task_f.get("backgroundJavascriptFallbackRequests") or 0),
            "taskFDatabase": None if self.task_a_only else task_f.get("database") or str(TASK_F_DB),
            "taskFDatabaseSnapshot": task_f.get("databaseSnapshot"),
            "silentBrowserMode": True, "browserForegroundActivationAttempted": False,
            "missingCarrierGroups": [],
            "restZoneEntryEnabled": routing.get("restZoneEntryEnabled") is True,
            "restZoneEntryPolicy": routing.get("restZoneEntryPolicy"),
            "restZoneEntryBQualifiedHeldCount": int(routing.get("restZoneEntryBQualifiedHeldCount") or 0),
            "restZoneEntryChatrQualifiedHeldCount": int(routing.get("restZoneEntryChatrQualifiedHeldCount") or 0),
            "restZoneEntryCandidateCount": int(routing.get("restZoneEntryCandidateCount") or 0),
            "neverProcessProtection": {
                "carrierNames": routing.get("neverProcessSourceCarrierNames") or ["快过期续费C", "快过期续费T"],
                "excludedCount": int(routing.get("neverProcessSourceExcludedCount") or 0),
                "candidateCount": 0,
                "actualWriteCount": 0,
                "passed": True,
            },
            "b1B11Use2ToB11TenDaySendThreshold": 240,
            "b1B11ToB12TenDaySendThreshold": 240,
            "b1vB11vTenDaySendThreshold": 240,
            "aZoneKoodaToB12Candidates": value("a_zone_kooda_to_b12"), "aZoneKoodaToB12Writes": value("a_zone_kooda_to_b12", "actualWrite"),
            "a1KiniToB12Candidates": value("a1_kini_latest_lt4_to_b12"), "a1KiniToB12Writes": value("a1_kini_latest_lt4_to_b12", "actualWrite"),
            "a1KiniHeldCounts": routing.get("a1KiniHeldCounts") or {},
            "bZoneTodayGte50ZeroCandidates": value("b1_b11_b12_today_gte50_zero_to_e"), "bZoneTodayGte50ZeroWrites": value("b1_b11_b12_today_gte50_zero_to_e", "actualWrite"),
            "aZoneChatrZeroToECCandidates": value("chatr_to_e"), "aZoneChatrZeroToECWrites": value("chatr_to_e", "actualWrite"),
            "b1B11Use2Lt240Candidates": value("b1_b11_use2_lt240_to_b11"), "b1B11Use2Lt240Writes": value("b1_b11_use2_lt240_to_b11", "actualWrite"),
            "b1B11Gte240Candidates": value("b1_b11_gte240_to_b12"), "b1B11Gte240Writes": value("b1_b11_gte240_to_b12", "actualWrite"),
            "b1vB11vUse2Lt240Candidates": value("b1v_b11v_use2_lt240_to_b11v"), "b1vB11vUse2Lt240Writes": value("b1v_b11v_use2_lt240_to_b11v", "actualWrite"),
            "b1vB11vGte240Candidates": value("b1v_b11v_gte240_to_b12v"), "b1vB11vGte240Writes": value("b1v_b11v_gte240_to_b12v", "actualWrite"),
            "c2PreviousDayLt5ToA11Candidates": value("c2_previous_day_reply_rate_lt5_to_a11"), "c2PreviousDayLt5ToA11Writes": value("c2_previous_day_reply_rate_lt5_to_a11", "actualWrite"),
            "c2DailyStatisticsSendTotal": routing.get("c2DailyStatisticsSendTotal"),
            "c2DailyStatisticsSendAlertTriggered": routing.get("c2DailyStatisticsSendAlertTriggered"),
            "c2DailyExecution": routing.get("c2DailyExecution"),
            "aZoneGte4ToA1Candidates": value("a_zone_gte4_to_a1"), "aZoneGte4ToA1Writes": value("a_zone_gte4_to_a1", "actualWrite"),
            "aZoneLt4ToA12Candidates": value("a_zone_lt4_to_a12"), "aZoneLt4ToA12Writes": value("a_zone_lt4_to_a12", "actualWrite"),
            "eReplyRecoveryCandidates": sum(item["candidateCount"] for key, item in by_route.items() if key.startswith("e_reply_recovery_")),
            "eReplyRecoveryWrites": sum(item.get("actualWrite", 0) for key, item in by_route.items() if key.startswith("e_reply_recovery_")),
            "b13TargetCandidates": 0, "b13TargetWrites": 0,
            "groups": groups,
            "timings": [{"stage": item["stage"], "duration": f"{item['durationSeconds']}秒"} for item in self.state["timings"]],
            "commandTimings": list(self.state.get("commandTimings") or []),
            "extraReceipts": extra_receipts,
            "extraAnomalies": [
                "任务B、任务C、任务D、任务E仅在当前真实用户授权门禁满足时执行；任务F权威目录同步默认自动执行",
                "任务D默认关闭；未授权时不生成候选、不读取卡保留、不查询任务D号码、不修改运营商、不写到期标记",
                "任务E默认关闭；未授权时不生成候选、不读取卡保留记录、不读取去重台账、不查询任务E号码、不写到期标记",
                "F只补查本轮原始10日表与本地F库均无有效激活日期的ICCID，同ICCID去重、仅缺截止日不补查；按既定独立会话验证和限速后台补查并写本地库，不扩展全平台库存或表外库记录、不阻塞主表，后台新增日期留到下轮A",
                "本轮Kooda解析后仍缺激活日期行整行浅红；下次新鲜导出系统或冻结库已有日期时取消；双日期空白仍按未换号新卡",
                ("Google完整关联及差集停用；主表日期逐字段系统优先，Kooda空值只读冻结本地库，Chatr空值只读本轮新鲜Google专用日期；不覆盖系统值、不从激活日期倒推系统截止日"
                 if self.chatr_google_date_fallback else
                 "历史兼容模式：Google日期补全未启用，Chatr仅使用系统日期；Kooda空值只读冻结本地库"),
                ("休息区转入已开启；按最新日发送>=50且回复=0转入E-T/E-C，V组仍禁止转入；已有休息区号码有回复后允许按备份恢复"
                 if routing.get("restZoneEntryEnabled") else
                 "休息区转入临时关闭；命中号码保持当前运营商并停止后续较低优先级规则，有回复后恢复流程不变"),
                "B区V组候选已合并到主路由一次工作簿扫描；文档生成与只读分析并发，V组及其它运营商实际写入继续严格串行",
                "旧泛化下架恢复仍关闭；过期下架卡C/T仅按最新人工授权执行截止日严格早于平台今天的独立存活测试，异步未确认不重试",
                "巡检策略禁止预览，本轮未生成或打开工作簿预览",
            ],
            "reportPath": str(report),
        }
        if self.task_a_only:
            config["extraAnomalies"][0] = "本轮仅执行任务A；任务B、C、D、E、F完整跳过（task_a_only）"
            config["extraAnomalies"][3] = "任务A只读冻结日期缓存用于系统空值补足；任务F未生成候选、未写库、未登录或补查、未导出F新增文件"
        for task_key in ('taskB', 'taskC', 'taskD', 'taskE'):
            task_state = self.state.get('stages', {}).get(task_key) or {}
            if task_state.get('dataAnomalySkippedCount'):
                config['extraAnomalies'].append(f"任务{task_key[-1]}异常数据跳过 {task_state['dataAnomalySkippedCount']} 条，不计成功；待人工处理。")
                evidence = task_state.get('dataAnomalyEvidencePath') or task_state.get('retentionReceipt')
                if evidence:
                    config['extraAnomalies'].append(f"[任务{task_key[-1]}异常明细与原始依据](<{evidence}>)")
        config_path = self.receipts / f"opensms_patrol_finalize_{self.run_id}_config.json"
        atomic_json(config_path, config)
        return config_path

    def import_current_f_dates(self, acquire: dict[str, Any]) -> None:
        """Standing user authorization is limited to this normal A run's new file."""
        if self.task_a_only:
            self.state['fDateImport'] = {'status': 'skipped_task_a_only', 'submitted': False}
            self._save_state()
            return
        receipt_path = self.receipts / f'opensms_f_date_import_{self.run_id}.json'
        result = {'runId': self.run_id, 'status': 'failed_before_submission', 'submitted': False}
        try:
            from opensms_system_missing_f_dates import export_difference
            from opensms_f_date_import import prepare_import
            difference = export_difference(acquire['sourceArchive'], acquire['sourceSha256'],
                                           self.run_id, self.delivery, self.receipts,
                                           report_date_cache=self.state.get('reportDateCache'))
            self.state['systemMissingFDates'] = difference
            if difference['exportedRowCount'] == 0:
                result['status'] = 'skipped_no_new_data'
            else:
                inputs = prepare_import(difference, self.run_id)
                input_path = self.receipts / f'opensms_f_date_import_{self.run_id}_input.json'
                if input_path.exists() or receipt_path.exists():
                    raise ValueError('F_IMPORT_REPLAY_FORBIDDEN')
                atomic_json(input_path, inputs)
                self.command('task_a_f_date_import',
                             [NODE, SCRIPTS / 'opensms_f_date_import.mjs', input_path, receipt_path],
                             env={'OPENSMS_F_DATE_IMPORT_RUN_ID': self.run_id})
                result = read_json(receipt_path)
        except Exception as exc:
            if Path(str(receipt_path) + '.intent.json').is_file():
                result.update(status='submission_outcome_uncertain', submitted=True)
            if receipt_path.is_file():
                saved = read_json(receipt_path)
                if saved.get('runId') == self.run_id:
                    result = saved
            if not result.get('error'):
                result['error'] = str(exc) if str(exc).startswith('F_IMPORT_') else 'F_IMPORT_OPERATION_FAILED'
        self.state['fDateImport'] = result
        self._save_state()

    def run_expired_card_probe(self, acquire: dict[str, Any]) -> None:
        """Publish only; never wait for network/tests before final delivery."""
        started = time.monotonic()
        if getattr(self.args, 'restore_unexpired_to_a11', False):
            self.complete_stage('expiredCardProbe', {'status': 'skipped_manual_restore_scope'}, started)
            return
        from opensms_expired_card_probe_background import prepare_and_start
        prior = (self.state.get('stages') or {}).get('expiredCardProbe')
        if prior:
            return  # Never dispatch this same parent run twice.
        try:
            result = prepare_and_start(self, acquire)
        except Exception as exc:
            # A background launch error must not invalidate the main workbook.
            result = {'runId': self.run_id, 'scope': 'expired_zones_all_listing_states_v2',
                      'status': 'failed_dispatch_no_business', 'background': True,
                      'statusIsLaunchSnapshot': True, 'mainReportIndependent': True,
                      'deadlineFilterEnabled': False, 'executed': False,
                      'waitForCompletion': False, 'error': str(exc)}
            failure = self.receipts / f'opensms_expired_probe_dispatch_failure_{self.run_id}.json'
            atomic_json(failure, result)
            result.update(dispatchFailureReceipt=str(failure), dispatchFailureSha256=sha256(failure))
        self.complete_stage('expiredCardProbe', result, started)

    def release_expired_card_probe(self) -> None:
        """Open the local gate only after final output and manifest verification."""
        from opensms_expired_card_probe_background import release_after_delivery
        probe = self.state.get('stages', {}).get('expiredCardProbe')
        try:
            release_after_delivery(probe)
        except Exception as exc:
            # Preserve successful delivery; the unopened gate forbids business.
            # Never modify the already-hashed parent state or main manifest.
            if probe:
                probe['gateError'] = str(exc)  # Returned JSON only, no parent rewrite.
            if probe and probe.get('backgroundState'):
                try:
                    path = Path(probe['backgroundState'])
                    from opensms_expired_card_probe_background import read, update
                    child = read(path)
                    child.update(status='failed_delivery_gate_no_business', error=str(exc))
                    update(path, child)
                except Exception:
                    # Even a local disk error cannot turn verified main output
                    # into failure. No gate means no background business.
                    probe['gateStateWriteUnconfirmed'] = True

    def current_difference_receipts(self) -> Path:
        """Resolve this run's already verified local supplement, never regenerate it."""
        saved = self.state.get("systemMissingFDates")
        if not saved:
            return self.receipts
        path = Path(saved.get("receipt") or "").resolve()
        root = (self.delivery / "receipts").resolve()
        if path.name != f"opensms_system_missing_f_dates_{self.run_id}.json":
            raise PatrolError("本轮差异导出回执身份不符")
        allowed = path.parent == root
        for key, directory in (("taskDContinuationAttempts", "task_d_continuation_attempts"),
                               ("taskGContinuationAttempts", "task_g_continuation_attempts")):
            for attempt in self.state.get(key) or []:
                recorded = Path(attempt.get("attemptDirectory") or "").resolve()
                allowed = allowed or (attempt.get("authorizationSource") == "current-user"
                    and path.parent == recorded and recorded.parent == root / directory
                    and re.fullmatch(r"[0-9]+", recorded.name) is not None)
        if not allowed or not path.is_file() or read_json(path) != saved:
            raise PatrolError("本轮差异导出回执目录或内容不符")
        # export_difference still independently verifies source/cache/template,
        # snapshot and output hashes before reuse. Its import policy is unchanged.
        return path.parent

    def finalize(self, acquire: dict[str, Any], candidates: dict[str, Any], document: dict[str, Any], refresh: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        report = self.delivery / f"OpenSMS_10日报表巡检_{self.run_id}.md"
        stable_audit = self.receipts / f"opensms_parallel_audit_{self.run_id}_receipt.json"
        initial_audit = self.receipts / f"opensms_parallel_audit_{self.run_id}_initial.json"
        final_audit = self.receipts / f"opensms_parallel_audit_{self.run_id}_final.json"
        finalize_receipt = self.receipts / f"opensms_report_generation_{self.run_id}.json"
        # Removed by the user; also clear any stale checkpoint link.
        # The full final workbook retains every row and its original sheets.
        self.state["portSubset"] = None
        from opensms_system_missing_f_dates import export_difference
        from opensms_v_swap_daily import export_daily
        from opensms_local_jobs import run_local_jobs
        verified_groups = tuple(self.state.get("writeGroups", {}).values())
        difference_receipts = self.current_difference_receipts()
        supplements, scheduling = run_local_jobs({
            "systemMissingFDates": lambda: export_difference(
                acquire["sourceArchive"], acquire["sourceSha256"], self.run_id,
                self.delivery, difference_receipts, report_date_cache=self.state.get("reportDateCache")),
            "vGroupSwapDaily": lambda: export_daily(
                STATE_ROOT / "v_group_swap_daily", self.run_id, self.delivery, self.receipts,
                verified_groups),
        }, workers=2)
        self.state.update(supplements)
        self.state["finalizeLocalJobScheduling"] = scheduling
        self._save_state()

        def generate(config_path: Path) -> None:
            self.command(
                "finalize_report",
                [PYTHON, SCRIPTS / "opensms_finalize_patrol_report.py", config_path, report, "--receipt", finalize_receipt],
            )
            self.command("delivery_manifest", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])

        config_path = self.build_finalize_config(acquire, candidates, document, refresh, stable_audit, "完成（待并行审计）")
        generate(config_path)
        initial = self.command(
            "audit_initial",
            [SCRIPTS / "run_opensms_parallel_audit.sh", "--config", config_path, "--delivery-dir", self.delivery, "--output", initial_audit, "--workers", "auto", "--json"],
            allow_codes=(0, 2),
        )
        shutil.copy2(initial_audit, stable_audit)
        initial_summary = read_json(initial_audit).get("summary") or {}
        status = f"完成（并行审计{initial_summary.get('passed', 0)}/{initial_summary.get('checks_total', 0)}通过，{initial_summary.get('warnings', 0)}提示，{initial_summary.get('failed', 0)}失败）"
        config_path = self.build_finalize_config(acquire, candidates, document, refresh, stable_audit, status)
        generate(config_path)
        final = self.command(
            "audit_final",
            [SCRIPTS / "run_opensms_parallel_audit.sh", "--config", config_path, "--delivery-dir", self.delivery, "--output", final_audit, "--workers", "auto", "--json"],
            allow_codes=(0, 2),
        )
        final_data = read_json(final_audit)
        summary = final_data.get("summary") or {}
        if int(summary.get("failed") or 0) != 0 or int(summary.get("failed_writes") or 0) != 0:
            raise PatrolError(f"最终并行审计失败: {summary}")
        shutil.copy2(final_audit, stable_audit)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="opensms-final-integrity") as pool:
            integrity_jobs = [
                pool.submit(ensure_xlsx, Path(acquire["sourceArchive"]), "交付源表"),
                pool.submit(ensure_xlsx, Path(refresh["finalWorkbook"]), "交付最终表"),
            ]
            for job in integrity_jobs:
                job.result()
        if not report.is_file() or report.stat().st_size < 1000:
            raise PatrolError("最终报告缺失或大小异常")
        current_sources = [
            ROOT / "opensms_generate_opr_zero_candidates.py",
            SCRIPTS / "opensms_balance_readonly_raw.mjs",
            SCRIPTS / "opensms_resolve_active_carriers.mjs",
            SCRIPTS / "opensms_patrol_runner.py",
        ]
        legacy_names = tuple("B" + suffix for suffix in ("31", "32", "33"))
        for source in current_sources[:-1]:
            content = source.read_text(encoding="utf-8")
            if any(name in content for name in legacy_names):
                raise PatrolError(f"当前执行代码仍含已删除运营商组: {source}")
        data = {
            "report": str(report), "config": str(config_path), "auditReceipt": str(stable_audit),
            "auditPassed": int(summary.get("passed") or 0), "auditWarnings": int(summary.get("warnings") or 0),
            "auditFailed": int(summary.get("failed") or 0),
            "auditPending": int(summary.get("pending") or 0),
        }
        return self.complete_stage("finalize", data, started)

    def summary(self, acquire: dict[str, Any], candidates: dict[str, Any], refresh: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
        task_b = self.state.get("stages", {}).get("taskB") or {}
        analysis = read_json(candidates["analysis"])
        balance = read_json(acquire["balance"])
        routing = read_json(candidates["routingManifest"])
        write_groups = list(self.state["writeGroups"].values())
        task_g = self.state.get("stages", {}).get("taskG") or {}
        g_writes = int(task_g.get("enteredCount") or 0) + int(task_g.get("restoredCount") or 0)
        task_c = self.state.get("stages", {}).get("taskC") or {}
        task_c_manifest = read_json(task_c["manifest"]) if task_c.get("manifest") else {}
        task_d = self.state.get("stages", {}).get("taskD") or {}
        task_d_swap_receipt = read_json(task_d["swapExpiryReceipt"]) if task_d.get("swapExpiryReceipt") else {}
        task_d_retention_receipt = read_json(task_d["retentionExpiryReceipt"]) if task_d.get("retentionExpiryReceipt") else {}
        task_e = self.state.get("stages", {}).get("taskE") or {}
        task_e_receipt = read_json(task_e["receipt"]) if task_e.get("receipt") else {}
        task_f = self.state.get("stages", {}).get("taskF") or {}
        document_stage = self.state.get("stages", {}).get("document") or {}
        card_receipt_payload = read_json(document_stage["cardReceipt"]) if document_stage.get("cardReceipt") else {}
        card_receipt_payload = card_receipt_payload.get("cardSlotReport") or card_receipt_payload
        task_f_red_review = (
            (card_receipt_payload.get("verification") or {}).get("task_f_missing_activation_red_review")
            or {}
        )
        task_d_carrier_groups = [
            item for item in write_groups if str(item.get("route") or "").startswith("task_d_retain_")
        ]
        stage_timings = self.state.get("timings") or []
        slowest_stages = sorted(
            (
                {
                    "stage": str(item.get("stage") or "unknown"),
                    "durationSeconds": float(item.get("durationSeconds") or 0),
                }
                for item in stage_timings
            ),
            key=lambda item: item["durationSeconds"],
            reverse=True,
        )[:5]
        result = {
            "status": "success",
            **execution_contract(self.task_a_only),
            "unexpiredRestore": self.state.get("stages", {}).get("unexpiredRestore"),
            "runId": self.run_id,
            "serverDate": acquire["serverDate"],
            "candidateCount": sum(item.get("candidateCount", 0) for item in write_groups) + int(task_g.get("candidateCount") or 0),
            "backendValidCount": sum(item.get("backendValid", 0) for item in write_groups) + g_writes,
            "actualWriteCount": sum(item.get("actualWrite", 0) for item in write_groups) + g_writes,
            "independentVerificationPassed": sum(1 for item in write_groups if item.get("result") == "passed" and item.get("independentReadbackPerformed", True)) + int(task_g.get("verifiedBatchCount") or 0),
            "noWriteGroupsStageVerified": sum(1 for item in write_groups if item.get("noWrite") and item.get("stageReadbackVerified")),
            "liveWorkbookRefreshCount": refresh["liveRefreshCount"],
            "restZoneEntryEnabled": routing.get("restZoneEntryEnabled") is True,
            "restZoneEntryHeldCount": (
                int(routing.get("restZoneEntryBQualifiedHeldCount") or 0)
                + int(routing.get("restZoneEntryChatrQualifiedHeldCount") or 0)
            ),
            "restZoneEntryCandidateCount": int(routing.get("restZoneEntryCandidateCount") or 0),
            "currentZero": analysis.get("currentZero"),
            "overallReplyRatePct": (analysis.get("overall") or {}).get("rate_pct"),
            "scopeComparison": analysis.get("scopeComparison"),
            "balanceGroups": build_balance_summary_groups(balance),
            "carrierReplyComparison": balance.get("carrierReplyComparison"),
            "low20": [{"group": row.get("group"), "percentRemaining": row.get("percentRemaining")} for row in balance.get("low20") or []],
            "low10": [{"group": row.get("group"), "percentRemaining": row.get("percentRemaining")} for row in balance.get("low10") or []],
            "audit": {"passed": final["auditPassed"], "warnings": final["auditWarnings"], "failed": final["auditFailed"], "pending": int(final.get("auditPending") or 0)},
            "taskB": {
                "status": "passed" if (self.state.get("stages", {}).get("taskB") or {}).get("executed") else "skipped",
                **self.summarize_write_groups("task_b_"),
                "sourceCarrierScope": task_b.get("sourceCarrierScope"),
                "sourceCarrierCoverage": task_b.get("sourceCarrierCoverage"),
                "sourceCarrierCoveragePassed": task_b.get("sourceCarrierCoveragePassed"),
                "unconfiguredCarrierSkippedCount": task_b.get("unconfiguredCarrierSkippedCount"),
                **anomaly_summary(task_b),
                "retainedSourceCarrierNames": task_b.get("retainedSourceCarrierNames") or [],
                "retainedSourceExcludedCount": task_b.get("retainedSourceExcludedCount"),
                "retainedSourceExcludedCounts": task_b.get("retainedSourceExcludedCounts") or {},
                "expiredRetainedSourceExcludedCount": task_b.get("expiredRetainedSourceExcludedCount"),
                "tomorrowRetainedSourceExcludedCount": task_b.get("tomorrowRetainedSourceExcludedCount"),
            },
            "taskC": {
                "status": "passed" if task_c.get("executed") is True else "skipped",
                **self.summarize_write_groups("task_c_"),
                "unconfiguredCarrierSkippedCount": task_c_manifest.get("unconfiguredCarrierSkippedCount"),
                **anomaly_summary(task_c_manifest),
                "taskBPriorityLockEnforced": task_c_manifest.get("taskBPriorityLockEnforced") is True,
                "taskBPriorityExcludedCount": int(task_c_manifest.get("taskBPriorityExcludedCount") or 0),
                "taskBPriorityExcludedCounts": task_c_manifest.get("taskBPriorityExcludedCounts") or {},
                "recentDailyReplyRateGuard": task_c_manifest.get("recentDailyReplyRateGuard"),
                "recentDailyReplyRateExcludedCount": task_c_manifest.get("recentDailyReplyRateExcludedCount"),
                "recentDailyReplyRateExcludedCounts": task_c_manifest.get("recentDailyReplyRateExcludedCounts") or {},
            },
            "taskD": {
                "status": task_d.get("status"),
                "retentionCarrierTransferEnabled": False,
                "retentionCarrierTransferStatus": "removed_by_user",
                **removed_population_fields(),
                "executed": task_d.get("executed") is True,
                "retentionSummary": task_d.get("retentionSummary"),
                "notificationSummary": task_d.get("notificationSummary"),
                "swapMetricCandidateCount": task_d.get("swapMetricCandidateCount", 0),
                "swapMetricUnconfiguredCarrierSkippedCount": task_d.get("swapMetricUnconfiguredCarrierSkippedCount", 0),
                "swapNotificationCount": task_d.get("swapNotificationCount", 0),
                "retentionUniqueCardCount": task_d.get("retentionUniqueCardCount", 0),
                **{key: task_d[key] for key in TASK_D_RETENTION_PARTITION_FIELDS if key in task_d},
                "carrierCandidateCount": task_d.get("carrierCandidateCount", 0),
                "carrierActualWrite": task_d.get("carrierActualWrite", 0),
                "swapExpireDate": task_d.get("swapExpireDate"),
                "retentionExpireDate": task_d.get("retentionExpireDate"),
                "businessReceiptsPassed": task_d.get("status") == "passed",
                "expiryTransactionalReceiptsPassed": (
                    task_d_swap_receipt.get("transactionalVerificationPassed") is True
                    and task_d_retention_receipt.get("transactionalVerificationPassed") is True
                ),
                "expiryIndependentReadbackAvailable": (
                    task_d_swap_receipt.get("independentReadbackAvailable") is True
                    and task_d_retention_receipt.get("independentReadbackAvailable") is True
                ),
                "carrierIndependentReadbackPassed": None,
                "carrierReadbackStatus": "not_applicable_transfer_removed",
            },
            "taskE": {
                "status": task_e.get("status"),
                "currentUserAuthorized": self.authorization["taskE"],
                "eligibleWorkbookRowCount": task_e.get("eligibleWorkbookRowCount", 0),
                "candidateCount": task_e.get("candidateCount", 0),
                "alreadySubmittedExcludedCount": task_e.get("alreadySubmittedExcludedCount", 0),
                "neverProcessSourceExcludedCount": task_e.get("neverProcessSourceExcludedCount", 0),
                "unconfiguredCarrierSkippedCount": task_e.get("unconfiguredCarrierSkippedCount", 0),
                **anomaly_summary(task_e),
                "dateGroupCount": task_e.get("dateGroupCount", 0),
                "expireDates": task_e.get("expireDates") or [],
                "processedCount": task_e.get("processedCount", 0),
                "submittedCount": task_e.get("submittedCount", 0),
                "notWrittenCount": task_e.get("notWrittenCount", 0),
                "notWrittenCards": task_e.get("notWrittenCards") or [],
                "matchedNoChangeCount": task_e.get("matchedNoChangeCount", 0),
                "matchedNoChangeCards": task_e.get("matchedNoChangeCards") or [],
                "retentionPageCount": task_e.get("retentionPageCount", 0),
                "retentionReadExecuted": task_e.get("retentionReadExecuted") is True,
                "retentionTotalRecordCount": task_e.get("retentionTotalRecordCount", 0),
                "retentionUniqueCardCount": task_e.get("retentionUniqueCardCount", 0),
                "retentionDeadlineMatchPhoneCount": task_e.get("retentionDeadlineMatchPhoneCount", 0),
                "retentionDeadlineMatchRecordCount": task_e.get("retentionDeadlineMatchRecordCount", 0),
                "retentionDeadlineMatches": task_e.get("retentionDeadlineMatches") or [],
                "retentionDeadlineAlertRequired": int(task_e.get("retentionDeadlineMatchPhoneCount") or 0) > 0,
                "ledgerDedupeKey": task_e.get("ledgerDedupeKey"),
                "ledger": task_e.get("ledger"),
                "ledgerSnapshot": task_e.get("ledgerSnapshot"),
                "businessReceiptPassed": task_e_receipt.get("businessReceiptPassed") is True,
                "transactionalVerificationPassed": task_e_receipt.get("transactionalVerificationPassed") is True,
                "ledgerVerificationPassed": task_e_receipt.get("ledgerVerificationPassed") is True,
                "independentReadbackAvailable": task_e_receipt.get("independentReadbackAvailable") is True,
            },
            "taskF": {
                **{key: task_f.get(key) for key in (
                    "executed", "reason",
                    "background", "mainReportIndependent", "backgroundJob", "backgroundJobSha256",
                    "backgroundState", "backgroundReport", "backgroundDirectory", "backgroundPid",
                    "statusIsLaunchSnapshot", "incrementalWorkbook", "incrementalReceipt",
                    "newActivationImportedCount", "notifyIncrementalWorkbook",
                    "reportDateCachePath", "reportDateCacheSha256",
                    "backgroundLaunchReceipt", "backgroundLaunchReceiptSha256",
                    "documentMissingDateRows", "documentMissingDateIccidCount", "documentMissingCutoffRows",
                    "documentCreationFallbackRows", "documentLookupNeededRows", "dateEligibilityMode",
                )},
                "status": task_f.get("status"),
                **{key: task_f.get(key) for key in (
                    "reportBackfillMode", "documentPath", "documentSha256", "documentRunId",
                    "documentKoodaRows", "documentMissingActivationRows", "documentActivationPresentRows",
                    "documentMissingActivationIccidCount", "readyForUploadCount",
                    "refreshExistingMissingCount", "documentExcludedCount", "cloudUploadAttempted",
                    "refreshedMissingActivationCount",
                    "refreshedBlankRecordCount",
                    "koRefreshEnabled", "documentKoRefreshRows", "totalLookupNeededRows",
                )},
                "currentUserAuthorized": self.authorization["taskF"],
                "automaticCatalogSync": task_f.get("automaticCatalogSync") is True,
                "postDocumentSupplemental": task_f.get("postDocumentSupplemental") is True,
                "documentGeneratedBeforeLookup": task_f.get("documentGeneratedBeforeLookup") is True,
                "resultsApplyNextExport": task_f.get("resultsApplyNextExport") is True,
                "documentMissingActivationRedRows": int(task_f_red_review.get("expected_rows") or 0),
                "documentFullRedRows": int(task_f_red_review.get("actual_full_red_rows") or 0),
                "documentRedReviewPassed": task_f_red_review.get("matches") is True,
                "missingActivationFillRgb": task_f_red_review.get("fill_rgb") or "FFC7CE",
                "catalogSource": task_f.get("catalogSource"),
                "catalogSourceSha256": task_f.get("catalogSourceSha256"),
                "catalogSourceRowCount": int(task_f.get("catalogSourceRowCount") or 0),
                "catalogFoundCount": int(task_f.get("catalogFoundCount") or 0),
                "catalogActivationMissingCount": int(task_f.get("catalogActivationMissingCount") or 0),
                "onlineLookupTriggered": task_f.get("onlineLookupTriggered") is True,
                "loginRequired": task_f.get("loginRequired") is True,
                "loginUrl": task_f.get("loginUrl"),
                "inventoryCount": int(task_f.get("inventoryCount") or 0),
                "inventoryTotalRows": int(task_f.get("inventoryTotalRows") or 0),
                "missingCardStoreCount": int(task_f.get("missingCardStoreCount") or 0),
                "invalidCardStoreCount": int(task_f.get("invalidCardStoreCount") or 0),
                "alreadyQueriedCount": int(task_f.get("alreadyQueriedCount") or 0),
                "pendingCount": int(task_f.get("pendingCount") or 0),
                "progress": task_f.get("progress"),
                "progressIntervalSeconds": int(task_f.get("progressIntervalSeconds") or 0),
                "databaseMissingOnly": task_f.get("databaseMissingOnly") is True,
                "foundImportedCount": int(task_f.get("foundImportedCount") or 0),
                "schemaUpgradePendingCount": int(task_f.get("schemaUpgradePendingCount") or 0),
                "schemaUpgradedCount": int(task_f.get("schemaUpgradedCount") or 0),
                "databaseCurrentSchemaCount": int(task_f.get("databaseCurrentSchemaCount") or 0),
                "recordSchemaVersion": int(task_f.get("recordSchemaVersion") or 0),
                "recordedWebFields": task_f.get("recordedWebFields") or [],
                "fullSourceDataIncluded": task_f.get("fullSourceDataIncluded") is True,
                "transientErrorCount": int(task_f.get("transientErrorCount") or 0),
                "requestedWorkers": int(task_f.get("requestedWorkers") or 0),
                "effectiveWorkers": int(task_f.get("effectiveWorkers") or 0),
                "perWorkerDelayMs": int(task_f.get("perWorkerDelayMs") or 0),
                "database": None if self.task_a_only else task_f.get("database") or str(TASK_F_DB),
                "databaseSnapshot": task_f.get("databaseSnapshot"),
                "iccidLibrary": task_f.get("iccidLibrary"),
                "latestIccidLibrary": task_f.get("latestIccidLibrary"),
                "iccidLibraryRecordCount": int(task_f.get("iccidLibraryRecordCount") or 0),
                "iccidLibraryFoundCount": int(task_f.get("iccidLibraryFoundCount") or 0),
                "iccidLibraryActivationMissingCount": int(task_f.get("iccidLibraryActivationMissingCount") or 0),
                "iccidLibraryNotFoundCount": int(task_f.get("iccidLibraryNotFoundCount") or 0),
                "deadlineWorkbook": task_f.get("deadlineWorkbook"),
                "deadlineExportReceipt": task_f.get("deadlineExportReceipt"),
                "deadlineRecordCount": int(task_f.get("deadlineRecordCount") or 0),
                "deadlineActivationCount": int(task_f.get("deadlineActivationCount") or 0),
                "deadlineMissingDateCount": int(task_f.get("deadlineMissingDateCount") or 0),
                "cutoffRule": task_f.get("cutoffRule"),
                "browser": task_f.get("browser"),
                "transportPriority": task_f.get("transportPriority") or [],
                "sameOriginHttpRequests": int(task_f.get("sameOriginHttpRequests") or 0),
                "backgroundJavascriptFallbackRequests": int(task_f.get("backgroundJavascriptFallbackRequests") or 0),
            },
            "performance": {
                "commandTimings": self.state.get("commandTimings") or [],
                "balanceStatisticsRead": (balance.get("performance") or {}).get("statisticsRead"),
                "stages": stage_timings,
                "pipelineWallSecondsBeforeFinalManifest": round(time.monotonic() - self.started_monotonic, 3),
                "exportPerformance": acquire.get("exportPerformance") or {},
                "documentProcessSeconds": document_stage.get("documentProcessSeconds"),
                "documentJoinSeconds": document_stage.get("documentJoinSeconds"),
                "slowestStages": slowest_stages,
                "acquireSubtasks": acquire.get("subtaskDurations") or {},
                "acquireTransport": acquire.get("acquireTransport"),
                "acquisitionScheduling": acquire.get("acquisitionScheduling") or {},
                "finalizeLocalJobScheduling": self.state.get("finalizeLocalJobScheduling") or {},
                "playwrightBrowserConnectionUsed": acquire.get("playwrightBrowserConnectionUsed") is True,
                "ioWorkers": self.args.io_workers,
                "analysisWorkers": self.args.analysis_workers,
                "taskFWorkers": 0 if self.task_a_only else self.args.task_f_workers,
                "processingUsesRunPrivateLocalCopies": acquire.get("processingUsesRunPrivateLocalCopies") is True,
                "sharedScanCacheUsed": candidates.get("sharedScanCacheUsed") is True,
                "scanCacheHashBound": acquire.get("scanCacheHashBound") is True,
                "candidateWorkbookPasses": int(candidates.get("candidateWorkbookPasses") or 0),
                "candidateProcessWorkbookReopens": int(candidates.get("candidateProcessWorkbookReopens") or 0),
                "scanCacheDurationSeconds": (acquire.get("subtaskDurations") or {}).get("scanCache"),
                "refreshEngine": refresh.get("refreshEngine"),
                "workbookRefreshDurationSeconds": refresh.get("workbookRefreshDurationSeconds"),
            },
            "sourceWorkbook": acquire["sourceArchive"],
            "googleChatrDateFallback": document_stage.get("googleChatrDateFallback") or {},
            "googleSystemMissingSheet": document_stage.get("googleSystemMissingSheet") or {},
            "koUnactivatedSheet": document_stage.get("koUnactivatedSheet") or {},
            **{key: acquire.get(key) for key in (
                "chatrGoogleSource", "chatrGoogleSourceSha256", "chatrGoogleDownloadReceipt",
                "chatrGoogleDownloadReceiptSha256", "chatrGoogleDates", "chatrGoogleDatesSha256",
            )},
            "systemKoodaSource": {
                "source": "system", "path": acquire["systemKoodaInventory"],
                "sourceKind": read_json(acquire["systemKoodaInventory"]).get('sourceKind'),
                "sourceScope": read_json(acquire["systemKoodaInventory"]).get('sourceScope'),
                "sha256": acquire["systemKoodaSha256"], "rows": acquire.get("systemKoodaRows", 0),
                "paginationAttemptCount": acquire.get("systemKoodaPaginationAttemptCount"),
                "paginationRecovered": acquire.get("systemKoodaPaginationRecovered"),
                "paginationEvidence": acquire.get("systemKoodaPaginationEvidence"),
                "exportCoverageAttemptCount": acquire.get("systemKoodaCoverageAttemptCount"),
                "exportCoverage": acquire.get("systemKoodaCoverage"),
            },
            "reportDateCachePath": (self.state.get("reportDateCache") or {}).get("path"),
            "reportDateCacheSha256": (self.state.get("reportDateCache") or {}).get("sha256"),
            "finalWorkbook": refresh["finalWorkbook"],
            "report": final["report"],
            "systemMissingFDates": self.state.get("systemMissingFDates"),
            "fDateImport": self.state.get("fDateImport"),
            "expiredCardProbe": self.state.get("stages", {}).get("expiredCardProbe"),
            "taskG": self.state.get("stages", {}).get("taskG"),
            "portSubset": self.state.get("portSubset"),
            "vGroupSwapDaily": self.state.get("vGroupSwapDaily"),
            "state": str(self.state_path),
        }
        if self.task_a_only:
            for task in "BCDEF":
                result["task" + task].update(self.task_a_excluded_stage())
        from opensms_chat_summary import render_summary
        chat_path = self.delivery / f"OpenSMS_聊天摘要_{self.run_id}.md"
        chat_path.write_text(render_summary(result), encoding="utf-8")
        result["chatSummaryPath"] = str(chat_path)
        summary_path = self.delivery / f"OpenSMS_10日报表巡检_摘要_{self.run_id}.json"
        atomic_json(summary_path, result)
        result["summaryPath"] = str(summary_path)
        return result

    def summarize_write_groups(self, prefix: str) -> dict[str, Any]:
        groups = [row for key, row in self.state.get("writeGroups", {}).items() if key.startswith(prefix)]
        return {
            "candidateCount": sum(int(row.get("candidateCount") or 0) for row in groups),
            "backendValidCount": sum(int(row.get("backendValid") or 0) for row in groups),
            "actualWriteCount": sum(int(row.get("actualWrite") or 0) for row in groups),
            "independentVerificationPassed": sum(row.get("result") == "passed" and row.get("independentReadbackPerformed", True) for row in groups),
            "noWriteGroupsStageVerified": sum(bool(row.get("noWrite") and row.get("stageReadbackVerified")) for row in groups),
            "stageReadbackPendingGroups": sum(bool(row.get("stageReadbackRequired") and not row.get("stageReadbackVerified")) for row in groups),
        }

    def write_failure_report(self, error: Exception) -> dict[str, Any]:
        clock_origin = getattr(self, "started_monotonic", None)
        pipeline_wall_seconds = (round(time.monotonic() - clock_origin, 3) if clock_origin is not None else
            ((self.state.get("summary") or {}).get("performance") or {}).get("pipelineWallSecondsBeforeFinalManifest"))
        completed_acquire = self.state.get("stages", {}).get("acquire") or {}
        partial = self.state.get("partialAcquisition") or {}
        if partial.get("runId") != self.run_id:
            partial = {}
        acquire = {**partial, **completed_acquire}
        balance_path = Path(acquire["balance"]) if acquire.get("balance") else None
        balance_groups = build_balance_summary_groups(read_json(balance_path) if balance_path and balance_path.is_file() else {})
        source = acquire.get("sourceArchive") if completed_acquire or partial.get("sourceVerified") else None
        if source:
            try:
                ensure_xlsx(Path(source), "失败交付已核验源表")
                if not acquire.get("sourceSha256") or sha256(Path(source)) != acquire["sourceSha256"]:
                    source = None
            except (OSError, PatrolError):
                source = None
        server_date = acquire.get("serverDate") if completed_acquire or partial.get("serverDateVerified") else None
        # A failed run can expose the latest independently verified stage table,
        # but has no all-stages-final workbook even if the refresh itself passed.
        final_workbook = None
        latest_verified = {}
        latest_verified_stage = None
        for stage in ("refresh", "refreshAfterTaskD", "refreshAfterTaskC", "refreshAfterTaskB", "refreshAfterStandard"):
            candidate = self.state.get("stages", {}).get(stage) or {}
            if candidate.get("finalWorkbook") and candidate.get("verifyReceipt") and candidate.get("readbackReceipt"):
                latest_verified, latest_verified_stage = candidate, stage
                break
        task_summaries = {
            name: {"status": "passed" if (self.state.get("stages", {}).get(name) or {}).get("executed") else "not_executed",
                   **anomaly_summary(self.state.get("stages", {}).get(name) or {}),
                   **self.summarize_write_groups(prefix)}
            for name, prefix in (("taskB", "task_b_"), ("taskC", "task_c_"))
        }
        for task in task_summaries.values():
            if task["stageReadbackPendingGroups"]:
                task["status"] = "pending_stage_readback"
        task_d_progresses = []
        for branch in ("swap_zero", "retention_verified"):
            progress_path = self.receipts / f"opensms_task_d_{branch}_{self.run_id}_progress.json"
            if not progress_path.is_file():
                continue
            progress = read_json(progress_path)
            uncertain = progress.get("uncertainBatch") or {}
            pending = progress.get("pendingBatch") or {}
            if not uncertain and pending.get("cardNos") and progress.get("status") in ("failed", "submitting", "submitted_response_received"):
                # Legacy receipts also preserve the exact pending identities. Without
                # a validated transaction receipt they are unknown, never failed/zero.
                uncertain = {"batchNumber": pending.get("batchNumber"), "expireDate": pending.get("expireDate"),
                             "inputCount": len(pending["cardNos"])}
            task_d_progresses.append({"branch": branch, "progress": str(progress_path),
                                      "confirmedInputCount": int(progress.get("confirmedInputCount") or sum(
                                          int(item.get("inputCount") or 0) for item in progress.get("batches") or [])),
                                      "uncertainBatch": uncertain,
                                      "uncertainCards": pending.get("cardNos") or [] if uncertain else []})
        task_d_confirmed = sum(item["confirmedInputCount"] for item in task_d_progresses)
        task_d_uncertain = sum(int(item["uncertainBatch"].get("inputCount") or 0) for item in task_d_progresses)
        task_d = self.state.get("stages", {}).get("taskD") or {}
        task_d_checkpoint = self.state.get("taskDCheckpoint") or {}
        task_d_partition = {**(task_d_checkpoint.get("retentionPartition") or {}),
                            **{key: task_d[key] for key in TASK_D_RETENTION_PARTITION_FIELDS if key in task_d}}
        population_removed = (population_statistics_removed(task_d) or population_statistics_removed(task_d_checkpoint)
                              or population_statistics_removed(self.state, report=True))
        population_report_fields = ({"taskD" + key[0].upper() + key[1:]: value
            for key, value in removed_population_fields().items()} if population_removed else {})
        task_d_summary = task_d.get("retentionSummary") if task_d.get("status") == "passed" and not population_removed else None
        from opensms_task_d_notification_summary import summary_lines as task_d_notification_lines
        from opensms_task_d_report_details import report_lines as task_d_phone_detail_lines
        task_d_status = task_d.get("status") or (
            ("business_completed_notification_summary_failed" if population_removed else "business_completed_statistics_failed") if task_d_checkpoint.get("businessCompleted")
            else "not_completed" if self.authorization.get("taskD") else "skipped_no_current_user_authorization"
        )
        task_e = self.state.get("stages", {}).get("taskE") or {}
        task_f = self.state.get("stages", {}).get("taskF") or {}
        task_e_progress_path = self.receipts / f"opensms_task_e_{self.run_id}_progress.json"
        task_e_progress = read_json(task_e_progress_path) if task_e_progress_path.is_file() else {}
        task_e_confirmed = int(task_e_progress.get("confirmedInputCount") or sum(
            int(item.get("inputCount") or 0) for item in task_e_progress.get("batches") or []
        ))
        task_e_uncertain = task_e_progress.get("uncertainBatch") or {}
        if not task_e_uncertain and task_e_progress.get("status") == "failed":
            pending = task_e_progress.get("pendingBatch") or {}
            if pending.get("cardNos") and "504" in str(task_e_progress.get("error") or ""):
                task_e_uncertain = {
                    "batchNumber": pending.get("batchNumber"),
                    "expireDate": pending.get("expireDate"),
                    "inputCount": len(pending.get("cardNos") or []),
                }
        report = self.delivery / f"OpenSMS_10日报表巡检_失败_{self.run_id}.md"
        lines = [
            "# OpenSMS 10日报表巡检失败报告",
            "",
            "## 本轮概况",
            "",
            f"- 状态：失败并停止；未把未确认步骤记为完成。",
            f"- 运行编号：{self.run_id}",
            f"- 服务器日期：{server_date or '未知，三方日期核验未通过'}。",
            f"- 策略文件：{POLICY}",
            "",
            "## 0回复与回复率",
            "",
            "- 本轮未完整完成，禁止把部分结果作为最终统计。",
            "",
            "## 运营商调整",
            "",
            f"- 已记录分组：{len(self.state.get('writeGroups') or {})}；仅带写入回执和独立回读的分组可视为已确认。",
            *[f"- 任务{name[-1]}：{task['status']}；候选{task['candidateCount']}，后台有效{task['backendValidCount']}，实际写入{task['actualWriteCount']}，独立复核通过{task['independentVerificationPassed']}组。"
              for name, task in task_summaries.items()],
            *([] if population_removed else [(f"- 任务D已完成：本轮新转入保留卡区 {task_d_summary['newTransferredCount']} 个；执行后保留区共 {task_d_summary['totalInRetentionCount']} 个（统计时间 {task_d_summary['asOf']}）。"
             if task_d_summary and task_d_summary.get("status") == "passed" else
             f"- 任务D保留区统计：{task_d_status}；本轮新增与执行后总量未知，不能以候选或申请记录数代替。")]),
            *([f"- 任务D {row['carrierName']}：新增 {row['newTransferredCount']} 个；区内总数 {row['totalInRetentionCount']} 个。"
               for row in task_d_summary.get("byCarrier", [])] if task_d_summary else []),
            *([f"- [任务D保留区统计回执](<{task_d['retentionSummaryReceipt']}>)"]
              if task_d.get("retentionSummaryReceipt") else []),
            *task_d_notification_lines(task_d.get("notificationSummary"), self.run_id),
            *task_d_phone_detail_lines({
                **population_report_fields,
                "runId": self.run_id, "taskAOnly": self.task_a_only,
                "taskDStatus": task_d_status, "taskDExecuted": task_d.get("executed") is True,
                "taskDRetentionCarrierTransferEnabled": False,
                "taskDCurrentUserAuthorized": self.authorization.get("taskD") is True,
                "taskDManifest": task_d.get("manifest") or task_d_checkpoint.get("manifest"),
                "taskDSwapExpiryReceipt": task_d.get("swapExpiryReceipt") or task_d_checkpoint.get("swapExpiryReceipt"),
                "taskDRetentionExpiryReceipt": task_d.get("retentionExpiryReceipt") or task_d_checkpoint.get("retentionExpiryReceipt"),
                "taskDRetentionSummaryReceipt": task_d.get("retentionSummaryReceipt"),
                "taskDRetentionGroupsReceipt": task_d.get("retentionGroupsReceipt"),
            }),
            *([f"- 任务D统计失败：{task_d_checkpoint['retentionStatisticsError']}；业务回执已保留，不自动重做写入。"]
              if task_d_checkpoint.get("retentionStatisticsError") else []),
            *([f"- 任务D通知回执汇总失败：{task_d_checkpoint['notificationSummaryError']}；业务回执已保留，不自动重做写入。"]
              if task_d_checkpoint.get("notificationSummaryError") else []),
            *([f"- 任务D卡保留筛选：实际命中 {task_d_partition.get('retentionMatchedCardCount', '未知')}；非保护可处理 {task_d_partition.get('retentionActionableCardCount', '未知')}；未配置运营商跳过 {task_d_partition.get('unconfiguredCarrierSkippedCount', 0)}；已删除运营商跳过 {task_d_partition.get('retiredCarrierSkippedCount', 0)}；筛选未命中跳过 {task_d_partition['filteredUnmatchedCount']}（均不计成功）。",
               f"- 任务D筛选未命中号码：{'、'.join(task_d_partition.get('filteredUnmatchedCards') or []) or '无'}。"]
              if "filteredUnmatchedCount" in task_d_partition else []),
            *([f"- [任务D卡保留全部分页与筛选证明](<{task_d_partition['retentionReceipt']}>)"]
              if task_d_partition.get("retentionReceipt") else []),
            "",
            "## 下架恢复",
            "",
            (f"- 过期区窄范围存活测试：{(self.state.get('stages', {}).get('expiredCardProbe') or {}).get('status', '未确认')}；异步及提交未确认不自动重试，见独立回执。"
             if self.state.get('stages', {}).get('expiredCardProbe') else
             "- 旧泛化功能已取消；本轮尚未执行新授权过期区存活测试。"),
            "",
            "## 余量与提醒",
            "",
            "- 若余量阶段已有回执，以该回执为准；否则本轮未知。",
            *[f"- {item['group']}余量：{item['percentRemaining'] if item['percentRemaining'] is not None else '未知'}{'%' if item['percentRemaining'] is not None else ''}。" for item in balance_groups],
            f"- 任务D已确认通知数：{task_d_confirmed}；提交结果不确定：{task_d_uncertain}个；自动重复提交：{'已阻止' if task_d_uncertain else '不适用'}。",
            "- 任务D不确定批次没有可验证的事务结果，不计入成功或失败；后台没有到期日独立查询接口，需后台核实后另行处理。",
            f"- 任务E状态：{task_e.get('status') or ('未授权跳过' if not self.authorization.get('taskE') else '未完成')}；已确认提交数：{task_e_confirmed}。",
            f"- 任务E提交结果不确定：{int(task_e_uncertain.get('inputCount') or 0)}个；自动重试：{'已阻止' if task_e_uncertain else '不适用'}。",
            f"- 任务F状态：{'仅任务A模式，完整跳过（task_a_only）' if self.task_a_only else task_f.get('status') or '自动目录同步未完成'}；需要登录：{'是' if task_f.get('loginRequired') else '否'}。",
            f"- 任务F最新ICCID库：{task_f.get('latestIccidLibrary') or '未生成'}。",
            "",
            "## 文件与回执",
            "",
        ]
        if source:
            lines.extend([f"- 本轮已核验新鲜源文件（不是最终状态表）：[{Path(source).name}](<{source}>)",
                          f"- 源文件SHA256：{acquire['sourceSha256']}。"])
        pagination_evidence = acquire.get("systemKoodaPaginationEvidence")
        if pagination_evidence and Path(pagination_evidence).is_file():
            lines.append(f"- [系统Kooda分页诊断与重读证据](<{pagination_evidence}>)。")
        for index, coverage_path in enumerate(acquire.get("systemKoodaCoverageReceipts") or [], 1):
            lines.append(f"- [系统库存与新鲜导出身份覆盖核验第{index}次](<{coverage_path}>)。")
        if final_workbook:
            lines.extend([f"- 最终状态工作簿：{final_workbook}"])
        elif latest_verified:
            lines.extend([f"- 最近已核验阶段表（{latest_verified_stage}，不是全任务最终表）：[{Path(latest_verified['finalWorkbook']).name}](<{latest_verified['finalWorkbook']}>)",
                          f"- 该阶段表SHA256：{latest_verified.get('finalWorkbookSha256') or '未记录'}；保留当时已独立回读的运营商状态，不代表后续不确定下卡通知已完成。"])
        lines.extend([
            f"- 断点状态：{self.state_path}",
            *[f"- 任务D {item['branch']}进度断点：{item['progress']}" for item in task_d_progresses],
            *( [f"- 任务E进度断点：{task_e_progress_path}"] if task_e_progress_path.is_file() else [] ),
            "",
            "## 异常",
            "",
            f"- {type(error).__name__}: {error}",
        ])
        if task_d_uncertain:
            lines.extend(["", "## 任务D提交结果不确定的逐号清单", "",
                          "下列号码均不计入成功或失败；本报告刷新不会重新提交。", "",
                          "| 分支 | 卡号 | 本次通知日期 | 结果 |", "| --- | --- | --- | --- |"])
            for item in task_d_progresses:
                for card in item["uncertainCards"]:
                    lines.append(f"| {item['branch']} | {card} | {item['uncertainBatch'].get('expireDate')} | 未确认，禁止自动重试 |")
        lines.extend(["", "## 已记录运营商分组与复核索引", "",
                      "| 分组 | 候选 | 后台有效 | 实际写入 | 结果 | 写入回执 | 独立复核 |",
                      "| --- | ---: | ---: | ---: | --- | --- | --- |"])
        for key, row in self.state.get("writeGroups", {}).items():
            receipt_link = f"[回执](<{row['write']}>)" if row.get("write") else "—"
            verify_link = f"[复核](<{row['verify']}>)" if row.get("verify") else "—"
            group_status = row.get("result", "未知")
            if row.get("stageReadbackRequired") and not row.get("stageReadbackVerified"):
                group_status = "无写入；阶段回读未完成"
            lines.append(f"| {key} | {row.get('candidateCount', '未知')} | {row.get('backendValid', '未知')} | {row.get('actualWrite', '未知')} | {group_status} | {receipt_link} | {verify_link} |")
        lines.extend(["", "## 本轮性能与优化", "", "| 阶段 | 秒 |", "| --- | ---: |"])
        for timing in self.state.get("timings") or []:
            lines.append(f"| {timing.get('stage')} | {timing.get('durationSeconds')} |")
        for timing in self.state.get("commandTimings") or []:
            lines.append(f"| 子程序 {timing['command']}（退出码{timing['exitCode']}） | {timing['durationSeconds']} |")
        document = self.state.get("stages", {}).get("document") or self.state.get("documentProcessTiming") or {}
        lines.extend(["", f"文档完整处理：{document.get('documentProcessSeconds', '未记录')}秒；上表document为主流程等待时间，不能与并行阶段直接累加。"])
        lines.append(f"程序启动至失败报告：{pipeline_wall_seconds if pipeline_wall_seconds is not None else '未知'}秒；不含启动前的调度/AI等待，且不累加重叠阶段。")
        for key, label in (("backendGenerationMs", "OpenSMS服务端生成"), ("downloadMs", "下载")):
            value = (acquire.get("exportPerformance") or {}).get(key)
            if value is not None:
                lines.append(f"- {label}：{value / 1000:.3f}秒。")
        performance_note = SCRIPTS / "OPENSMS_PERFORMANCE_V51.md"
        if performance_note.is_file():
            note_copy = self.receipts / performance_note.name
            shutil.copy2(performance_note, note_copy)
            lines.extend([f"- [程序优化、并发边界、测试与服务端建议](<{note_copy}>)。"])
        lines.extend(["", f"- [本轮自动恢复记录](<{self.automatic_recovery_report()}>)。"])
        reliability_note = SCRIPTS / "OPENSMS_RELIABILITY_V53.md"
        if reliability_note.is_file():
            note_copy = self.copy_artifact(reliability_note)
            lines.extend([f"- [v53后台自动恢复规则](<{note_copy}>)。"])
        from opensms_report_summary import compact_phone_tables
        from opensms_v_swap_daily import export_daily, report_lines as v_swap_lines
        try:
            self.state["vGroupSwapDaily"] = export_daily(
                STATE_ROOT / "v_group_swap_daily", self.run_id, self.delivery, self.receipts,
                self.state.get("writeGroups", {}).values())
            lines.extend(v_swap_lines(self.state["vGroupSwapDaily"]))
        except Exception as stats_error:
            self.state.pop("vGroupSwapDaily", None)
            lines.extend(["", f"- V组每日统计未完成：{stats_error}；不以0冒充结果，已验证写入仍保留原始回执。"])
        from opensms_task_g import report_lines as task_g_lines
        lines.extend(task_g_lines(self.state.get("stages", {}).get("taskG")))
        report.write_text("\n".join(compact_phone_tables(lines)) + "\n", encoding="utf-8")
        result = {
            "status": "failed",
            "vGroupSwapDaily": self.state.get("vGroupSwapDaily"),
            **execution_contract(self.task_a_only),
            "runId": self.run_id,
            "serverDate": server_date,
            "error": str(error),
            "documentFailure": self.state.get("documentFailure"),
            "exportFailure": self.state.get("exportFailure"),
            "taskDPreparationFailure": self.state.get("taskDPreparationFailure"),
            "expiredCardProbe": self.state.get('stages', {}).get('expiredCardProbe'),
            "taskG": self.state.get('stages', {}).get('taskG'),
            "fDateImport": self.state.get('fDateImport'),
            "balanceGroups": balance_groups,
            **task_summaries,
            "taskD": {"status": task_d_status, "retentionSummary": task_d_summary,
                      **(removed_population_fields() if population_removed else {}),
                      "retentionCarrierTransferEnabled": False, "retentionCarrierTransferStatus": "removed_by_user",
                      "notificationSummary": task_d.get("notificationSummary"), **task_d_partition,
                      **{key: task_d[key] for key in ("executed", "reason", "swapMetricCandidateCount", "swapNotificationCount",
                          "retentionUniqueCardCount", "carrierCandidateCount", "carrierActualWrite") if key in task_d}},
            "taskDConfirmedSubmittedCount": task_d_confirmed,
            "taskDUncertainSubmittedCount": task_d_uncertain,
            "taskDAutomaticRetryBlocked": bool(task_d_uncertain),
            "taskEConfirmedSubmittedCount": task_e_confirmed,
            "taskEUncertainSubmittedCount": int(task_e_uncertain.get("inputCount") or 0),
            "taskEAutomaticRetryBlocked": bool(task_e_uncertain),
            "sourceWorkbook": source,
            "sourceWorkbookSha256": acquire.get("sourceSha256") if source else None,
            "finalWorkbook": final_workbook,
            "latestVerifiedWorkbook": latest_verified.get("finalWorkbook"),
            "latestVerifiedStage": latest_verified_stage,
            "partialWorkbookIsFinal": False,
            "partialAcquisition": partial,
            "apiTargetCleanup": self.state.get("apiTarget") or {},
            "performance": {"stages": self.state.get("timings") or [], "commandTimings": self.state.get("commandTimings") or [], "exportPerformance": acquire.get("exportPerformance") or {},
                            "pipelineWallSecondsBeforeFinalManifest": pipeline_wall_seconds,
                            "documentProcessSeconds": document.get("documentProcessSeconds"),
                            "documentJoinSeconds": document.get("documentJoinSeconds")},
            "report": str(report),
            "state": str(self.state_path),
        }
        if self.task_a_only:
            for task in "BCDEF":
                result.setdefault("task" + task, {}).update(self.task_a_excluded_stage())
        from opensms_chat_summary import render_summary
        chat_path = self.delivery / f"OpenSMS_聊天摘要_失败_{self.run_id}.md"
        chat_path.write_text(render_summary(result), encoding="utf-8")
        result["chatSummaryPath"] = str(chat_path)
        return result

    def execute(self) -> dict[str, Any]:
        try:
            self.verify_policy()
            if getattr(self.args, "continue_task_d", False):
                return self.continue_task_d()
            if getattr(self.args, "continue_task_g", False):
                return self.continue_task_g()
            task_f_debug_iccid = str(getattr(self.args, "task_f_debug_iccid", "") or "").strip()
            if task_f_debug_iccid:
                result = self.run_task_f_debug(task_f_debug_iccid)
                self.state["status"] = "success"
                self.state["summary"] = result
                self._save_state()
                self.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
                return result
            if int(getattr(self.args, "task_f_sample_size", 0) or 0):
                self.repair_task_f_lookup()
                task_f = self.run_task_f()
                result = self.finish_task_f_sample(task_f)
                self.state["status"] = "success"
                self.state["summary"] = result
                self._save_state()
                self.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
                return result
            if bool(getattr(self.args, "task_f_only", False)):
                self.repair_task_f_lookup()
                task_f = self.run_task_f()
                result = self.finish_task_f_only(task_f)
                self.state["status"] = "success"
                self.state["summary"] = result
                self._save_state()
                self.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
                return result
            acquire = self.acquire()
            self.start_document(acquire)
            candidates = self.analyze_and_candidates(acquire)
            document = self.finish_document()
            if self.task_a_only:
                self.skip_task_f()
            else:
                self.start_background_task_f(document, acquire)
            if getattr(self.args, "restore_unexpired_to_a11", False):
                self.run_unexpired_restore(document, acquire)
            else:
                self.run_standard_writes(candidates)

            # A later business stage may only read a workbook whose carrier
            # cells have been independently reread after the prior stage.
            if self.authorization["taskB"] or self.authorization["taskC"] or self.authorization["taskD"] or self.authorization["taskE"]:
                standard_refresh = self.refresh_workbook(
                    document,
                    stage_name="refreshAfterStandard",
                    output_label="普通规则后状态",
                )
                document = self.refreshed_document(document, standard_refresh)

            if self.authorization["taskB"]:
                self.run_task_b(document, acquire)
                if self.authorization["taskC"] or self.authorization["taskD"] or self.authorization["taskE"]:
                    task_b_refresh = self.refresh_workbook(
                        document,
                        stage_name="refreshAfterTaskB",
                        output_label="任务B后状态",
                    )
                    document = self.refreshed_document(document, task_b_refresh)

            if self.authorization["taskC"]:
                self.run_task_c(document, acquire)
                if self.authorization["taskD"] or self.authorization["taskE"]:
                    task_c_refresh = self.refresh_workbook(
                        document,
                        stage_name="refreshAfterTaskC",
                        output_label="任务C后状态",
                    )
                    document = self.refreshed_document(document, task_c_refresh)

            if self.authorization["taskD"]:
                self.run_task_d(document, acquire)
                if self.authorization["taskE"]:
                    task_d_refresh = self.refresh_workbook(
                        document,
                        stage_name="refreshAfterTaskD",
                        output_label="任务D后状态",
                    )
                    document = self.refreshed_document(document, task_d_refresh)
            else:
                self.skip_task_d()
            if self.authorization["taskE"]:
                self.run_task_e(document, acquire)
                document = self.add_task_e_unwritten_sheet(document)
            else:
                self.skip_task_e()
            self.import_current_f_dates(acquire)
            self.run_expired_card_probe(acquire)
            from opensms_task_g import SETTINGS
            g_enabled = (not self.task_a_only and not getattr(self.args, "restore_unexpired_to_a11", False)
                         and read_json(SETTINGS).get("enabled") is not False)
            before_g = None
            if g_enabled:
                before_g = self.refresh_workbook(document, stage_name="refreshBeforeTaskG", output_label="任务G前状态")
                document = self.refreshed_document(document, before_g)
            self.run_task_g(acquire, before_g)
            refresh = self.refresh_workbook(document)
            self.close_api_target()
            final = self.finalize(acquire, candidates, document, refresh)
            result = self.summary(acquire, candidates, refresh, final)
            self.state["status"] = "success"
            self.state["summary"] = result
            self._save_state()
            self.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
            self.release_expired_card_probe()
            return result
        except Exception as exc:
            if getattr(self.args, "continue_task_d", False) and self.state.get("taskDContinuationAttempts"):
                self.state["taskDContinuationAttempts"][-1].update(
                    status="failed", error=str(exc), completedAt=now_hkt().isoformat())
            if getattr(self.args, "continue_task_g", False) and self.state.get("taskGContinuationAttempts"):
                self.state["taskGContinuationAttempts"][-1].update(
                    status="failed", error=str(exc), completedAt=now_hkt().isoformat())
            if self.document_process and self.document_process.poll() is None:
                self.document_process.terminate()
                try:
                    self.document_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.document_process.kill()
            if self.document_log_handle and not self.document_log_handle.closed:
                self.document_log_handle.close()
            try:
                self.close_api_target()
            except Exception as cleanup_error:
                # Preserve the original stage failure; the cleanup error remains
                # explicit in state and in the failure delivery before manifest.
                self.state["apiTargetCleanupError"] = str(cleanup_error)
            self.state["status"] = "failed"
            self.state["error"] = str(exc)
            self.failure_result = self.write_failure_report(exc)
            self.state["summary"] = self.failure_result
            self._save_state()
            try:
                self.command("delivery_manifest_failed", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
            except Exception:
                pass
            raise

    def continue_task_d(self) -> dict[str, Any]:
        """Current-user continuation of D's exact HTTP502 tail, then E/G/final."""
        history = self.state["taskDContinuationAttempts"][-1]
        for proof in history["preflight"].get("files", []):
            file = Path(proof["path"])
            if file.resolve() != self.state_path.resolve() and sha256(file) != proof["sha256"]:
                raise PatrolError("TASK_D_CONTINUATION_EVIDENCE_CHANGED")
        self.create_api_target()
        acquire = self.state["stages"]["acquire"]
        dates = self.command("task_d_continuation_server_dates",
            [NODE, SCRIPTS / "opensms_read_server_dates_fast_raw.mjs"],
            env={"BITBROWSER_CDP": self.args.cdp, "OPENSMS_FORCE_RAW_CDP": "1"}).payload
        if dates.get("success") is not True or any(
                (dates.get(key) or {}).get("serverDate") != acquire["serverDate"]
                for key in ("day", "phoneConfig")):
            raise PatrolError("TASK_D_CONTINUATION_PLATFORM_DATE_CHANGED: 未提交，不允许旧日期跨日续接")
        history["dateReceipts"] = [str(self.copy_artifact(dates[key]["path"],
            f"task_d_continuation_date_{key}_{self.run_id}.json")) for key in ("day", "phoneConfig")]
        self._save_state()
        document = self.refreshed_document(self.state["stages"]["document"], self.state["stages"]["refreshAfterTaskC"])
        self.run_task_d(document, acquire)
        refreshed = self.refresh_workbook(document, stage_name="refreshAfterTaskD", output_label="任务D后状态")
        document = self.refreshed_document(document, refreshed)
        self.run_task_e(document, acquire)
        document = self.add_task_e_unwritten_sheet(document)
        # These stages were never reached in the original run. Do not dispatch
        # another F query or repeat export, ordinary writes, B or C.
        self.import_current_f_dates(acquire)
        self.run_expired_card_probe(acquire)
        from opensms_task_g import SETTINGS
        before_g = None
        if read_json(SETTINGS).get("enabled") is not False:
            before_g = self.refresh_workbook(document, stage_name="refreshBeforeTaskG", output_label="任务G前状态")
            document = self.refreshed_document(document, before_g)
        self.run_task_g(acquire, before_g)
        refresh = self.refresh_workbook(document)
        self.close_api_target()
        self.receipts = self.state_path.parent
        final = self.finalize(acquire, self.state["stages"]["candidates"], document, refresh)
        result = self.summary(acquire, self.state["stages"]["candidates"], refresh, final)
        history.update(status="success", completedAt=now_hkt().isoformat())
        result.update(taskDContinuation=True, completedBusinessStagesReplayed=False, exportReplayed=False)
        self.state.update(status="success", summary=result)
        self.state.pop("error", None)
        self._save_state()
        self.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
        self.release_expired_card_probe()
        return result

    def continue_task_g(self) -> dict[str, Any]:
        """Only a proven pre-query G failure; never replay earlier stages."""
        history = self.state["taskGContinuationAttempts"][-1]
        self.create_api_target()
        acquired = self.state["stages"]["acquire"]
        dates = self.command("task_g_continuation_server_dates",
            [NODE, SCRIPTS / "opensms_read_server_dates_fast_raw.mjs"],
            env={"BITBROWSER_CDP": self.args.cdp, "OPENSMS_FORCE_RAW_CDP": "1"}).payload
        if dates.get("success") is not True or any(
                (dates.get(key) or {}).get("serverDate") != acquired["serverDate"]
                for key in ("day", "phoneConfig")):
            raise PatrolError("TASK_G_CONTINUATION_PLATFORM_DATE_CHANGED: 未写入，需要新鲜轮次")
        history["dateReceipts"] = [str(self.copy_artifact(dates[key]["path"],
            f"task_g_continuation_date_{key}_{self.run_id}.json")) for key in ("day", "phoneConfig")]
        document = self.state["stages"]["document"]
        before = self.refresh_workbook(document,
            stage_name="refreshBeforeTaskGContinuation", output_label="任务G续接前状态")
        # The original failed stage and files remain immutable in the attempt
        # history. Only this explicit user-authorized branch may replace state.
        history["previousTaskG"] = self.state["stages"].pop("taskG")
        self._save_state()
        self.run_task_g(acquired, before)
        document = self.refreshed_document(document, before)
        refresh = self.refresh_workbook(document)
        self.close_api_target()
        candidates = self.state["stages"]["candidates"]
        # Derived F/V supplements already exist at the original same-run
        # receipt directory. Their normal no-overwrite verifier must read that
        # directory, not interpret the private G attempt as missing evidence.
        self.receipts = self.state_path.parent
        final = self.finalize(acquired, candidates, document, refresh)
        result = self.summary(acquired, candidates, refresh, final)
        history.update(status="success", completedAt=now_hkt().isoformat())
        result.update(taskGContinuation=True, completedBusinessStagesReplayed=False,
                      exportReplayed=False, fImportReplayed=False)
        self.state.update(status="success", summary=result)
        self.state.pop("error", None)
        self._save_state()
        self.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", self.delivery])
        self.release_expired_card_probe()
        return result


def static_check() -> dict[str, Any]:
    missing = [str(path) for path in REQUIRED_FILES if not path.is_file()]
    if not PYTHON.is_file():
        missing.append(str(PYTHON))
    if not NODE.is_file():
        missing.append(str(NODE))
    if not NODE_MODULES.is_dir():
        missing.append(str(NODE_MODULES))
    if missing:
        raise PatrolError("缺少运行依赖: " + ", ".join(missing))
    policy_text = POLICY.read_text(encoding="utf-8")
    markers = [
        "任务B", "任务C", "任务D", "任务E", "任务F", "后台静默", "独立回读", "新鲜",
        "统一本地程序唯一执行入口", "AI 不得手工重做", "快过期续费C/T保护",
    ]
    absent_markers = [marker for marker in markers if marker not in policy_text]
    if absent_markers:
        raise PatrolError(f"策略文件关键门禁缺失: {absent_markers}")
    python_files = [
        SCRIPTS / "opensms_task_g.py",
        SCRIPTS / "opensms_build_report_kooda_source.py",
        ROOT / "services/opensms_document_api/pipeline/report_kooda_source.py",
        SCRIPTS / "opensms_auth_keychain.py",
        SCRIPTS / "opensms_local_jobs.py",
        SCRIPTS / "opensms_acquire_schedule.py",
        SCRIPTS / "benchmark_opensms_local_jobs.py",
        SCRIPTS / "opensms_patrol_runner.py",
        SCRIPTS / "opensms_site_execution.py",
        SCRIPTS / "opensms_browser_session.py",
        ROOT / "opensms_analyze_current.py",
        ROOT / "opensms_generate_opr_zero_candidates.py",
        ROOT / "opensms_generate_e_reply_recovery.py",
        ROOT / "opensms_export_scan_cache.py",
        SCRIPTS / "opensms_generate_a_zone_split.py",
        SCRIPTS / "opensms_generate_task_b_candidates.py",
        SCRIPTS / "opensms_generate_task_c_candidates.py",
        SCRIPTS / "opensms_finalize_patrol_report.py",
        SCRIPTS / "opensms_parallel_audit.py",
        SCRIPTS / "opensms_expired_card_probe_audit.py",
        SCRIPTS / "opensms_expired_card_probe_background.py",
        SCRIPTS / "grabandgo_auth_keychain.py",
        SCRIPTS / "opensms_refresh_workbook_carriers_fast.py",
        SCRIPTS / "opensms_highlight_google_activated_missing.py",
        SCRIPTS / "opensms_patrol_profile.py",
        SCRIPTS / "opensms_chat_summary.py",
        SCRIPTS / "opensms_make_delivery_manifest.py",
        SCRIPTS / "opensms_generate_task_d_zero_metrics.py",
        SCRIPTS / "opensms_task_d_retention_summary.py",
        SCRIPTS / "opensms_task_d_notification_summary.py",
        SCRIPTS / "opensms_generate_task_e_candidates.py",
        SCRIPTS / "opensms_task_f_db.py",
        SCRIPTS / "opensms_report_date_cache.py",
        SCRIPTS / "opensms_task_f_delta.py",
        SCRIPTS / "opensms_task_f_background.py",
        SCRIPTS / "opensms_task_f_progress_status.py",
        SCRIPTS / "opensms_generate_quick_renew_c_restore.py",
        ROOT / "services/opensms_document_api/pipeline/process_card_slots.py",
        ROOT / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py",
        ROOT / "services/opensms_document_api/pipeline/card_retention_report.py",
        ROOT / "services/opensms_document_api/pipeline/ko_unactivated.py",
    ]
    node_files = [
        SCRIPTS / "opensms_task_g_engine.mjs",
        SCRIPTS / "opensms_execute_task_g.mjs",
        ROOT / "opensms_protocol_client.mjs",
        SCRIPTS / "opensms_new_browser.mjs",
        SCRIPTS / "opensms_browser_auth_store.mjs",
        ROOT / "opensms_api_response.mjs",
        ROOT / "opensms_payload_readiness.mjs",
        ROOT / "opensms_payload_codec.mjs",
        SCRIPTS / "opensms_download_google_sheet.mjs",
        SCRIPTS / "opensms_google_download_transport.mjs",
        SCRIPTS / "opensms_api_target_guard_cli.mjs",
        SCRIPTS / "opensms_export_fast_raw.mjs",
        SCRIPTS / "opensms_read_server_dates_fast_raw.mjs",
        SCRIPTS / "opensms_export_submission_ready.mjs",
        SCRIPTS / "opensms_balance_statistics_readonly.mjs",
        SCRIPTS / "opensms_resolve_active_carriers.mjs",
        SCRIPTS / "opensms_generate_paired_opr_carrier_candidates.mjs",
        SCRIPTS / "opensms_collect_final_carrier_state.mjs",
        SCRIPTS / "opensms_readonly_concurrency.mjs",
        SCRIPTS / "opensms_carriers_readonly.mjs",
        SCRIPTS / "opensms_refresh_workbook_carriers.mjs",
        SCRIPTS / "opensms_prepare_task_d.mjs",
        SCRIPTS / "opensms_task_d_filtered_retention.mjs",
        SCRIPTS / "opensms_collect_task_d_retention_population.mjs",
        SCRIPTS / "opensms_card_retention_readonly_lib.mjs",
        SCRIPTS / "opensms_collect_card_retention_report.mjs",
        SCRIPTS / "opensms_collect_task_e_card_retention.mjs",
        SCRIPTS / "opensms_execute_task_d_expiry.mjs",
        SCRIPTS / "opensms_execute_task_e_expiry.mjs",
        SCRIPTS / "opensms_expiry_receipt.mjs",
        SCRIPTS / "opensms_expiry_submission.mjs",
        SCRIPTS / "opensms_collect_task_f_kooda_cards.mjs",
        SCRIPTS / "grabandgo_task_f_lookup.mjs",
        SCRIPTS / "opensms_expired_card_probe.mjs",
        SCRIPTS / "opensms_expired_card_probe_lib.mjs",
        SCRIPTS / "grabandgo_task_f_session_queue.mjs",
        SCRIPTS / "opensms_task_f_export_xlsx.mjs",
        ROOT / "opensms_set_carrier_candidates.mjs",
        ROOT / "services/opensms_document_api/pipeline/add_a11_low_reply_sheet.mjs",
    ]
    shell_files = [
        ROOT / "services/opensms_document_api/pipeline/build_card_slot_report.sh",
        SCRIPTS / "run_opensms_parallel_audit.sh",
        SCRIPTS / "run_opensms_task_a.sh",
        SCRIPTS / "run_opensms_export.sh",
        SCRIPTS / "run_opensms_task_f.sh",
    ]
    checks = []
    syntax_jobs = (
        [(path, [str(PYTHON), "-m", "py_compile", str(path)]) for path in python_files]
        + [(path, [str(NODE), "--check", str(path)]) for path in node_files]
        + [(path, ["/bin/bash", "-n", str(path)]) for path in shell_files]
    )

    def check_syntax(job: tuple[Path, list[str]]) -> dict[str, Any]:
        path, argv = job
        completed = subprocess.run(argv, capture_output=True, text=True)
        return {"file": str(path), "passed": completed.returncode == 0, "error": completed.stderr.strip()}

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="opensms-syntax") as pool:
        checks.extend(pool.map(check_syntax, syntax_jobs))
    code_sources = [
        ROOT / "opensms_generate_opr_zero_candidates.py",
        SCRIPTS / "opensms_balance_readonly_raw.mjs",
        SCRIPTS / "opensms_resolve_active_carriers.mjs",
    ]
    legacy_names = tuple("B" + suffix for suffix in ("31", "32", "33"))
    legacy_hits = [str(path) for path in code_sources if any(name in path.read_text(encoding="utf-8") for name in legacy_names)]
    runner_text = (SCRIPTS / "opensms_patrol_runner.py").read_text(encoding="utf-8")
    main_text = runner_text.rsplit("\ndef main() -> int:\n", 1)[1]
    site_execution_text = (ROOT / "opensms_site_execution.mjs").read_text(encoding="utf-8")
    site_execution_contract = (
        "SiteExecutionLease(" in main_text
        and main_text.find("validate_task_selection(args)") < main_text.find("SiteExecutionLease(")
        and main_text.find("SiteExecutionLease(") < main_text.find("runner = PatrolRunner(")
        and 'child_env.pop("OPENSMS_SITE_LOCK_TOKEN", None)' in runner_text
        and 'child_env.update(getattr(self, "site_execution_environment", {}))' in runner_text
        and "OPENSMS_PACING_REQUEST_UNCONFIRMED" in main_text
        and "opensms_site_lock.mjs" in site_execution_text
        and "跨窗口网站整轮互斥" in policy_text
    )
    browser_session_separation = (
        '"OPENSMS_FORCE_RAW_CDP": "1"' in runner_text
        and '"OPENSMS_EXACT_CDP_ONLY": "1"' in runner_text
        and 'SCRIPTS / "opensms_export_fast_raw.mjs"]' in runner_text
        and '"OPENSMS_EXPORT_SUBMISSION_READY_PATH": str(ready_path)' in runner_text
        and 'SCRIPTS / "opensms_read_server_dates_fast_raw.mjs"' in runner_text
        and '"playwrightBrowserConnectionUsed": False' in runner_text
    )
    protocol_text = (ROOT / "opensms_protocol_client.mjs").read_text(encoding="utf-8")
    fast_export_text = (SCRIPTS / "opensms_export_fast_raw.mjs").read_text(encoding="utf-8")
    fast_dates_text = (SCRIPTS / "opensms_read_server_dates_fast_raw.mjs").read_text(encoding="utf-8")
    browser_session_separation = (
        browser_session_separation
        and "createRawBackgroundPage" in fast_export_text
        and "createRawBackgroundPage" in fast_dates_text
        and "background: true" in protocol_text
        and "connectOverCDP" not in fast_export_text
        and "connectOverCDP" not in fast_dates_text
    )
    authenticated_page_preference = (
        "old /login tab beside the authenticated" in protocol_text
        and "connectOpenSmsBrowser(cdp, pathName)" in protocol_text
        and "route(candidate) !== '/login'" in protocol_text
    )
    routing_text = (ROOT / "opensms_generate_opr_zero_candidates.py").read_text(encoding="utf-8")
    analysis_text = (ROOT / "opensms_analyze_current.py").read_text(encoding="utf-8")
    e_recovery_text = (ROOT / "opensms_generate_e_reply_recovery.py").read_text(encoding="utf-8")
    scan_cache_text = (ROOT / "opensms_export_scan_cache.py").read_text(encoding="utf-8")
    carrier_guard_text = (ROOT / "opensms_set_carrier_candidates.mjs").read_text(encoding="utf-8")
    shared_scan_cache_contract = (
        'CACHE_FORMAT = "opensms-export-scan-cache-v1"' in scan_cache_text
        and "expected_source=source" in routing_text
        and "expected_source=args.export" in e_recovery_text
        and "expected_source=path" in analysis_text
        and routing_text.count('parser.add_argument("--scan-cache")') == 1
        and e_recovery_text.count('parser.add_argument("--scan-cache")') == 1
        and analysis_text.count('ap.add_argument("--scan-cache")') == 1
        and '"processingSource"' in runner_text
        and '"processingSystemKooda"' in runner_text
        and "scanCacheHashBound=True" in runner_text
        and '"candidateProcessWorkbookReopens": 0' in runner_text
        and "本轮新鲜源表在本机只允许生成一次SHA-256绑定的共享扫描缓存" in policy_text
    )
    b_standard_threshold_contract = (
        "B1_B11_USE2_TO_B11_THRESHOLD = 240" in routing_text
        and "B1_B11_TO_B12_THRESHOLD = 240" in routing_text
        and "B_V_TEN_DAY_SEND_THRESHOLD = 240" in routing_text
        and "b1_b11_use2_lt240_to_b11" in routing_text
        and "B_STANDARD_USE2_THRESHOLD = 240" in carrier_guard_text
        and "B_STANDARD_GTE240_THRESHOLD = 240" in carrier_guard_text
        and "V_ZONE_TEN_DAY_THRESHOLD = 240" in carrier_guard_text
        and "10 日发送窗口数 `<240` 时转 B11" in policy_text
        and "10 日发送窗口数 `<240`：转 B11V" in policy_text
    )
    rest_zone_entry_enabled_contract = (
        "REST_ZONE_ENTRY_ENABLED = False" in routing_text
        and "REST_ZONE_ENTRY_ENABLED = false" in carrier_guard_text
        and '"restZoneEntryDisabledRoutes"' in routing_text
        and "restZoneEntryEnabled=false" in policy_text
        and "休息区转入功能已关闭" in carrier_guard_text
        and "candidate.restZoneEntryEnabled !== REST_ZONE_ENTRY_ENABLED" in carrier_guard_text
        and "opensms_e_origin_backup_" in carrier_guard_text
    )
    quick_renew_c_protection_contract = (
        'NEVER_PROCESS_SOURCE_CARRIERS = ("快过期续费C", "快过期续费T")' in routing_text
        and "new Set(['快过期续费C', '快过期续费T'])" in carrier_guard_text
        and "快过期续费C/T永久保护" in carrier_guard_text
        and "快过期续费C/T保护" in policy_text
        and "强制任务C/D/E不构成例外" in policy_text
        and "仅获本轮授权的任务B" in policy_text
        and "isAllCarrierTaskB(candidate)" in carrier_guard_text
        and "sourceCarrierScope=all_carriers_v1" in policy_text
        and "expiredCoveragePassed" in (SCRIPTS / "opensms_generate_task_b_candidates.py").read_text(encoding="utf-8")
        and "tomorrowCoveragePassed" in (SCRIPTS / "opensms_generate_task_b_candidates.py").read_text(encoding="utf-8")
    )
    task_c_generator_text = (SCRIPTS / "opensms_generate_task_c_candidates.py").read_text(encoding="utf-8")
    final_state_text = (SCRIPTS / "opensms_collect_final_carrier_state.mjs").read_text(encoding="utf-8")
    task_b_priority_lock_contract = (
        "taskBPriorityLockEnforced" in task_c_generator_text
        and "TASK_B_PRIORITY_SOURCE_CARRIERS" in carrier_guard_text
        and "任务B已过期/明日到期优先级锁定" in carrier_guard_text
        and "任务B过期/明日到期结果禁止被任务C覆盖" in final_state_text
        and "task_b_priority_invalid_count" in (SCRIPTS / "opensms_parallel_audit.py").read_text(encoding="utf-8")
        and "任务 B 的已过期和明日到期路由优先级高于任务 C" in policy_text
    )
    task_e_generator_text = (SCRIPTS / "opensms_generate_task_e_candidates.py").read_text(encoding="utf-8")
    task_e_executor_text = (SCRIPTS / "opensms_execute_task_e_expiry.mjs").read_text(encoding="utf-8")
    task_e_retention_text = (SCRIPTS / "opensms_card_retention_readonly_lib.mjs").read_text(encoding="utf-8")
    task_d_executor_text = (SCRIPTS / "opensms_execute_task_d_expiry.mjs").read_text(encoding="utf-8")
    expiry_receipt_text = (SCRIPTS / "opensms_expiry_receipt.mjs").read_text(encoding="utf-8")
    expiry_receipt_contract = all(token in expiry_receipt_text for token in (
        "Number.isSafeInteger(n) && n >= 0", "response?.code !== 200",
        "!Array.isArray(data.notFound)", "matchedCardCount + notFound.length !== inputCount",
        "missing.size !== notFound.length", "(!allowNotFound && notFound.length)",
    ))
    task_d_submission_contract = all(token in task_d_executor_text for token in (
        "MARKER_BATCH_SIZE = 20", "submission_outcome_uncertain", "assertNoTaskDReplay",
        "fs.open(lockPath, 'wx')", "automaticRetryBlocked", "validateTaskDRetentionSourceProof",
    ))
    task_d_prepare_text = (SCRIPTS / "opensms_prepare_task_d.mjs").read_text(encoding="utf-8")
    task_d_run_text = runner_text.split("    def run_task_d(", 1)[1].split("    def collect_task_d_notification_summary", 1)[0]
    task_d_submission_contract = task_d_submission_contract and expiry_receipt_contract and (
        "validateExpiryReceipt(response, batchCards)" in task_d_executor_text
        and
        "retentionCarrierTransferEnabled: false" in task_d_prepare_text
        and "writeCarrierCandidate" not in task_d_prepare_text
        and "self.run_write(" not in task_d_run_text
        and "validateTaskDRetentionNotificationIdentity" in task_d_executor_text
        and "assertTaskDNotificationOnly(manifest)" in task_d_executor_text
        and "转入B14保留区工作已删除" in carrier_guard_text
        and "转入保留区工作已删除" in policy_text
    )
    task_e_contract = (
        'ELIGIBLE_EXPIRY_STATUSES = ("6天", "快过期", "已过期")' in task_e_generator_text
        and 'NEVER_PROCESS_SOURCE_CARRIERS = ("快过期续费C", "快过期续费T")' in task_e_generator_text
        and '"ledgerDedupeKey": "phone"' in task_e_generator_text
        and "batchSetChatExpire" in task_e_executor_text
        and "MARKER_BATCH_SIZE = 20" in task_e_executor_text
        and "submission_outcome_uncertain" in task_e_executor_text
        and "automaticRetryBlocked" in task_e_executor_text
        and "--recovery-progress" in task_e_executor_text
        and "idempotentRecoveryNoChange" in task_e_executor_text
        and expiry_receipt_contract
        and "validateExpiryReceipt(response, batchCards, { allowNotFound: true })" in task_e_executor_text
        and "passed_with_not_found" in task_e_executor_text
        and "notWrittenCards" in task_e_executor_text
        and "isReconciledPartialReceipt" in task_e_executor_text
        and "submitted_transactionally_confirmed" in task_e_executor_text
        and "任务E默认关闭" in policy_text
        and "同一手机号一经成功提交" in policy_text
        and "collectCardRetention" in task_e_retention_text
        and "retentionDeadlineMatches" in task_e_generator_text
        and "retentionComparisonDoesNotChangeCandidates" in task_e_generator_text
        and "界面“新到期日”作为本规则中的“下卡通知时间”" in policy_text
    )
    task_f_db_text = (SCRIPTS / "opensms_task_f_db.py").read_text(encoding="utf-8")
    task_f_inventory_text = (SCRIPTS / "opensms_collect_task_f_kooda_cards.mjs").read_text(encoding="utf-8")
    task_f_lookup_text = (SCRIPTS / "grabandgo_task_f_lookup.mjs").read_text(encoding="utf-8")
    document_builder_text = (
        ROOT / "services/opensms_document_api/pipeline/process_card_slots.py"
    ).read_text(encoding="utf-8")
    document_verifier_text = (
        ROOT / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py"
    ).read_text(encoding="utf-8")
    task_f_background_text = (SCRIPTS / "opensms_task_f_background.py").read_text(encoding="utf-8")
    # Check the concrete current isolation/cap/fallback contract rather than a
    # historical whole policy sentence. Editorial rewording must not disable a
    # safe implementation, while removing the actual safeguards still fails.
    task_f_policy_section = policy_text.split("### 任务F", 1)[-1].split("### 任务F每5分钟", 1)[0]
    compact_f_policy = re.sub(r"[\s`]", "", task_f_policy_section)
    task_f_isolation_contract = (
        bool(re.search(r"固定请求8个(?:相互)?隔离(?:的)?本机HTTPCookie容器", compact_f_policy))
        and ("隔离不成立回落串行" in compact_f_policy or "自动降为1个串行工作器" in compact_f_policy)
        and "const targetWorkers = Math.min(workerCount, items.length);" in task_f_lookup_text
        and "await requireUniqueWorkerSessions(await initializeConcurrentWorkers(" in task_f_lookup_text
        and "sessionIds.length !== expectedCount || uniqueCount !== expectedCount" in task_f_lookup_text
        and "const rechecks = await Promise.allSettled(candidate.map" in task_f_lookup_text
        and "macos-keychain-independent-form-login-serial-fallback', 1, credential, requestFactory" in task_f_lookup_text
    )
    task_f_contract = (
        "OPENSMS_REPORT_DATE_CACHE_PATH" in runner_text
        and "freeze_report_date_cache" in runner_text
        and "database_writer_lock" in task_f_background_text
        and "start_new_session=True" in task_f_background_text
        and "def validate_job" in task_f_background_text
        and "activationDateUpdatedCount" in task_f_db_text
        and "opensms_task_f_delta.py" in runner_text
        and "附件中ICCID存在但`lblActivateDate`为空时先记录`activation_missing`" in policy_text
        and '"resultsWriteBackToDocument": False' in task_f_background_text
        and "def document_backfill_rows" in task_f_db_text
        and '"--document-sha256"' in task_f_db_text
        and '"reportBackfillMode"' in runner_text
        and '"cloudUploadAttempted": False' in runner_text
        and task_f_isolation_contract
        and "task_f_kooda_iccid_library_latest.json" in policy_text
        and 'TASK_F_CATALOG_SOURCE' in runner_text
        and 'TASK_F_LATEST_LIBRARY' in runner_text
        and 'OPENSMS_TASK_F_POLICY_CATALOG_SYNC' in runner_text
        and '"import-catalog"' in task_f_db_text
        and '"export-catalog"' in task_f_db_text
        and 'def import_catalog' in task_f_db_text
        and 'def export_catalog' in task_f_db_text
        and 'OPENSMS_FORCE_TASK_F' in runner_text
        and 'OPENSMS_ENABLE_TASK_F' in runner_text
        and 'OPENSMS_EXECUTE_TASK_F' in runner_text
        and 'KOODA_QUERY = \'koo\'' in task_f_inventory_text
        and 'const PAGE_SIZE = 500' in task_f_inventory_text
        and 'OPENSMS_TASK_F_POLICY_CATALOG_SYNC' in task_f_inventory_text
        and 'source_iccid TEXT NOT NULL' in task_f_db_text
        and 'record_schema_version INTEGER NOT NULL DEFAULT 1' in task_f_db_text
        and 'source_data_json TEXT NOT NULL' in task_f_db_text
        and 'RECORDED_WEB_FIELDS' in task_f_db_text
        and 'return activation + timedelta(days=29)' in task_f_db_text
        and "TERMINAL_STATUSES = (\"found\", \"not_found\", \"activation_missing\")" in task_f_db_text
        and 'DEFAULT_WORKERS = 8' in task_f_lookup_text
        and 'MAX_WORKERS = 8' in task_f_lookup_text
        and "adaptiveConcurrency: true" in task_f_lookup_text
        and "sharedSessionForcesSerial: true" in task_f_lookup_text
        and "shared-aspnet-session-serial-worker" in task_f_lookup_text
        and "macos-keychain-independent-form-login" in task_f_lookup_text
        and "databaseCheckpointAfterEveryBatch" in runner_text
        and "batches = [] if not pending_rows else" in runner_text
        and "identityMismatchStoredAsTerminal: false" in task_f_lookup_text
        and "sharedSessionNotFoundRequiresSerialConfirmation: true" in task_f_lookup_text
        and "shared-session-not-found-confirmation" in task_f_lookup_text
        and "精确验证查询ICCID、列表`SIM Number`和详情ICCID三者一致" in policy_text
        and 'DEFAULT_DELAY_MS = 1000' in task_f_lookup_text
        and 'DEFAULT_PROGRESS_INTERVAL_MS = 300000' in task_f_lookup_text
        and 'const progressIntervalMs = DEFAULT_PROGRESS_INTERVAL_MS;' in task_f_lookup_text
        and 'LIVE_PROGRESS_SNAPSHOT_INTERVAL_MS = 10000' in task_f_lookup_text
        and 'clearInterval(snapshotTimer);' in task_f_lookup_text
        and 'databaseMissingOnly: pendingPayload.reportBackfillMode !== true' in task_f_lookup_text
        and "recordedWebFields: ['Purchase Date', 'Act. Request Date', 'Activation Date', 'SIM Number', 'SKU']" in task_f_lookup_text
        and 'fullSourceDataIncluded: true' in task_f_lookup_text
        and 'sourceData: raw' in task_f_inventory_text
        and 'CUSTOMER_SIM_LOGIN_MARKER' in task_f_lookup_text
        and 'exactCustomerSimMarker' in task_f_lookup_text
        and 'connectBitBrowser430' in task_f_lookup_text
        and "Target.createTarget" in task_f_lookup_text
        and "background: true" in task_f_lookup_text
        and 'requestWithJavascriptFallback' in task_f_lookup_text
        and "same-origin-http" in task_f_lookup_text
        and "background-page-javascript" in task_f_lookup_text
        and 'firecrawlSensitiveLookupUsed' in runner_text
        and 'OPENSMS_REPORT_DATE_CACHE_PATH' in document_builder_text
        and 'fgColor="FFC7CE"' in document_builder_text
        and '"task_f_missing_activation_red_review"' in document_verifier_text
        and '"task_f_missing_dates_new_card_review"' in document_verifier_text
        and '"system_kooda_source_review"' in document_verifier_text
        and 'OPENSMS_SYSTEM_KOODA_PATH' in runner_text
        and 'systemRows: collected.rows' in task_f_inventory_text
        and 'def classify_number_change' in document_builder_text
        and '"missing_dates_change_status": "未换号"' in document_builder_text
        and '未换号' in policy_text and '新卡' in policy_text
        and 'days_until_expiry <= 0' in document_builder_text
        and 'return "6天"' in document_builder_text
    )
    document_audit_text = (SCRIPTS / "opensms_parallel_audit.py").read_text(encoding="utf-8")
    audit_launcher_text = (SCRIPTS / "run_opensms_parallel_audit.sh").read_text(encoding="utf-8")
    audit_runtime_contract = (
        "/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" in audit_launcher_text
        and "OPENSMS_AUDIT_PYTHON" in audit_launcher_text
        and "run_opensms_parallel_audit.sh" in runner_text
    )
    document_dimension_cache_contract = (
        "assoc_max_column = ws.max_column" in document_builder_text
        and "main_max_column = ws.max_column" in document_builder_text
        and "range(1, ws.max_column + 1)" not in document_builder_text
        and "禁止在逐行或逐单元格循环中反复读取 `worksheet.max_column`" in policy_text
    )
    google_dependency_disabled_contract = (
        execution_contract()["googleAssociationEnabled"] is False
        and execution_contract()["googleDisabledReason"] == "user_disabled"
        and 'child_env["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"] = "0"' in runner_text
        and '"--no-google"' in runner_text
        and 'opensms_download_google_sheet.mjs' in runner_text[
            runner_text.index("    def acquire("):runner_text.index("    def verify_system_kooda_acquisition(")
        ]
        and execution_contract()["googleChatrDateFallbackEnabled"] is True
        and "OPENSMS_CHATR_GOOGLE_DATES_PATH" in runner_text
        and "OPENSMS_GOOGLE_ASSOCIATION_ENABLED" in document_builder_text
        and "google_dependency" in document_builder_text
        and "system_source_only" in document_builder_text
        and "google_dependency_review" in document_verifier_text
        and "googleAssociationEnabled" in document_audit_text
        and "user_disabled" in policy_text
    )
    unused_port_contract = (
        'create_sheet("系统无-谷歌表有")' in document_builder_text
        and "def build_unused_port_rows" in document_builder_text
        and '"device_port_capacity": 64' in document_builder_text
        and '"unused_port_review"' in document_verifier_text
        and "review_port_presence" in document_verifier_text
        and "google_comparison_slots" in document_builder_text
        and '"系统无-谷歌表有"' in document_audit_text
        and "每台数字设备严格按1至64共64个物理端口展开" in re.sub(r"\s+", "", policy_text)
        and "system_source_only" in document_builder_text
        and "receipt_device_scope_matches" in document_verifier_text
        and "系统中无" in document_builder_text
        and "注册失败" in document_builder_text
    )
    ordered_barrier_tokens = (
        'self.run_standard_writes(candidates)',
        'self.run_task_b(document, acquire)',
        'self.run_task_c(document, acquire)',
        'self.run_task_d(document, acquire)',
        'self.run_task_e(document, acquire)',
        'refresh = self.refresh_workbook(document)',
    )
    ordered_barrier_positions = [runner_text.find(token) for token in ordered_barrier_tokens]
    stage_barriers_present = (
        all(position >= 0 for position in ordered_barrier_positions)
        and ordered_barrier_positions == sorted(ordered_barrier_positions)
        and 'stage_name="refreshAfterStandard"' in runner_text
        and 'stage_name="refreshAfterTaskB"' in runner_text
        and 'stage_name="refreshAfterTaskC"' in runner_text
        and 'stage_name="refreshAfterTaskD"' in runner_text
    )
    task_f_post_document_background = (
        runner_text.find('self.start_background_task_f(document, acquire)') >= 0
        and runner_text.find('document = self.finish_document()') >= 0
        and runner_text.find('document = self.finish_document()') < runner_text.rfind('self.start_background_task_f(document, acquire)')
        and "resultsApplyNextExport" in runner_text
    )
    contract = execution_contract()
    program_only_contract = (
        contract["executionMode"] == "local-program-only"
        and contract["manualAiStepsRequired"] is False
        and contract["stageOrder"] == list(STAGE_ORDER)
        and contract["balanceSummaryGroups"] == list(BALANCE_SUMMARY_GROUPS)
        and '"balanceGroups": build_balance_summary_groups(balance)' in runner_text
        and "聊天区“余量与提醒”每轮必须逐组显示全部十二组" in policy_text
        and PROGRAM_ENTRYPOINT.is_file()
    )
    exact_bitbrowser_profile = (
        BITBROWSER_430_PROFILE_ID in runner_text
        and "resolve_bitbrowser_430_cdp" in runner_text
        and 'default=os.environ.get("BITBROWSER_CDP")' in runner_text
    )
    default_authorization = validate_authorization(
        argparse.Namespace(
            force_task_b=False,
            force_task_c=False,
            force_task_d=False,
            force_task_e=False,
            force_task_f=False,
            authorization_source=None,
        )
    )
    task_bcde_default_off_contract = (
        all(default_authorization[name] is False for name in ("taskB", "taskC", "taskD", "taskE"))
        and 'action="store_true"' in runner_text
        and "TASK_BCDE_GATE_ENV_VARS" in runner_text
        and "child_env.pop(gate_name, None)" in runner_text
        and 'env["OPENSMS_FORCE_TASK_D"] = "1"' in runner_text
        and "任务 B、任务 C、任务 D、任务 E 统一默认关闭" in policy_text
        and "父进程残留环境变量均不得启动或延续这些任务" in policy_text
    )
    task_a_args = build_parser().parse_args(["run", "--task-a-only"])
    task_a_authorization = validate_authorization(task_a_args)
    task_a_rejected_modes = []
    for flag in ("--force-task-b", "--force-task-c", "--force-task-d", "--force-task-e", "--force-task-f", "--task-f-only"):
        try:
            validate_task_selection(build_parser().parse_args(["run", "--task-a-only", flag]))
        except PatrolError:
            task_a_rejected_modes.append(flag)
    task_a_view = object.__new__(PatrolRunner)
    task_a_view.args, task_a_view.run_id = task_a_args, "offline-static-check"
    task_a_child = task_a_view.child_environment({
        **{field: "1" for field in TASK_BCDE_GATE_ENV_VARS},
        "OPENSMS_FORCE_TASK_F": "1", "OPENSMS_ENABLE_TASK_F": "1", "OPENSMS_EXECUTE_TASK_F": "1",
        "OPENSMS_TASK_F_POLICY_CATALOG_SYNC": "1", "OPENSMS_SYSTEM_KOODA_SNAPSHOT": "1",
    })
    task_a_only_contract = (
        execution_contract(True)["stageOrder"] == ["document", "standard", "expiredCardProbe"]
        and execution_contract(True)["taskFEnabledForRun"] is False
        and all(task_a_authorization["task" + task] is False for task in "BCDEF")
        and len(task_a_rejected_modes) == 6
        and all(not name.startswith("OPENSMS_TASK_F_") for name in task_a_child)
        and "OPENSMS_FORCE_TASK_F" not in task_a_child
        and task_a_child.get("OPENSMS_SYSTEM_KOODA_SNAPSHOT") == "1"
        and "OPENSMS_SYSTEM_KOODA_SNAPSHOT" in task_f_inventory_text
        and "def check_task_a_only_scope" in document_audit_text
        and "run_opensms_task_a.sh" in policy_text
        and 'if self.task_a_only:\n                self.skip_task_f()\n            else:' in runner_text
        and main_text.find('validate_task_selection(args)') < main_text.find('site_lease = SiteExecutionLease(')
        and main_text.find('site_lease = SiteExecutionLease(') < main_text.find('state = read_json(args.resume_state)')
    )
    reliable_background_acquisition_contract = (
        execution_contract()["automaticRecoveryPolicy"] == "v53-startup-and-allowlisted-reads-only"
        and "def record_partial_acquisition" in runner_text
        and "sourceVerified=True" in runner_text
        and "serverDateVerified=True" in runner_text
        and 'partial.get("runId") != self.run_id' in runner_text
        and 'child_env.pop("OPENSMS_RAW_API_TARGET_ID", None)' in runner_text
        and 'child_env["OPENSMS_RAW_API_TARGET_ID"] = self.api_target_id' in runner_text
        and "self.close_api_target()\n            final = self.finalize" in runner_text
        and "partialAcquisition" in policy_text
        and "finalWorkbook=null" in policy_text
    )
    new_browser_contract = (
        read_json(SCRIPTS / "opensms_execution_settings.json") == {
            "browser":"cloakbrowser", "requestCompletionGapMs":1000, "requestConcurrency":1}
        and "resolve_configured_browser()" in main_text
        and "report_lease = SiteExecutionLease(file=REPORT_LOCK" in main_text
        and 'child_env["OPENSMS_EXACT_CDP_ONLY"] = "1"' in runner_text
        and "OPENSMS_SITE_JOB_OWNER_GONE" in site_execution_text
        and "completionGapMs: 1000" in (ROOT / "opensms_request_pacing.mjs").read_text(encoding="utf-8")
    )
    passed = (
        all(item["passed"] for item in checks)
        and site_execution_contract
        and new_browser_contract
        and not legacy_hits
        and browser_session_separation
        and authenticated_page_preference
        and stage_barriers_present
        and program_only_contract
        and exact_bitbrowser_profile
        and task_bcde_default_off_contract
        and task_a_only_contract
        and reliable_background_acquisition_contract
        and b_standard_threshold_contract
        and rest_zone_entry_enabled_contract
        and quick_renew_c_protection_contract
        and task_b_priority_lock_contract
        and task_d_submission_contract
        and task_e_contract
        and task_f_contract
        and google_dependency_disabled_contract
        and unused_port_contract
        and audit_runtime_contract
        and shared_scan_cache_contract
        and document_dimension_cache_contract
        and task_f_post_document_background
    )
    result = {
        "status": "passed" if passed else "failed",
        **contract,
        "mode": "offline-static-check",
        "siteExecutionContract": {"passed": site_execution_contract, "crossWindow": True,
                                  "wholeJobExclusive": True, "staleLockStealing": False},
        "configuredBrowserContract": {"passed":new_browser_contract, "browser":"cloakbrowser",
                                      "requestCompletionGapMs":1000, "requestConcurrency":1,
                                      "fallbackToOldBrowser":False, "manualLoginRequired":True},
        "livePatrolExecuted": False,
        "policy": str(POLICY),
        "syntaxChecks": checks,
        "removedCarrierRouteHits": legacy_hits,
        "browserSessionSeparation": {
            "exportAndDateUsePageSession": False,
            "exportAndDateUseRawBackgroundTargets": browser_session_separation,
            "playwrightBrowserConnectionUsed": False,
            "apiOnlyReadsUseRawCdp": True,
            "authenticatedPagePreferredOverStaleLoginTab": authenticated_page_preference,
        },
        "programOnlyContractPassed": program_only_contract,
        "taskAOnlyContract": {
            "passed": task_a_only_contract,
            "taskSelection": "A", "explicitFlag": "--task-a-only",
            "excludedTasks": list("BCDEF"), "conflictsRejectedBeforeConnection": task_a_rejected_modes,
            "systemKoodaSnapshotRetained": True, "taskFEnvironmentCleared": True,
            "manualDefaultTaskFEnabled": contract["taskFEnabledForRun"],
        },
        "reliableBackgroundAcquisitionContract": {
            "passed": reliable_background_acquisition_contract,
            "partialFactsPersistedBeforeStageCompletion": True,
            "serverDateRequiresThreeWayAgreement": True,
            "failureHasNoFinalWorkbook": True,
            "apiTargetPinnedForRun": True,
            "cleanupBeforeDeliveryManifest": True,
        },
        "sharedScanCacheContract": {
            "passed": shared_scan_cache_contract,
            "hashBound": True,
            "candidateWorkbookScansPerRun": 1,
            "candidateProcessWorkbookReopens": 0,
            "processingInputsUseRunPrivateLocalCopies": True,
        },
        "bStandardThresholdContract": {
            "passed": b_standard_threshold_contract,
            "b1B11Use2ToB11LessThan": 240,
            "b1B11FallbackToB12GreaterThanOrEqual": 240,
            "b1vB11vThreshold": 240,
        },
        "restZoneEntryEnabledContract": {
            "passed": rest_zone_entry_enabled_contract,
            "restZoneEntryEnabled": False,
            "restZoneExitRecoveryEnabled": True,
        },
        "quickRenewCProtectionContract": {
            "passed": quick_renew_c_protection_contract,
            "carrierNames": ["快过期续费C", "快过期续费T"],
            "forcedTasksAreExceptions": {"B": True, "C": False, "D": False, "E": False},
            "taskBSourceCarrierScope": "all_carriers_v1",
        },
        "taskBPriorityLockContract": {
            "passed": task_b_priority_lock_contract,
            "taskCMaySupersedeExpiredOrTomorrowDue": False,
        },
        "taskEContract": {
            "passed": task_e_contract,
            "default": "off",
            "eligibleExpiryStatuses": ["6天", "快过期", "已过期"],
            "ledgerDedupeKey": "phone",
            "markerBatchSize": 20,
            "gatewayTimeoutUncertainRetryBlocked": True,
            "userAuthorizedFreshManifestRecoverySupported": True,
            "liveWritesSerial": True,
        },
        "taskDSubmissionContract": {
            "passed": task_d_submission_contract,
            "retentionCarrierTransferEnabled": False,
            "markerBatchSize": 20,
            "liveWritesSerial": True,
            "uncertainRetryBlockedBeforeBrowser": True,
        },
        "taskFContract": {
            "passed": task_f_contract,
            "isolatedSessionsAndSerialFallbackChecked": task_f_isolation_contract,
            "default": "on_post_document_catalog_incremental",
            "openSmsPageSize": 500,
            "defaultWorkers": 8,
            "perWorkerMinimumDelayMs": 1000,
            "progressIntervalSeconds": 300,
            "databaseMissingOnly": False,
            "databaseActivationMissingOnly": True,
            "recordSchemaVersion": 2,
            "recordedWebFields": ["Purchase Date", "Act. Request Date", "Activation Date", "SIM Number", "SKU"],
            "fullOpenSmsSourceDataIncluded": True,
            "openSmsInventoryBrowser": "BitBrowser 430 existing profile",
            "grabAndGoBrowser": "isolated local HTTP login containers; BitBrowser 430 background fallback",
            "transportPriority": ["same-origin-http", "background-page-javascript"],
            "backgroundTargetRequiredOnlyForFallback": True,
            "batchSize": 250,
            "databaseCheckpointAfterEveryBatch": True,
            "database": str(TASK_F_DB),
            "catalogSource": str(TASK_F_CATALOG_SOURCE),
            "latestIccidLibrary": str(TASK_F_LATEST_LIBRARY),
            "cutoffRule": "activation_date_plus_29_days_inclusive_30_day_period",
            "googleDateFallbackEnabled": False,
            "documentGeneratedBeforeBackgroundLookup": task_f_post_document_background,
            "supplementalResultsApplyNextExport": True,
            "documentDateSource": "system_then_frozen_local_cache",
            "resultsWriteBackToDocument": False,
            "missingActivationFillRgb": "FFC7CE",
        },
        "googleDependencyDisabledContract": {
            "passed": google_dependency_disabled_contract,
            "scope": "legacy_full_association_only",
            "googleAssociationEnabled": False,
            "googleDisabledReason": "user_disabled",
            "downloadDispatched": False,
            "googleChatrDateFallbackEnabled": True,
            "chatrDateDownloadConfigured": True,
            "legacyAssociationFallbackAllowed": False,
            "googleOnlySheetsRequired": False,
            "unusedPortDeviceScope": "system_source_only",
        },
        "unusedPortSheetContract": {
            "passed": unused_port_contract,
            "sheet": "系统无-谷歌表有",
            "googleComparisonColumn": "谷歌表对比",
            "googlePresentValue": "谷歌表存在",
            "adjacentRightSheet": "谷歌有-系统无",
            "portsPerDevice": 64,
            "statuses": ["系统中无", "注册失败"],
        },
        "auditRuntimeContract": {
            "passed": audit_runtime_contract,
            "defaultRuntime": str(PYTHON),
            "openpyxlAvailable": True,
        },
        "documentDimensionCacheContract": {
            "passed": document_dimension_cache_contract,
            "repeatedMaxColumnReadsInsideRowLoops": 0,
        },
        "documentStageBarriersPassed": stage_barriers_present,
        "bitBrowser430Resolution": {
            "profileId": BITBROWSER_430_PROFILE_ID,
            "portFile": str(BITBROWSER_430_PORT_FILE),
            "dynamicExactProfile": exact_bitbrowser_profile,
        },
        "authorizationGates": {
            "passed": task_bcde_default_off_contract,
            "taskBDefault": "off",
            "taskCDefault": "off",
            "taskDDefault": "off",
            "taskEDefault": "off",
            "taskFDefault": "on_post_document_catalog_incremental",
            "requiredCliSource": "current-user",
            "requiredEnvironment": ["OPENSMS_FORCE_TASK_B=1", "OPENSMS_FORCE_TASK_C=1", "OPENSMS_FORCE_TASK_D=1", "OPENSMS_ENABLE_TASK_D=1", "OPENSMS_EXECUTE_TASK_D=1", "OPENSMS_FORCE_TASK_E=1", "OPENSMS_ENABLE_TASK_E=1", "OPENSMS_EXECUTE_TASK_E=1"],
            "taskFDiagnosticEnvironment": ["OPENSMS_FORCE_TASK_F=1", "OPENSMS_ENABLE_TASK_F=1", "OPENSMS_EXECUTE_TASK_F=1"],
        },
        "passed": passed,
    }
    if not passed:
        raise PatrolError(json.dumps(result, ensure_ascii=False))
    return result


class TaskFWorkersAction(argparse.Action):
    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: str | None = None) -> None:
        namespace.task_f_workers_supplied = True
        setattr(namespace, self.dest, values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenSMS 10日报表一键巡检程序")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="仅做离线静态检查，不连接浏览器、不执行巡检")
    status = subparsers.add_parser("status", help="只读进度与本地进程，不连接浏览器、不启动或重试任务")
    status.add_argument("--state", required=True, type=Path)
    status.add_argument("--json", action="store_true")
    report = subparsers.add_parser("refresh-failure-report", help="仅用失败断点重建本地报告与清单，不联网、不重试任何任务")
    report.add_argument("--state", required=True, type=Path)
    report.add_argument("--json", action="store_true")
    finish = subparsers.add_parser("finish-local", help="仅完成业务已确认后的本地报告与审计，不联网、不重放任务")
    finish.add_argument("--state", required=True, type=Path)
    finish.add_argument("--authorization-source", required=True, choices=("current-user",))
    finish.add_argument("--json", action="store_true")
    run = subparsers.add_parser("run", help="执行一轮完整巡检")
    # The new browser's exact endpoint comes from the verified local session,
    # not stale BitBrowser/CDP variables inherited from another window.
    run.add_argument("--cdp", default=None)
    run.add_argument("--task-a-only", action="store_true", help="仅执行本轮新鲜任务A，完整跳过B/C/D/E/F且禁止断点续跑")
    run.add_argument("--restore-unexpired-to-a11", action="store_true", help="仅当前明确授权：恢复四个隔离/过期区未到截止日的号码到A11，跳过普通流转")
    run.add_argument("--force-task-b", action="store_true")
    run.add_argument("--force-task-c", action="store_true")
    run.add_argument("--force-task-d", action="store_true")
    run.add_argument("--force-task-e", action="store_true")
    run.add_argument("--task-e-recovery-progress", type=Path)
    run.add_argument("--force-task-f", action="store_true")
    run.add_argument("--task-f-only", action="store_true")
    run.add_argument("--task-f-sample-size", type=int, choices=range(1, 101))
    run.add_argument("--task-f-repair-lookup", type=Path)
    run.add_argument("--task-f-debug-iccid")
    run.add_argument("--authorization-source", choices=("current-user", "timer", "history"))
    run.add_argument("--io-workers", type=int, default=4)
    run.add_argument("--analysis-workers", type=int, default=4)
    run.set_defaults(task_f_workers_supplied=False)
    run.add_argument("--task-f-workers", type=int, default=8, choices=range(1, 9), action=TaskFWorkersAction)
    run.add_argument("--refresh-engine", choices=("targeted-ooxml", "artifact-tool"), default="targeted-ooxml")
    run.add_argument("--resume-state", type=Path)
    run.add_argument("--continue-task-g", action="store_true", help="仅当前用户授权：继续已证明零提交的G查询失败及收尾，不重放其它阶段")
    run.add_argument("--continue-task-d", action="store_true", help="仅当前用户授权：同平台日续接已停止的D卡保留HTTP502余下批次及E/G；不重放导出/B/C/成功D批次")
    run.add_argument('--skip-data-anomalies-this-run', action='store_true', help='仅当前用户另行明确授权：续接D零提交数据异常准备失败；不适用于不明提交或定时入口')
    run.add_argument("--json", action="store_true")
    return parser


def compact_launcher_result(result: dict[str, Any]) -> dict[str, Any]:
    """The chat boundary carries aggregates, never unbounded phone/ICCID lists.

    This is an output-only projection. The complete summary, business evidence,
    failure checkpoints and all authorization decisions are unchanged.
    """
    projected = _launcher_result_fields(result)
    detail_keys = {
        "iccid_conflicts", "iccidConflicts", "filteredUnmatchedCards",
        "neverProcessSourceExcludedCards", "missing", "structuralErrors",
        "archivedMatches", "rows", "results", "phones", "cardNos",
        "cards", "uncertainCards", "journal", "records",
    }

    def trim(value):
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                if key in detail_keys and isinstance(item, list):
                    output[key + "DetailCount"] = len(item)
                else:
                    output[key] = trim(item)
            return output
        if isinstance(value, list):
            return [trim(item) for item in value]
        return value

    compact = trim(projected)
    # Carrier/scope comparison detail is only displayed in the complete MD.
    # Keep it in the complete JSON, not each model invocation. This projection
    # never changes the stored summary or the independently verified metrics.
    comparison = compact.get("carrierReplyComparison")
    if isinstance(comparison, dict):
        compact["carrierReplyComparison"] = {key: comparison[key] for key in (
            "runId", "serverDate", "carrierCount", "comparableCount",
            "currentUnknownCount", "independentReadbackPassed", "snapshotPath",
            "rowsDetailCount",
        ) if key in comparison}
        compact["carrierReplyComparison"]["detailsInCompleteResult"] = True
    scope = compact.get("scopeComparison")
    if isinstance(scope, dict):
        compact["scopeComparison"] = {key: scope[key] for key in (
            "snapshotPath", "snapshotSha256", "status", "independentReadbackPassed",
        ) if key in scope}
        compact["scopeComparison"]["detailsInCompleteResult"] = True
    # These lists/provenance blocks are not used by the seven-section summary.
    # Business counts, failures/uncertain writes, verification flags and current
    # output links remain untouched (including unknown/null rather than zero).
    task_b = compact.get("taskB")
    if isinstance(task_b, dict) and isinstance(task_b.get("sourceCarrierCoverage"), list):
        task_b["sourceCarrierCoverageDetailCount"] = len(task_b.pop("sourceCarrierCoverage"))
    notice = (compact.get("taskD") or {}).get("notificationSummary")
    if isinstance(notice, dict):
        notice.pop("evidence", None)
    difference = compact.get("systemMissingFDates")
    if isinstance(difference, dict):
        for key in ("databaseSnapshot", "databaseSnapshotSha256", "sourceDetail",
                    "sourceDetailSha256", "template", "templateSha256"):
            difference.pop(key, None)
    # Detailed provenance belongs in the linked complete result, not every AI
    # invocation. Keep every business count/status used by the chat renderer.
    google = compact.get("googleChatrDateFallback")
    if isinstance(google, dict):
        compact["googleChatrDateFallback"] = {key: google[key] for key in (
            "enabled", "status", "scope", "source", "record_count", "field_counts",
            "matched_rows", "filled_rows", "chatr_rows", "unmatched_rows",
            "missing_activation_rows", "missing_cutoff_rows", "iccid_conflict_count",
            "iccid_conflict_sheet", "iccid_conflict_policy", "iccid_conflictsDetailCount",
        ) if key in google}
    inventory = compact.get("systemKoodaSource")
    if isinstance(inventory, dict):
        coverage = inventory.get("exportCoverage") or {}
        compact["systemKoodaSource"] = {key: inventory[key] for key in (
            "source", "sourceKind", "sourceScope", "rows", "paginationAttemptCount", "paginationRecovered", "exportCoverageAttemptCount",
        ) if key in inventory}
        compact["systemKoodaSource"]["exportCoverage"] = {key: coverage[key] for key in (
            "status", "sourceKoodaCount", "matchedCount", "missingCount", "iccidConflictCount",
            "structuralErrorCount", "sourceDuplicateKeyCount", "inventoryDuplicateKeyCount",
            "archivedMatchedCount", "transactionalSnapshotGuaranteed",
        ) if key in coverage}
    if result.get("runId") and result.get("status") in {"success", "failed"}:
        compact["completeResultLocation"] = result.get("summaryPath") or "state.summary"
    return compact


def _launcher_result_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Keep stdout small; the complete machine result remains in summaryPath."""
    if result.get("status") == "failed" and result.get("runId") and result.get("state"):
        keys = (
            "status", "executionMode", "pipelineVersion", "programEntrypoint", "manualAiStepsRequired", "aiRole",
            "taskSelection", "taskAOnly", "taskFEnabledForRun", "taskFSkippedReason", "runId", "serverDate",
            "error", "exportFailure", "taskDPreparationFailure", "fDateImport", "expiredCardProbe", "taskG", "systemMissingFDates", "balanceGroups", "sourceWorkbook", "sourceWorkbookSha256", "finalWorkbook",
            "latestVerifiedWorkbook", "latestVerifiedStage", "partialWorkbookIsFinal", "report", "state", "chatSummaryPath",
            "taskDConfirmedSubmittedCount", "taskDUncertainSubmittedCount", "taskDAutomaticRetryBlocked",
            "taskEConfirmedSubmittedCount", "taskEUncertainSubmittedCount", "taskEAutomaticRetryBlocked",
        )
        compact = {key: result.get(key) for key in keys}
        compact.update({"launcherJsonMode": "compact-high-signal", "completeResultLocation": "state.summary"})
        for task in "BCDEF":
            data = result.get("task" + task) or {}
            compact["task" + task] = {key: data[key] for key in (
                "status", "executed", "reason", "candidateCount", "backendValidCount", "actualWriteCount",
                "independentVerificationPassed", "noWriteGroupsStageVerified", "stageReadbackPendingGroups",
                "swapMetricCandidateCount", "swapNotificationCount", "retentionUniqueCardCount", "carrierCandidateCount", "carrierActualWrite",
                "retentionSummaryContractVersion", "retentionPopulationEnabled", "retentionPopulationStatus", "retentionPopulationReadExecuted",
                "retentionCarrierTransferEnabled", "retentionCarrierTransferStatus",
                "retentionSummary", "notificationSummary", "retentionDeadlineMatchPhoneCount",
                "retentionDeadlineAlertRequired", "background", "backgroundState", "backgroundReport",
            ) + TASK_D_RETENTION_PARTITION_FIELDS if key in data}
        timing = result.get("performance") or {}
        compact["performance"] = {key: timing.get(key) for key in (
            "stages", "pipelineWallSecondsBeforeFinalManifest", "documentProcessSeconds", "documentJoinSeconds", "exportPerformance",
        )}
        failure = result.get("documentFailure") or {}
        compact["documentFailure"] = {key: failure.get(key) for key in ("exitCode", "error", "dispatchReceipt", "log")}
        local_failure = failure.get("localFailure") or {}
        compact["documentFailure"]["failedChecks"] = local_failure.get("failed_checks") or []
        compact["documentFailure"]["verificationReceipt"] = local_failure.get("receipt_path")
        return compact
    if result.get("status") != "success":
        return result

    task_d = result.get("taskD") or {}
    task_e = result.get("taskE") or {}
    task_f = result.get("taskF") or {}
    performance = result.get("performance") or {}
    return {
        "status": result.get("status"),
        "executionMode": result.get("executionMode"),
        "pipelineVersion": result.get("pipelineVersion"),
        "programEntrypoint": result.get("programEntrypoint"),
        "manualAiStepsRequired": result.get("manualAiStepsRequired"),
        "aiRole": result.get("aiRole"),
        "taskSelection": result.get("taskSelection"),
        "taskAOnly": result.get("taskAOnly") is True,
        "taskFEnabledForRun": result.get("taskFEnabledForRun"),
        "taskFSkippedReason": result.get("taskFSkippedReason"),
        "googleChatrDateFallbackEnabled": result.get("googleChatrDateFallbackEnabled"),
        "googleChatrDateFallback": result.get("googleChatrDateFallback") or {},
        "koUnactivatedSheet": {key: (result.get("koUnactivatedSheet") or {}).get(key)
            for key in ("enabled", "status", "scope", "sheet", "row_count", "frozen_at", "f_snapshot_row_count", "excluded_not_in_system_count",
                        "slot_source", "current_slot_filled_count", "current_slot_empty_count", "multiple_current_slot_count")},
        "googleSystemMissingSheet": {key: (result.get("googleSystemMissingSheet") or {}).get(key)
            for key in ("enabled", "status", "scope", "missing_rows", "missing_unique_slots", "activation_highlighted_rows")},
        "chatrDocumentDateSource": result.get("chatrDocumentDateSource"),
        "launcherJsonMode": "compact-high-signal",
        "runId": result.get("runId"),
        "serverDate": result.get("serverDate"),
        "candidateCount": result.get("candidateCount"),
        "unexpiredRestore": result.get("unexpiredRestore"),
        "backendValidCount": result.get("backendValidCount"),
        "actualWriteCount": result.get("actualWriteCount"),
        "independentVerificationPassed": result.get("independentVerificationPassed"),
        "noWriteGroupsStageVerified": result.get("noWriteGroupsStageVerified"),
        "liveWorkbookRefreshCount": result.get("liveWorkbookRefreshCount"),
        "restZoneEntryEnabled": result.get("restZoneEntryEnabled"),
        "restZoneEntryHeldCount": result.get("restZoneEntryHeldCount"),
        "currentZero": result.get("currentZero"),
        "overallReplyRatePct": result.get("overallReplyRatePct"),
        "systemMissingFDates": result.get("systemMissingFDates"),
        "fDateImport": result.get("fDateImport"),
        "expiredCardProbe": result.get("expiredCardProbe"),
        "taskG": ({key: result["taskG"].get(key) for key in (
            "status", "executed", "reason", "threshold", "enteredCount", "restoredCount", "restingCount",
            "alreadyHandledCount", "sourceChangedCount", "detachedCount", "unknownMetricRowCount",
            "sourceKiniExcludedCount", "sourceUnknownOprCount", "liveKiniExcludedCount", "liveUnknownOprCount",
            "byOriginalCarrier", "independentReadbackPassed", "receipt", "independentAudit", "error")}
            if result.get("taskG") else None),
        "scopeComparison": result.get("scopeComparison"),
        "balanceGroups": result.get("balanceGroups") or [],
        "carrierReplyComparison": result.get("carrierReplyComparison"),
        "low20": result.get("low20") or [],
        "low10": result.get("low10") or [],
        "audit": result.get("audit") or {},
        "taskB": result.get("taskB") or {},
        "taskC": result.get("taskC") or {},
        "taskD": {
            key: task_d.get(key)
            for key in (
                "status", "executed", "reason", "swapMetricCandidateCount", "swapNotificationCount",
                "retentionUniqueCardCount", "carrierCandidateCount", "carrierActualWrite",
                "retentionCarrierTransferEnabled", "retentionCarrierTransferStatus",
                "retentionSummary", "notificationSummary", "retentionSummaryContractVersion",
                "retentionPopulationEnabled", "retentionPopulationStatus", "retentionPopulationReadExecuted",
            ) + TASK_D_RETENTION_PARTITION_FIELDS if key in task_d
        },
        "taskE": {
            key: task_e.get(key)
            for key in (
                "status", "executed", "reason", "candidateCount", "submittedCount", "notWrittenCount",
                "matchedNoChangeCount", "retentionDeadlineMatchPhoneCount",
                "retentionDeadlineAlertRequired",
            ) if key in task_e
        },
        "taskF": {
            key: task_f.get(key)
            for key in (
                "status", "executed", "reason", "documentMissingActivationRedRows", "documentRedReviewPassed",
                "background", "mainReportIndependent", "backgroundJob", "backgroundJobSha256",
                "backgroundState", "backgroundReport", "backgroundDirectory", "backgroundPid",
                "statusIsLaunchSnapshot", "incrementalWorkbook", "incrementalReceipt",
                "newActivationImportedCount", "notifyIncrementalWorkbook",
                "documentMissingDateRows", "documentMissingDateIccidCount", "documentMissingCutoffRows",
                "documentCreationFallbackRows", "documentLookupNeededRows", "dateEligibilityMode",
                "inventoryCount", "pendingCount", "onlineLookupTriggered",
                "foundImportedCount", "transientErrorCount", "requestedWorkers",
                "effectiveWorkers", "iccidLibrary", "latestIccidLibrary",
                "deadlineWorkbook", "deadlineExportReceipt", "deadlineRecordCount",
                "deadlineActivationCount", "deadlineMissingDateCount",
                "reportBackfillMode", "readyForUploadCount", "refreshExistingMissingCount",
                "documentExcludedCount", "documentMissingActivationIccidCount", "cloudUploadAttempted",
                "refreshedMissingActivationCount",
                "refreshedBlankRecordCount",
                "koRefreshEnabled", "documentKoRefreshRows", "totalLookupNeededRows",
            ) if key in task_f
        },
        "performance": {
            "stages": performance.get("stages") or [],
            "balanceStatisticsRead": performance.get("balanceStatisticsRead"),
            "pipelineWallSecondsBeforeFinalManifest": performance.get("pipelineWallSecondsBeforeFinalManifest"),
            "exportPerformance": performance.get("exportPerformance") or {},
            "documentProcessSeconds": performance.get("documentProcessSeconds"),
            "documentJoinSeconds": performance.get("documentJoinSeconds"),
            "slowestStages": performance.get("slowestStages") or [],
            "acquireSubtasks": performance.get("acquireSubtasks") or {},
            "acquireTransport": performance.get("acquireTransport"),
            "playwrightBrowserConnectionUsed": performance.get("playwrightBrowserConnectionUsed") is True,
        },
        "sourceWorkbook": result.get("sourceWorkbook"),
        "systemKoodaSource": result.get("systemKoodaSource"),
        "finalWorkbook": result.get("finalWorkbook"),
        "report": result.get("report"),
        "portSubset": result.get("portSubset"),
        "vGroupSwapDaily": ({key: value for key, value in result["vGroupSwapDaily"].items()
                             if key not in {"journal", "rows"}}
                            if result.get("vGroupSwapDaily") else None),
        "state": result.get("state"),
        "summaryPath": result.get("summaryPath"),
        "chatSummaryPath": result.get("chatSummaryPath"),
    }


def read_status(state_path: Path) -> dict[str, Any]:
    state = read_json(state_path)
    pid = state.get("runnerPid")
    process_alive = None
    if isinstance(pid, int) and pid > 0:
        checked = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
        process_alive = checked.returncode == 0 and any(
            "opensms_patrol_runner.py " + command in checked.stdout for command in ("run", "finish-local"))
    tasks = {}
    for name in ("taskB", "taskC", "taskD", "taskE", "taskF", "taskG"):
        task = state.get("stages", {}).get(name) or {}
        tasks[name] = {key: task[key] for key in ("executed", "status", "reason", "actualWrite", "carrierActualWrite", "submittedCount", "pendingCount", "foundImportedCount",
            "enteredCount", "restoredCount", "restingCount", "independentReadbackPassed") if key in task}
    recorded_status = state.get("status")
    interrupted = recorded_status in {"running", "preparing"} and process_alive is False
    checkpoint_writes = sum(int(row.get("actualWrite") or 0) for row in state.get("writeGroups", {}).values())
    g = state.get("stages", {}).get("taskG") or {}
    checkpoint_writes += int(g.get("enteredCount") or 0) + int(g.get("restoredCount") or 0)
    return {
        "status": "interrupted" if interrupted else recorded_status, "recordedStatus": recorded_status,
        "runId": state.get("runId"),
        "taskSelection": state.get("taskSelection", "default"), "taskAOnly": state.get("taskAOnly") is True,
        "readOnly": True, "processAlive": process_alive,
        "lastUpdatedAt": state.get("updatedAt"),
        "completedStages": list(state.get("stages", {})),
        # A killed runner may have submitted a write before saving its group.
        # Do not turn an incomplete checkpoint into a false zero or completion.
        "actualCarrierWrites": None if interrupted else checkpoint_writes,
        "checkpointCarrierWrites": checkpoint_writes,
        "writesRequireReceiptReconciliation": interrupted,
        "tasks": tasks, "error": state.get("error") or (
            "主进程已退出，断点未记录终态；写入数量需核对本轮回执，禁止自动重放" if interrupted else None),
        "report": (state.get("summary") or {}).get("report"),
        "state": str(state_path.resolve()),
    }


def refresh_failure_report(state_path: Path) -> dict[str, Any]:
    state_path = state_path.resolve()
    state = read_json(state_path)
    if state.get("status") != "failed" or read_status(state_path)["processAlive"]:
        raise PatrolError("仅允许已停止的失败断点进行本地报告刷新；禁止启动或重试业务")
    delivery = Path(state["delivery"]).resolve()
    if state_path.parent != delivery / "receipts":
        raise PatrolError("失败断点必须位于本轮交付receipts目录")
    # Intentionally bypass construction: __init__ configures a live patrol.
    # This code only reads existing evidence and writes local presentation files.
    view = object.__new__(PatrolRunner)
    view.state, view.state_path = state, state_path
    view.run_id, view.delivery, view.receipts = state["runId"], delivery, delivery / "receipts"
    view.authorization = state.get("authorization") or {}
    result = view.write_failure_report(PatrolError(state.get("error") or "失败断点未记录原因"))
    state["summary"] = result
    state["reportRefreshedAt"] = datetime.now(TZ).isoformat()
    atomic_json(state_path, state)
    from opensms_make_delivery_manifest import build_manifest
    manifest = build_manifest(delivery)
    if manifest["verification_failures"]:
        raise PatrolError(f"本地失败报告清单回读失败: {manifest['verification_failures']}")
    return {**result, "localReportRefreshOnly": True, "businessTasksStarted": False,
            "manifestVerification": manifest}


def local_finalization_checkpoint(state_path: Path) -> dict[str, Any]:
    """Fail closed unless only final local audit/reporting remains."""
    state_path = state_path.resolve()
    state = read_json(state_path)
    run_id = str(state.get("runId") or "")
    delivery = Path(state.get("delivery") or "").resolve()
    if (not re.fullmatch(r"\d{8}T\d{6}\+0800", run_id)
            or delivery.name != run_id or state_path.parent != delivery / "receipts"
            or state_path.name != f"opensms_patrol_runner_state_{run_id}.json"):
        raise PatrolError("本地收尾断点路径或运行编号不匹配")
    if state.get("status") != "failed" or read_status(state_path)["processAlive"]:
        raise PatrolError("本地收尾仅允许已停止的失败断点")
    g_verified_continuation = bool(
        state.get("taskGContinuationAttempts")
        and state["taskGContinuationAttempts"][-1].get("authorizationSource") == "current-user"
        and (state["taskGContinuationAttempts"][-1].get("preflight") or {}).get("runId") == run_id
        and (state.get("stages", {}).get("taskG") or {}).get("status") == "passed"
        and (state.get("stages", {}).get("taskG") or {}).get("independentReadbackPassed") is True
    )
    d_attempt = (state.get("taskDContinuationAttempts") or [{}])[-1]
    d_verified_continuation = bool(
        d_attempt.get("authorizationSource") == "current-user"
        and (d_attempt.get("preflight") or {}).get("businessCompleted") is True
        and (d_attempt.get("preflight") or {}).get("serverDate") == (state.get("stages", {}).get("acquire") or {}).get("serverDate")
        and (state.get("stages", {}).get("taskD") or {}).get("status") == "passed"
        and (state.get("stages", {}).get("taskG") or {}).get("status") == "passed"
        and (state.get("stages", {}).get("taskG") or {}).get("independentReadbackPassed") is True
    )
    verified_continuation = g_verified_continuation or d_verified_continuation
    continuation_local_failure = (verified_continuation
        and state.get("error") == "既有差异导出未完整核验，禁止覆盖或复用")
    if not str(state.get("error") or "").startswith("最终并行审计失败:") and not continuation_local_failure:
        raise PatrolError("断点不是最终本地审计失败，禁止跳过未完成业务")
    if ((state.get("apiTarget") or {}).get("status") != "closed"
            or state.get("apiTargetCleanupError")):
        raise PatrolError("后台操作页未确认关闭，禁止本地收尾")
    stages = state.get("stages") or {}
    for name in ("acquire", "candidates", "document", "standardWrites", "refresh"):
        if not (stages.get(name) or {}).get("completedAt"):
            raise PatrolError(f"本地收尾缺少已完成阶段: {name}")
    authorization = state.get("authorization") or {}
    for task in ("B", "C", "D", "E"):
        if authorization.get("task" + task) and not (stages.get("task" + task) or {}).get("completedAt"):
            raise PatrolError(f"任务{task}未完成，禁止本地收尾")
    if authorization.get("taskD"):
        task_d = stages["taskD"]
        if task_d.get("status") != "passed" or (task_d.get("notificationSummary") or {}).get("status") != "passed":
            raise PatrolError("任务D回执或通知统计未通过")
        from opensms_task_d_notification_summary import build_from_files
        confirmed = build_from_files(run_id, task_d["manifest"],
                                     task_d["swapExpiryReceipt"], task_d["retentionExpiryReceipt"])
        if any(confirmed[key] != task_d["notificationSummary"].get(key) for key in
               ("confirmedSubmissionCount", "uniqueNotifiedPhoneCount", "overlapPhoneCount")):
            raise PatrolError("任务D事务回执统计不一致，禁止本地收尾")
    if authorization.get("taskE"):
        task_e = stages["taskE"]
        if task_e.get("status") not in ("passed", "passed_with_not_found", "no_candidate"):
            raise PatrolError("任务E结果不明，禁止本地收尾")
        if task_e.get("executed") and task_e.get("ledgerVerificationPassed") is not True:
            raise PatrolError("任务E台账未核验，禁止本地收尾")
    task_g = stages.get("taskG") or {}
    if task_g.get("executed") and (task_g.get("status") != "passed" or task_g.get("independentReadbackPassed") is not True):
        raise PatrolError("任务G独立回读未通过，禁止本地收尾")
    if task_g.get("executed"):
        from opensms_task_g import audit
        if audit(task_g["input"], task_g["receipt"]) != read_json(task_g["independentAudit"]):
            raise PatrolError("任务G独立回执重建不一致，禁止本地收尾")
    imported = state.get("fDateImport") or {}
    d_import_not_submitted = False
    if d_verified_continuation and imported.get("receipt"):
        receipt = Path(imported["receipt"]).resolve()
        attempt_dir = Path(d_attempt.get("attemptDirectory") or "").resolve()
        d_import_not_submitted = (receipt.parent == attempt_dir
            and attempt_dir.parent == delivery / "receipts" / "task_d_continuation_attempts"
            and re.fullmatch(r"[0-9]+", attempt_dir.name) is not None
            and receipt.name == f"opensms_f_date_import_{run_id}.json"
            and receipt.is_file() and read_json(receipt) == imported
            and imported.get("runId") == run_id and imported.get("cloudUploadAttempted") is False
            and imported.get("error") == "F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED"
            and not Path(str(receipt) + ".intent.json").exists())
    import_not_submitted_warning = bool(
        (g_verified_continuation or d_import_not_submitted) and imported.get("status") == "failed_before_submission"
        and imported.get("submitted") is False
        and not (delivery / "receipts" / f"opensms_f_date_import_{run_id}.json.intent.json").exists()
    )
    # Zero-row current-run difference exports are a normal completed no-submit
    # outcome. Bind their immutable receipt and output instead of loosening any
    # submitted/uncertain import guard or generating a historical replacement.
    no_new_import = False
    if (imported.get("status") == "skipped_no_new_data" and imported.get("runId") == run_id
            and imported.get("submitted") is False):
        difference = state.get("systemMissingFDates") or {}
        receipt = Path(difference.get("receipt") or "").resolve()
        allowed_parent = receipt.parent == delivery / "receipts"
        for key, folder in (("taskDContinuationAttempts", "task_d_continuation_attempts"),
                            ("taskGContinuationAttempts", "task_g_continuation_attempts")):
            for attempt in state.get(key) or []:
                directory = Path(attempt.get("attemptDirectory") or "").resolve()
                allowed_parent = allowed_parent or (attempt.get("authorizationSource") == "current-user"
                    and directory.parent == delivery / "receipts" / folder
                    and re.fullmatch(r"[0-9]+", directory.name) is not None and receipt.parent == directory)
        output = Path(difference.get("output") or "").resolve()
        no_new_import = (allowed_parent and receipt.name == f"opensms_system_missing_f_dates_{run_id}.json"
            and receipt.is_file() and read_json(receipt) == difference
            and difference.get("runId") == run_id and difference.get("status") == "passed"
            and type(difference.get("exportedRowCount")) is int and difference["exportedRowCount"] == 0
            and type(difference.get("rowCount")) is int and difference["rowCount"] == 0
            and difference.get("independentReadbackPassed") is True
            and difference.get("cloudUploadAttempted") is False
            and difference.get("sourceSha256") == stages["acquire"].get("sourceSha256")
            and output.parent == delivery and run_id in output.name and output.is_file()
            and sha256(output) == difference.get("sha256")
            and not (receipt.parent / f"opensms_f_date_import_{run_id}.json.intent.json").exists())
    if imported.get("status") not in ("passed", "skipped") and not import_not_submitted_warning and not no_new_import:
        raise PatrolError("F日期导入阶段未完成，禁止本地收尾")
    if imported.get("submitted") and (imported.get("runId") != run_id
            or imported.get("status") != "passed" or imported.get("independentReadbackPassed") is not True):
        raise PatrolError("F日期导入结果不明，禁止本地收尾")
    for group in (state.get("writeGroups") or {}).values():
        if group.get("result") not in ("passed", "no_candidate"):
            raise PatrolError("存在未确认流转组，禁止本地收尾")
    for stage, path_key, hash_key in (("acquire", "sourceArchive", "sourceSha256"),
                                      ("refresh", "finalWorkbook", "finalWorkbookSha256")):
        evidence = stages[stage]
        path = Path(evidence.get(path_key) or "")
        if not path.is_file() or not evidence.get(hash_key) or sha256(path) != evidence[hash_key]:
            raise PatrolError(f"本地收尾源文件完整性未通过: {stage}")
    return state


def finish_local_report(state_path: Path, authorization_source: str) -> dict[str, Any]:
    """Reuse the normal finalizer; never construct a live runner or site lease."""
    if authorization_source != "current-user":
        raise PatrolError("本地收尾必须有当轮用户继续授权")
    state_path = state_path.resolve()
    # A private local lock protects this state without opening a browser/site.
    # The delivery manifest intentionally excludes its .~ temporary files.
    lock = state_path.parent / ".~finalization_only.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        state = local_finalization_checkpoint(state_path)
        view = object.__new__(PatrolRunner)
        view.args = build_parser().parse_args(["run", "--json"])
        view.state, view.state_path = state, state_path
        view.run_id = state["runId"]
        view.day, view.delivery = view.run_id[:8], Path(state["delivery"]).resolve()
        view.receipts, view.run_dir = view.delivery / "receipts", RUN_LOG_ROOT / view.run_id
        view.run_dir.mkdir(parents=True, exist_ok=True)
        view.authorization = state["authorization"]
        view.api_target_id, view.failure_result = None, None
        view.started_monotonic = time.monotonic()
        attempt = view.receipts / "finalization_attempts" / str(time.time_ns())
        attempt.mkdir(parents=True, exist_ok=False)
        archive = []
        old_files = [state_path]
        for pattern in (f"opensms_parallel_audit_{view.run_id}_*.json",
                        f"opensms_patrol_finalize_{view.run_id}_config.json",
                        f"opensms_report_generation_{view.run_id}.json"):
            old_files.extend(view.receipts.glob(pattern))
        old_files.extend(view.delivery.glob(f"OpenSMS*{view.run_id}.md"))
        old_files.extend(path for path in (view.run_dir / (label + ".log") for label in
            ("finalize_report", "audit_initial", "audit_final", "delivery_manifest", "delivery_manifest_complete")) if path.is_file())
        for index, path in enumerate(old_files):
            target = attempt / f"{index:03d}_{path.name}"
            shutil.copy2(path, target)
            digest = sha256(path)
            if sha256(target) != digest:
                raise PatrolError("原失败收尾证据归档回读失败")
            archive.append({"source": str(path), "archive": str(target), "sha256": digest})
        history = {"startedAt": now_hkt().isoformat(), "previousError": state["error"],
                   "archive": archive, "localOnly": True, "businessTasksReplayed": False}
        state.setdefault("localFinalizationHistory", []).append(history)
        state.update(status="running", runnerPid=os.getpid(), localFinalizationOnly=True)
        view._save_state()
        try:
            stages = state["stages"]
            acquire, candidates, document, refresh = (stages[name] for name in ("acquire", "candidates", "document", "refresh"))
            before_g = stages.get("refreshBeforeTaskGContinuation") or stages.get("refreshBeforeTaskG")
            if before_g:
                document = view.refreshed_document(document, before_g)
            final = view.finalize(acquire, candidates, document, refresh)
            result = view.summary(acquire, candidates, refresh, final)
            result.update(localFinalizationOnly=True, businessTasksReplayed=False)
            history.update(status="success", completedAt=now_hkt().isoformat())
            state.update(status="success", summary=result)
            state.pop("error", None)
            view._save_state()
            view.command("delivery_manifest_complete", [PYTHON, SCRIPTS / "opensms_make_delivery_manifest.py", view.delivery])
            view.release_expired_card_probe()
            return result
        except Exception as exc:
            history.update(status="failed", error=str(exc), completedAt=now_hkt().isoformat())
            state.update(status="failed", error=str(exc))
            state["summary"] = view.write_failure_report(exc)
            view._save_state()
            raise
    finally:
        os.close(descriptor)
        lock.unlink()


def task_d_continuation_checkpoint(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Local-only preflight; repeat after acquiring the original shared locks."""
    if (not args.resume_state or args.authorization_source != "current-user"
            or any(getattr(args, flag, False) for flag in (
                "continue_task_g", "task_a_only", "restore_unexpired_to_a11", "force_task_f",
                "task_f_only", "task_f_sample_size", "task_f_repair_lookup", "task_f_debug_iccid",
                "task_e_recovery_progress"))):
        raise PatrolError("TASK_D_CONTINUATION_AUTHORIZATION_INVALID")
    state = read_json(args.resume_state)
    business_complete = (state.get("taskDCheckpoint") or {}).get("businessCompleted") is True
    attempts = state.get("taskDContinuationAttempts") or []
    run_id, delivery = state.get("runId", ""), Path(state.get("delivery") or "").resolve()
    state_path = Path(args.resume_state).resolve()
    if (not re.fullmatch(r"\d{8}T\d{6}\+0800", run_id) or delivery.name != run_id
            or state_path != delivery / "receipts" / f"opensms_patrol_runner_state_{run_id}.json"
            or state.get("status") != "failed" or read_status(state_path)["processAlive"]
            or state.get("taskGContinuationAttempts")
            or (attempts and (not business_complete or attempts[-1].get("status") != "failed"
                or state.get("error") != "任务D通知统计：通知或事务回执不属于本轮目录/运行编号"))
            or state.get("authorization") != validate_authorization(args)
            or not all(state["authorization"].get("task" + name) for name in "BCDE")
            or (state.get("apiTarget") or {}).get("status") != "closed" or state.get("apiTargetCleanupError")):
        raise PatrolError("TASK_D_CONTINUATION_CHECKPOINT_INVALID")
    stages = state.get("stages") or {}
    required = {"acquire", "candidates", "document", "standardWrites", "taskB", "taskC",
                "taskF", "refreshAfterStandard", "refreshAfterTaskB", "refreshAfterTaskC"}
    if set(stages) != required or any(not stages[name].get("completedAt") for name in required):
        raise PatrolError("TASK_D_CONTINUATION_PRIOR_STAGES_INVALID")
    acquired, refreshed = stages["acquire"], stages["refreshAfterTaskC"]
    if (acquired.get("serverDateVerified") is not True
            or sha256(Path(acquired["sourceArchive"])) != acquired["sourceSha256"]
            or sha256(Path(acquired["processingSource"])) != acquired["sourceSha256"]
            or sha256(Path(refreshed["finalWorkbook"])) != refreshed["finalWorkbookSha256"]
            or read_json(refreshed["finalStateReceipt"]).get("passed") is not True):
        raise PatrolError("TASK_D_CONTINUATION_SOURCE_OR_READBACK_CHANGED")
    for group in state.get("writeGroups", {}).values():
        if group.get("result") not in ("passed", "no_candidate"):
            raise PatrolError("TASK_D_CONTINUATION_PRIOR_WRITE_UNCONFIRMED")
        if group.get("result") == "passed":
            if group.get("noWrite") is True:
                if group.get("stageReadbackVerified") is not True:
                    raise PatrolError("TASK_D_CONTINUATION_PRIOR_NO_WRITE_UNVERIFIED")
            elif read_json(group["verify"]).get("passed") is not True:
                raise PatrolError("TASK_D_CONTINUATION_PRIOR_WRITE_UNCONFIRMED")
            for field in ("validated", "write"):
                if not Path(group[field]).is_file():
                    raise PatrolError("TASK_D_CONTINUATION_PRIOR_WRITE_EVIDENCE_MISSING")
    checkpoint = state.get("taskDCheckpoint") or {}
    if getattr(args, 'skip_data_anomalies_this_run', False):
        if (checkpoint or state.get('taskDContinuationAttempts') or state.get('fDateImport')
                or (STATE_ROOT / 'f_date_import' / 'pending.json').exists()
                or any(state_path.parent.glob(f'opensms_task_d_*_{run_id}_progress.json'))
                or any(state_path.parent.glob(f'opensms_task_d_swap_zero_{run_id}_receipt.json'))
                or any(state_path.parent.glob(f'opensms_task_d_retention_verified_{run_id}_receipt.json'))
                or any(state_path.parent.glob(f'opensms_task_d_{run_id}_manifest.json'))):
            raise PatrolError('TASK_D_PREPARATION_CONTINUATION_NOT_ZERO_SUBMISSION')
        raw_path = state_path.parent / f'opensms_task_d_filtered_queries_{run_id}.json'
        failure = state.get('taskDPreparationFailure') or {}
        log = RUN_LOG_ROOT / run_id / 'task_d_prepare.log'
        if failure.get('log') != str(log) or not log.is_file() or not raw_path.is_file():
            raise PatrolError('TASK_D_PREPARATION_CONTINUATION_EVIDENCE_MISSING')
        before = {path: sha256(path) for path in (state_path, raw_path, log)}
        checked = subprocess.run([str(NODE), str(SCRIPTS / 'opensms_task_d_preparation_continuation.mjs'),
            str(state_path), str(raw_path)], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
        if checked.returncode != 0:
            raise PatrolError('TASK_D_PREPARATION_CONTINUATION_RAW_PROOF_REJECTED')
        proof = parse_last_json(checked.stdout)
        if proof.get('status') != 'passed' or any(sha256(path) != digest for path, digest in before.items()):
            raise PatrolError('TASK_D_PREPARATION_CONTINUATION_EVIDENCE_CHANGED')
        files = [state_path, raw_path, log, Path(acquired['sourceArchive']), Path(acquired['processingSource']),
            Path(refreshed['finalWorkbook']), Path(refreshed['finalStateReceipt'])]
        return state, {**proof, 'serverDate': acquired['serverDate'],
            'currentUserAnomalySkipAuthorized': True,
            'files': [{'path': str(file), 'sha256': sha256(file)} for file in dict.fromkeys(files)]}
    manifest_path = Path(checkpoint.get("manifest") or "").resolve()
    manifest = read_json(manifest_path)
    progress_path = state_path.parent / f"opensms_task_d_retention_verified_{run_id}_progress.json"
    progress = read_json(progress_path)
    if (not manifest_path.is_relative_to(state_path.parent) or manifest.get("runId") != run_id
            or manifest.get("retentionCarrierTransferEnabled") is not False
            or checkpoint.get("carrierRoutes") != []
            or progress.get("status") != "failed" or progress.get("uncertainSubmission") is not True
            or progress.get("phase") != "retention_verified" or progress.get("serverDate") != acquired["serverDate"]
            or not re.search(r"POST /phone/config/batchSetChatExpire HTTP 502[；; ]", progress.get("error", ""))
            or Path(progress.get("manifestPath") or "").resolve() != Path(manifest["retentionNotificationPath"]).resolve()
            or Path(str(progress_path) + ".lock").exists()
            or (state_path.parent / f"opensms_task_d_retention_verified_{run_id}_receipt.json").exists()):
        raise PatrolError("TASK_D_CONTINUATION_PENDING_SCOPE_INVALID")
    swap_path = Path(checkpoint["swapExpiryReceipt"])
    swap = read_json(swap_path)
    swap_manifest = read_json(manifest["swapNotificationPath"])
    cards = []
    for batch in swap.get("batches", []):
        data = (batch.get("serverResponse") or {}).get("data") or {}
        size = len(batch.get("cardNos", []))
        if (not size or batch.get("businessReceiptPassed") is not True
                or batch.get("transactionalVerificationPassed") is not True
                or (batch.get("serverResponse") or {}).get("code") != 200
                or data.get("inputCount") != size or data.get("matchedCardCount") != size
                or type(data.get("updatedChatCount")) is not int or data["updatedChatCount"] < 0
                or data.get("notFound") != [] or batch.get("expireDate") != acquired["serverDate"]):
            raise PatrolError("TASK_D_CONTINUATION_SWAP_RECEIPT_INVALID")
        cards.extend(batch["cardNos"])
    if (swap.get("status") not in ("passed", "no_candidate") or swap.get("businessReceiptPassed") is not True
            or cards != swap_manifest.get("cardNos") or len(set(cards)) != len(cards)):
        raise PatrolError("TASK_D_CONTINUATION_SWAP_RECEIPT_INVALID")
    files = [state_path, manifest_path, progress_path, swap_path,
        Path(manifest["retentionNotificationPath"]), Path(manifest["retentionReceiptPath"]),
        Path(manifest["swapNotificationPath"])]
    if business_complete:
        from opensms_task_d_notification_summary import build_from_files
        # Recompute all confirmed transactions before allowing local-only D
        # reconciliation. No write endpoint is called in this path.
        build_from_files(run_id, manifest_path, swap_path, checkpoint["retentionExpiryReceipt"])
        files.append(Path(checkpoint["retentionExpiryReceipt"]))
    return state, {"progress": str(progress_path), "progressSha256": sha256(progress_path),
        "businessCompleted": business_complete,
        "serverDate": acquired["serverDate"], "exportReplayed": False,
        "completedBusinessStagesReplayed": False,
        "files": [{"path": str(file), "sha256": sha256(file)} for file in files]}


def prepare_task_d_continuation(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    state, proof = task_d_continuation_checkpoint(args)
    delivery = Path(state["delivery"])
    attempt = delivery / "receipts" / "task_d_continuation_attempts" / str(time.time_ns())
    attempt.mkdir(parents=True, exist_ok=False)
    files = [Path(item["path"]) for item in proof["files"]]
    files.extend(delivery.glob(f"OpenSMS*{state['runId']}.md"))
    files.extend(file for file in (delivery / "FILES.tsv", delivery / "SHA256SUMS.txt") if file.is_file())
    files.extend((RUN_LOG_ROOT / state["runId"]).glob("*.log"))
    archive = []
    for index, file in enumerate(files):
        target = attempt / f"{index:03d}_previous_{file.name}"
        digest = sha256(file)
        shutil.copy2(file, target)
        if sha256(target) != digest or sha256(file) != digest:
            raise PatrolError("TASK_D_CONTINUATION_ARCHIVE_CHANGED")
        archive.append({"source": str(file), "archive": str(target), "sha256": digest})
    history = {"startedAt": now_hkt().isoformat(), "status": "authorized", "authorizationSource": "current-user",
        "previousError": state["error"], "attemptDirectory": str(attempt), "archive": archive, "preflight": proof,
        "exportReplayed": False, "completedBusinessStagesReplayed": False}
    state.setdefault("taskDContinuationAttempts", []).append(history)
    atomic_json(attempt / "continuation_preflight.json", history)
    return state, attempt


def task_g_continuation_checkpoint(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate locally before any browser access, then again under both leases."""
    if (not getattr(args, "resume_state", None) or args.authorization_source != "current-user"
            or any(getattr(args, flag, False) for flag in (
                "force_task_b", "force_task_c", "force_task_d", "force_task_e", "force_task_f",
                "task_a_only", "restore_unexpired_to_a11", "task_f_only", "task_f_sample_size",
                "task_f_repair_lookup", "task_f_debug_iccid", "task_e_recovery_progress"))):
        raise PatrolError("任务G续接仅接受当轮用户授权和精确原断点，不得混入其它任务")
    from opensms_task_g_continuation import validate_checkpoint
    state = read_json(args.resume_state)
    try:
        proof = validate_checkpoint(args.resume_state, state,
            process_alive=read_status(args.resume_state)["processAlive"],
            settings_path=SCRIPTS / "opensms_task_g_settings.json",
            ledger_path=STATE_ROOT / "task_g_weekend_rest/ledger.json")
    except Exception as exc:
        raise PatrolError(str(exc)) from exc
    return state, proof


def prepare_task_g_continuation(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    state, proof = task_g_continuation_checkpoint(args)
    delivery = Path(state["delivery"])
    attempt = delivery / "receipts" / "task_g_continuation_attempts" / str(time.time_ns())
    attempt.mkdir(parents=True, exist_ok=False)
    old_files = [Path(args.resume_state)]
    g = state["stages"]["taskG"]
    old_files.extend(Path(g[field]) for field in ("input", "receipt"))
    old_files.extend(delivery.glob(f"OpenSMS*{state['runId']}.md"))
    logs = RUN_LOG_ROOT / state["runId"]
    old_files.extend(path for path in (logs / (label + ".log") for label in (
        "task_g_prepare", "task_g_execute", "delivery_manifest_failed")) if path.is_file())
    archive = []
    for index, file in enumerate(old_files):
        target = attempt / f"{index:03d}_previous_{file.name}"
        digest = sha256(file)
        shutil.copy2(file, target)
        if sha256(target) != digest or sha256(file) != digest:
            raise PatrolError("任务G原失败证据归档核验失败")
        archive.append({"source": str(file), "archive": str(target), "sha256": digest})
    history = {"startedAt": now_hkt().isoformat(), "status": "authorized",
        "authorizationSource": "current-user", "attemptDirectory": str(attempt),
        "previousError": state["error"], "archive": archive, "preflight": proof,
        "exportReplayed": False, "completedBusinessStagesReplayed": False,
        "fImportReplayed": False, "previousTaskG": dict(g)}
    state.setdefault("taskGContinuationAttempts", []).append(history)
    atomic_json(attempt / "continuation_preflight.json", history)
    return state, attempt


def main() -> int:
    args = build_parser().parse_args()
    runner: PatrolRunner | None = None
    site_lease = None
    report_lease = None
    try:
        if args.command == "check":
            result = static_check()
        elif args.command == "status":
            result = read_status(args.state)
        elif args.command == "refresh-failure-report":
            result = refresh_failure_report(args.state)
        elif args.command == "finish-local":
            result = finish_local_report(args.state, args.authorization_source)
        else:
            validate_task_selection(args)
            validate_authorization(args)
            if args.continue_task_d:
                task_d_continuation_checkpoint(args)
            if args.continue_task_g:
                task_g_continuation_checkpoint(args)
            if REQUEST_LOCK.exists():
                raise PatrolError(f"OPENSMS_PACING_REQUEST_UNCONFIRMED: 遗留请求锁需要确认清理，未启动导出: {REQUEST_LOCK}")
            browser_session = ensure_configured_browser()
            if args.cdp and args.cdp != browser_session["cdp"]:
                raise PatrolError("OPENSMS_NEW_BROWSER_ENDPOINT_MISMATCH: 不允许替换新浏览器会话")
            args.cdp = browser_session["cdp"]
            args.browser_session = browser_session
            # Whole jobs serialize across windows before creating browser pages.
            # Status/check/local report refresh do not acquire a live-site lease.
            site_lease = SiteExecutionLease(on_wait=lambda: print(json.dumps({
                "event": "site_execution_waiting", "onlineStarted": False,
                "message": "其它窗口占用430网站，等待共享任务锁",
            }, ensure_ascii=False), file=sys.stderr, flush=True)).acquire()
            # Several reports may share the manual-login browser, but only one
            # may own its complete export/write/readback workflow at a time.
            report_lease = SiteExecutionLease(file=REPORT_LOCK, on_wait=lambda: print(json.dumps({
                "event":"site_execution_waiting", "onlineStarted":False,
                "message":"另一个导出正在使用新浏览器，等待整轮锁",
            }, ensure_ascii=False), file=sys.stderr, flush=True)).acquire()
            # Rebind after waiting: an idle manual-login service can transition
            # to headless, but never while our whole-job locks are held.
            browser_session = resolve_configured_browser()
            if browser_session["status"] != "ready":
                raise PatrolError("OPENSMS_NEW_BROWSER_LOGIN_REQUIRED: 原浏览器登录未确认")
            if args.cdp != browser_session["cdp"]:
                raise PatrolError("OPENSMS_NEW_BROWSER_SESSION_CHANGED: 会话在等待期间改变，未启动业务")
            args.browser_session = browser_session
            if REQUEST_LOCK.exists():
                # The old request lane cannot be stolen merely because its PID
                # died. Preserve it for ownership/HTTP-completion confirmation.
                raise PatrolError(f"OPENSMS_PACING_REQUEST_UNCONFIRMED: 遗留请求锁需要确认清理，未启动导出: {REQUEST_LOCK}")
            state = read_json(args.resume_state) if args.resume_state else None
            if state and (state.get("browser") or {}).get("sessionId") != browser_session["sessionId"]:
                raise PatrolError("OPENSMS_BROWSER_RESUME_IDENTITY_MISMATCH: 不在新浏览器重放旧会话断点，请保留原回执")
            if args.continue_task_g:
                state, attempt = prepare_task_g_continuation(args)
                # Preserve the original disabled task authorization unchanged.
                # The current-user continuation permission is recorded separately.
                args.authorization_source = state["authorization"].get("authorizationSource")
            if args.continue_task_d:
                state, attempt = prepare_task_d_continuation(args)
            runner = PatrolRunner(args, state)
            if args.continue_task_g or args.continue_task_d:
                runner.receipts = attempt
                runner.state["status"] = "running"
            runner.site_execution_environment = site_lease.environment
            runner.site_execution_environment.update({
                "OPENSMS_SITE_JOB_LOCK_TOKEN": report_lease.owner["token"],
                "OPENSMS_SITE_JOB_LOCK_PID": str(report_lease.owner["pid"]),
            })
            runner.state["siteExecution"] = {"exclusive": True, "file": str(site_lease.file),
                                             "ownerPid": site_lease.owner["pid"], "crossWindow": True,
                                             "inheritedBrowserSession": site_lease.inherited,
                                             "reportLock":str(report_lease.file), "reportOwnerPid":os.getpid()}
            runner._save_state()
            # Publish exact monitor ownership without changing the final JSON
            # stdout contract or asking the caller to guess the newest folder.
            print(json.dumps({
                "event": "patrol_started", "runId": runner.run_id,
                "state": str(runner.state_path), "runnerPid": os.getpid(),
                "taskSelection": "A" if runner.task_a_only else "default",
                "taskAOnly": runner.task_a_only,
                "taskFProgressIntervalSeconds": 0 if runner.task_a_only else 300,
            }, ensure_ascii=False), file=sys.stderr, flush=True)
            result = runner.execute()
        output = compact_launcher_result(result) if args.command in ("run", "finish-local") and getattr(args, "json", False) else result
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":") if getattr(args, "json", False) else None, indent=None if getattr(args, "json", False) else 2))
        return 0
    except Exception as exc:
        payload = runner.failure_result if runner and runner.failure_result else {"status": "failed", "error": str(exc)}
        if args.command == "run" and getattr(args, "task_a_only", False) and runner is None:
            payload.update(execution_contract(True))
            payload["taskF"] = PatrolRunner.task_a_excluded_stage()
        output = compact_launcher_result(payload) if args.command == "run" and getattr(args, "json", False) else payload
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2
    finally:
        if report_lease is not None:
            cleanup_failed = runner and ((runner.state.get("apiTarget") or {}).get("status") == "cleanup_failed"
                                         or runner.state.get("apiTargetCleanupError"))
            if not cleanup_failed and not REQUEST_LOCK.exists():
                report_lease.release()
            elif runner is None:
                report_lease.release()
        if site_lease is not None:
            cleanup_failed = runner and ((runner.state.get("apiTarget") or {}).get("status") == "cleanup_failed"
                                         or runner.state.get("apiTargetCleanupError"))
            if not cleanup_failed and not REQUEST_LOCK.exists():
                site_lease.release()
            elif runner is None:
                # No browser work started here; preserve the separate lane.
                site_lease.release()
        # A headless browser cannot display a manual login window. Only the
        # confirmed-expiry flag from this exact session permits this UI action,
        # after our report/page/request ownership has been safely released.
        if (args.command == "run" and not REQUEST_LOCK.exists()
                and not (runner and (runner.state.get("apiTargetCleanupError")
                    or (runner.state.get("apiTarget") or {}).get("status") == "cleanup_failed"))):
            try:
                login_notice = show_pending_manual_login(getattr(args,"browser_session",None))
                if login_notice:
                    print(json.dumps(login_notice,ensure_ascii=False),file=sys.stderr,flush=True)
            except Exception:
                print(json.dumps({"event":"manual_login_window","status":"unconfirmed",
                                  "businessRestarted":False},ensure_ascii=False),file=sys.stderr,flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
