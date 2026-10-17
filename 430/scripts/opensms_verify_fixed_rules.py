#!/usr/bin/env python3
"""Bounded, offline A-H regression; never starts a live patrol.

Usage: python3 scripts/opensms_verify_fixed_rules.py [--mode quick|full] [--workers 2]
       python3 scripts/opensms_verify_fixed_rules.py --list --json

The existing run_opensms_offline_tests.py remains the legacy broad regression.
This curated entry runs each file once, with disjoint writable sandboxes. Full
adds document/report integration tests; it is not arbitrary unittest discovery.
Requires macOS sandbox-exec and the existing bundled Python/Node dependencies.
There is deliberately no unprotected, network-enabled or browser fallback mode.

Known limitation: on this Mac the system Bash here-document temporary writes
are incompatible with the strict per-group writable sandbox, even with a short
private TMPDIR. Full document integration can therefore fail with
sandbox_heredoc_temp_incompatible. Preserve that failed receipt; do not repeatedly
rerun it or relax isolation. Quick remains the regular fixed-rule acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies')
PYTHON = RUNTIME / 'python/bin/python3'
NODE = RUNTIME / 'node/bin/node'
SANDBOX = Path('/usr/bin/sandbox-exec')
MAX_WORKERS = 4


@dataclass(frozen=True)
class Group:
    name: str
    runtime: str
    files: tuple[str, ...]


def py(name, *files):
    return Group(name, 'python', tuple(f'tests/test_{file}.py' for file in files))


QUICK = (
    py('b14_removed_and_origin_speed', 'opensms_b14_removed', 'opensms_e_history_performance'),
    py('task_g', 'opensms_task_g'),
    Group('task_g_engine', 'node', ('tests/test_opensms_task_g.mjs',)),
    Group('task_h_engine', 'node', ('tests/test_opensms_task_h.mjs',)),
    py('carrier_reply_comparison', 'opensms_carrier_reply_comparison'),
    py('scope_comparison', 'opensms_scope_comparison', 'opensms_device_reply_periods'),
    py('report_only_f', 'opensms_report_only_f'),
    py('ko_refresh', 'opensms_task_f_ko_refresh'),
    py('creation_usage_days', 'opensms_creation_usage_days'),
    py('koo_two_day_field', 'opensms_koo_two_day_field'),
    py('local_performance_contract', 'opensms_local_benchmark', 'opensms_audit_scan_performance', 'opensms_program_performance', 'opensms_diagnose_run', 'opensms_local_jobs', 'opensms_parallel_finalize', 'opensms_cpu_audit'),
    py('v_group_daily_and_compact_report', 'opensms_v_swap_daily'),
    Group('v_group_boundary', 'node', ('tests/test_opensms_v_group_guard.mjs',)),
    py('card_retention_report', 'opensms_card_retention_report'),
    Group('card_retention_readonly', 'node', ('tests/test_opensms_card_retention_report.mjs',)),
    py('device_daily_statistics', 'opensms_device_daily_statistics', 'opensms_room_mapping'),
    Group('silent_protocol_guards', 'node', (
        'tests/test_opensms_new_browser.mjs',
        'tests/test_opensms_shared_browser.mjs',
        'tests/test_opensms_browser_auth_store.mjs',
        'tests/test_opensms_recover_request_lane.mjs',
        'tests/test_opensms_request_pacing.mjs',
        'tests/test_opensms_bounded_status_fetch.mjs',
        'tests/test_opensms_clock_inventory_recovery.mjs',
        'tests/test_opensms_background_startup_recovery.mjs',
        'tests/test_opensms_read_retry.mjs',
        'tests/test_opensms_silent_target.mjs',
        'tests/test_opensms_hidden_viewport.mjs',
        'tests/test_opensms_api_target_guard_cli.mjs',
        'tests/test_opensms_api_target_isolation.mjs',
        'tests/test_opensms_navigation_guard.mjs',
        'tests/test_opensms_export_performance.mjs',
        'tests/test_opensms_export_submission_ready.mjs',
        'tests/test_opensms_payload_codec.mjs',
        'tests/test_opensms_payload_build_recovery.mjs',
        'tests/test_opensms_payload_readiness.mjs',
        'tests/test_opensms_google_download_transport.mjs',
        'tests/test_opensms_login_preflight.mjs')),
    py('chatr_date_fallback', 'opensms_chatr_google_dates', 'opensms_google_room_layout', 'opensms_google_system_missing_sheets', 'opensms_runner_chatr_google', 'opensms_creation_date_fallback', 'opensms_export_port_subset'),
    py('inventory_coverage', 'opensms_inventory_coverage', 'opensms_archived_identity', 'opensms_acquire_reliability', 'opensms_system_kooda_runner', 'opensms_acquire_schedule'),
    py('export_failure_diagnostics', 'opensms_export_failure_diagnostics'),
    py('authorization', 'opensms_browser_session', 'opensms_bitbrowser_430', 'opensms_site_execution', 'opensms_patrol_runner', 'opensms_automatic_recovery_reporting', 'opensms_task_a_only', 'opensms_export_entry',
       'opensms_runner_no_google', 'opensms_task_wrappers'),
    py('routing_abc', 'opensms_generate_a_zone_split', 'opensms_generate_opr_zero_candidates',
       'opensms_generate_task_b_candidates', 'opensms_task_b_retained_audit', 'opensms_task_b_lucky', 'opensms_task_b_all_carriers', 'opensms_generate_task_c_candidates',
       'opensms_set_carrier_candidates_guard', 'opensms_task_c_daily_guard', 'opensms_deadline_routing', 'opensms_parallel_audit'),
    py('task_d', 'opensms_generate_task_d_zero_metrics', 'opensms_task_d_filtered_audit', 'opensms_task_d_preparation_failure',
       'opensms_task_d_retention_summary', 'opensms_task_d_retention_runner',
       'opensms_task_d_retention_reporting', 'opensms_task_d_notification_summary', 'opensms_task_d_report_details', 'opensms_task_d_continuation'),
    py('task_e', 'opensms_generate_task_e_candidates', 'opensms_execute_task_e_expiry', 'opensms_task_e_unwritten_sheet'),
    py('task_f', 'opensms_task_f', 'opensms_task_f_performance',
       'opensms_report_date_cache_delta', 'opensms_task_f_report_backfill',
       'opensms_task_f_background', 'opensms_task_f_report_backfill_audit',
       'opensms_task_f_progress_status', 'opensms_background_f_reporting',
       'opensms_task_f_reconcile_document', 'task_f_deadline_export',
       'opensms_system_missing_f_dates', 'opensms_task_f_template'),
    Group('readonly_concurrency', 'node', (
        'tests/opensms_readonly_concurrency.test.mjs',
        'tests/test_opensms_final_read_concurrency.mjs',
        'tests/opensms_balance_statistics_readonly.test.mjs',
        'tests/test_opensms_task_d_retention_population.mjs',
        'tests/test_opensms_task_f_inventory.mjs',
        'tests/test_grabandgo_task_f_concurrency.mjs')),
    Group('task_d_transactions', 'node', (
        'tests/test_opensms_expiry_submission.mjs',
        'tests/opensms_expiry_receipt.test.mjs',
        'tests/opensms_card_retention_completeness.test.mjs',
        'tests/opensms_task_d_filtered_guards.test.mjs',
        'tests/opensms_task_d_filtered_retention.test.mjs',
        'tests/opensms_task_d_expiry.test.mjs')),
    Group('task_b_all_carriers_write', 'node', ('tests/test_opensms_task_b_all_carriers.mjs',)),
    Group('f_import_and_expired_probe', 'node', ('tests/test_opensms_f_date_import.mjs', 'tests/test_opensms_expired_card_probe.mjs')),
    py('f_import_and_expired_probe_audit', 'opensms_f_date_import', 'opensms_expired_card_probe', 'opensms_expired_card_probe_background'),
)
FULL_EXTRA = (
    py('ko_unactivated_sheet', 'opensms_ko_unactivated'),
    py('restored_google_difference', 'opensms_restored_google_difference'),
    py('chatr_document_integration', 'opensms_chatr_document_fallback', 'opensms_chatr_google_audit_report'),
    py('document_integration', 'opensms_document_no_google', 'opensms_local_system_source',
       'opensms_system_kooda_source', 'opensms_system_kooda_integration',
       'opensms_report_date_priority', 'opensms_export_schema_compat'),
    py('document_integrity', 'opensms_numeric_text_ooxml', 'opensms_retained_excel_value_types',
       'opensms_document_performance', 'opensms_export_scan_cache',
       'opensms_refresh_workbook_carriers_fast', 'opensms_zero_delta_integration'),
    py('report_audit', 'opensms_no_google_audit_report', 'opensms_task_a_report_audit',
       'opensms_finalize_patrol_report',
       'opensms_patrol_balance_summary'),
    py('task_f_lifecycle', 'opensms_task_f_deadline_hook',
       'opensms_task_f_receipt_staging'),
    py('failure_diagnostics', 'opensms_document_failure_diagnostics'),
    Group('protocol_guards', 'node', (
        'tests/test_opensms_carrier_noop.mjs',
        'tests/opensms_task_d_mapping_diagnosis.test.mjs')),
)


def selected_groups(mode):
    groups = QUICK + (FULL_EXTRA if mode == 'full' else ())
    files = [file for group in groups for file in group.files]
    if len(files) != len(set(files)) or len(groups) != len({g.name for g in groups}):
        raise ValueError('重复测试文件或组名；拒绝重复执行')
    for file in files:
        if not (ROOT / file).is_file():
            raise FileNotFoundError(f'固定测试清单缺少文件: {file}')
    return groups


def isolated_environment(directory):
    # Use an allowlist: inherited OPENSMS, credentials, proxies and runtime
    # injection flags must never authorize or redirect development validation.
    env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TZ')
           if key in os.environ}
    env.update({
        'PATH': os.pathsep.join(map(str, (PYTHON.parent, NODE.parent, '/usr/bin', '/bin'))),
        'PYTHONPATH': os.pathsep.join(map(str, (ROOT, ROOT / 'tests', ROOT / 'scripts'))),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
        'PYTHONPYCACHEPREFIX': str(Path(directory) / 'pycache'),
        'TMPDIR': str(directory), 'TMP': str(directory), 'TEMP': str(directory),
        'BITBROWSER_CDP': 'http://127.0.0.1:1', 'OPENSMS_EXACT_CDP_ONLY': '1',
        'OPENSMS_FORCE_RAW_CDP': '1', 'OPENSMS_RAW_API_TARGET_ID': 'offline-fixed-rules',
        'OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY': '1',
        'OPENSMS_FIXED_RULES_OFFLINE': '1',
        'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
        'VECLIB_MAXIMUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1',
    })
    return env


def sandbox_profile(directory, temporary=None):
    # Each child can only write in its own directory. This also prevents test
    # groups from sharing DBs, workbooks, caches, production logs or deliveries.
    allowed = {str(Path(path).resolve()) for path in (directory, temporary) if path is not None}
    private = ''.join(f'(allow file-write* (subpath {json.dumps(path)}))' for path in sorted(allowed))
    return ('(version 1)(allow default)(deny network*)(deny file-write*)'
            f'{private}'
            '(allow file-write-data (literal "/dev/null"))'
            '(deny process-exec (literal "/usr/bin/security")'
            ' (literal "/usr/bin/open") (literal "/usr/bin/osascript"))')


def command_for(group):
    if group.runtime == 'python':
        return [str(PYTHON), '-B', '-m', 'unittest', '-q',
                *(Path(file).stem for file in group.files)]
    if group.runtime == 'node':
        return [str(NODE), '--test', '--test-reporter=tap', '--test-concurrency=1', *group.files]
    raise ValueError(f'不支持的测试运行时: {group.runtime}')


def cleanup_process_group(process, grace=0.4):
    """Only signal the session created for this Popen, never a discovered PID."""
    pgid = process.pid
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError('拒绝清理非本次创建的进程组')
    sent = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        absent = False
        deadline = time.monotonic() + 2
        while True:
            try:
                os.killpg(pgid, sig)
                sent = True
                break
            except ProcessLookupError:
                absent = True
                break
            except PermissionError:
                # macOS sandbox-exec can briefly reject signals while applying
                # its profile, before exec replaces it with the test runtime.
                # Retry only this owned group, never discover/kill other PIDs.
                if time.monotonic() >= deadline:
                    raise
                process.poll()
                time.sleep(0.02)
        if absent:
            break
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                process.poll()
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
                time.sleep(0.02)
    process.wait(timeout=2)
    return {'processGroup': pgid, 'signalsSent': sent, 'parentReaped': True}


def run_groups(groups, output, *, workers, timeout, command_builder=command_for,
               sandbox=SANDBOX, cancelled=lambda: False):
    """Bounded dispatch, fail-stop queue, then drain all already-started groups."""
    if not 1 <= workers <= MAX_WORKERS or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('workers必须为1至4，timeout必须为有限正数')
    started = time.monotonic()
    pending = list(groups)
    active = {}
    results = {}
    stopped = False
    peak = 0
    scheduler_error = None

    def finish(name, status, error=None):
        item = active.pop(name)
        process, row = item['process'], item['row']
        try:
            row['cleanup'] = cleanup_process_group(process)
            if status == 'passed' and row['cleanup']['signalsSent']:
                status = 'orphaned_children'
                error = '测试退出后仍有本组子进程，已清理并判失败'
        except Exception as exc:
            status = 'cleanup_failed'
            error = f'{error or ""}; {type(exc).__name__}: {exc}'.strip('; ')
        finally:
            item['handle'].close()
            try:
                if row.get('cleanup', {}).get('parentReaped'):
                    item['temporary'].cleanup()
                    row['temporaryDirectoryRemoved'] = True
                else:
                    row['temporaryDirectoryRemoved'] = False
            except OSError as exc:
                status = 'cleanup_failed'
                error = f'{error or ""}; {type(exc).__name__}: {exc}'.strip('; ')
                row['temporaryDirectoryRemoved'] = False
        row.update(status=status, passed=status == 'passed', exitCode=process.returncode,
                   elapsedSeconds=round(time.monotonic() - item['started'], 3), error=error)
        try:
            # Keep complete logs on disk; only read the bounded tail containing
            # unittest/TAP totals, rather than buffer an unbounded failure log.
            with Path(row['log']).open('rb') as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 65536))
                content = handle.read().decode('utf-8', errors='replace')
        except OSError as exc:
            content = ''
            row.update(status='log_read_failed', passed=False,
                       error=f'{row.get("error") or ""}; {type(exc).__name__}: {exc}'.strip('; '))
        counts = re.findall(r'(?:Ran (\d+) tests?|^# tests (\d+)$)', content, re.MULTILINE)
        row['testCount'] = sum(int(a or b) for a, b in counts) if counts else None
        if not row['passed'] and 'cannot create temp file for here document: Operation not permitted' in content:
            row['failureKind'] = 'sandbox_heredoc_temp_incompatible'
            row['error'] = ('已知环境限制：系统Bash的here-doc临时写与每组目录隔离不兼容。'
                            '保留失败回执，不自动重复full，不放开共享目录或网络；'
                            '日常固定规则使用quick，本次full仍判失败。')
        results[name] = row

    try:
        while pending or active:
            if cancelled():
                stopped = True
                for name in list(active):
                    finish(name, 'cancelled', '用户中断；已清理本组进程')
            while pending and len(active) < workers and not stopped:
                group = pending.pop(0)
                before = time.monotonic()
                directory = output / group.name
                row = {'name': group.name, 'files': list(group.files), 'runtime': group.runtime,
                       'timeoutSeconds': timeout, 'timedOut': False, 'log': str(directory / 'test.log')}
                handle = None
                temporary_context = None
                try:
                    directory.mkdir()
                    # Use a short private TMPDIR for macOS /bin/bash heredocs;
                    # never grant writes to the shared temporary root itself.
                    temporary_context = tempfile.TemporaryDirectory(prefix='ov-', dir='/tmp')
                    temporary = Path(temporary_context.name).resolve()
                    row['temporaryDirectory'] = str(temporary)
                    command = command_builder(group)
                    row['command'] = command
                    handle = Path(row['log']).open('x', encoding='utf-8')
                    process = subprocess.Popen(
                        [str(sandbox), '-p', sandbox_profile(directory, temporary), *command], cwd=ROOT,
                        env=isolated_environment(temporary), stdin=subprocess.DEVNULL,
                        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
                    active[group.name] = {'process': process, 'handle': handle,
                                          'row': row, 'started': before, 'temporary': temporary_context}
                    peak = max(peak, len(active))
                except Exception as exc:
                    if handle:
                        handle.close()
                    if temporary_context:
                        temporary_context.cleanup()
                    row.update(status='launch_failed', passed=False, exitCode=None,
                               elapsedSeconds=round(time.monotonic() - before, 3),
                               testCount=None, error=f'{type(exc).__name__}: {exc}')
                    results[group.name] = row
                    stopped = True
            for name, item in list(active.items()):
                code = item['process'].poll()
                if code is not None:
                    finish(name, 'passed' if code == 0 else 'failed')
                    stopped |= code != 0 or not results[name]['passed']
                elif time.monotonic() - item['started'] >= timeout:
                    item['row']['timedOut'] = True
                    finish(name, 'timeout', f'测试组超过{timeout:g}秒；未自动重试')
                    stopped = True
            if stopped and not active:
                break
            if active:
                time.sleep(0.05)
    except BaseException as exc:
        # Even unexpected scheduler errors drain/clean every process before
        # reporting the scheduler error with the completed group evidence.
        scheduler_error = f'{type(exc).__name__}: {exc}'
        for name in list(active):
            finish(name, 'cancelled', '调度异常；已清理本组进程')
    for group in pending:
        results[group.name] = {'name': group.name, 'files': list(group.files),
                              'status': 'not_started', 'passed': False, 'exitCode': None,
                              'testCount': None, 'elapsedSeconds': 0, 'timedOut': False,
                              'error': '先前测试失败或中断，停止派发'}
    ordered = [results[group.name] for group in groups]
    return {'status': 'passed' if not scheduler_error and all(row['passed'] for row in ordered) else 'failed',
            'schedulerError': scheduler_error,
            'workersRequested': workers, 'workersUsed': peak,
            'peakConcurrentGroups': peak, 'elapsedSeconds': round(time.monotonic() - started, 3),
            'groupCount': len(groups), 'testCount': sum(row.get('testCount') or 0 for row in ordered),
            'results': ordered}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mode', choices=('quick', 'full'), default='quick')
    parser.add_argument('--workers', type=int, choices=range(1, MAX_WORKERS + 1), default=2)
    parser.add_argument('--timeout', type=float, default=180, help='每组超时秒数；超时判失败')
    parser.add_argument('--list', action='store_true', help='只列固定清单，不运行测试或创建文件')
    parser.add_argument('--json', action='store_true', help='兼容统一入口格式；输出始终为JSON')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout必须为有限正数')
    try:
        groups = selected_groups(args.mode)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'mode': args.mode,
                          'scope': 'offline-tests-not-live-patrol', 'livePatrolExecuted': False,
                          'results': [], 'receipt': None,
                          'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
        return 2
    if args.list:
        print(json.dumps({'mode': args.mode, 'scope': 'offline-tests-not-live-patrol',
                          'groups': [group.__dict__ for group in groups]}, ensure_ascii=False))
        return 0
    try:
        output_root = ROOT / 'outputs'
        output_root.mkdir(exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix='opensms-fixed-rules-', dir=str(output_root))).resolve()
    except OSError as exc:
        print(json.dumps({'status': 'failed', 'mode': args.mode, 'livePatrolExecuted': False,
                          'results': [], 'receipt': None,
                          'error': f'无法创建独立验收目录: {exc}'}, ensure_ascii=False))
        return 2
    receipt = output / 'verification.json'
    print(json.dumps({'event': 'fixed_rule_verification_started', 'mode': args.mode,
                      'receipt': str(receipt)}, ensure_ascii=False), file=sys.stderr, flush=True)
    payload = {'mode': args.mode, 'createdAt': datetime.now().astimezone().isoformat(),
               'scope': 'offline-tests-not-live-patrol', 'livePatrolExecuted': False,
               'networkDeniedByOS': False, 'writesRestrictedToOwnGroupByOS': False,
               'inheritedBusinessEnvironmentCleared': True, 'browserFallbackDisabled': True}
    interrupted = []
    old_handlers = {}
    before = time.monotonic()
    try:
        if sys.platform != 'darwin' or not SANDBOX.is_file():
            raise RuntimeError('缺少macOS离线隔离；拒绝无保护运行')
        if not PYTHON.is_file() or not NODE.is_file():
            raise RuntimeError('缺少现有Python/Node运行时；不联网安装或回退')
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, lambda number, frame: interrupted.append(number))
        payload.update(networkDeniedByOS=True, writesRestrictedToOwnGroupByOS=True)
        payload.update(run_groups(groups, output, workers=args.workers, timeout=args.timeout,
                                  cancelled=lambda: bool(interrupted)))
    except Exception as exc:
        payload.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    payload.update(totalElapsedSeconds=round(time.monotonic() - before, 3), receipt=str(receipt))
    with receipt.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
