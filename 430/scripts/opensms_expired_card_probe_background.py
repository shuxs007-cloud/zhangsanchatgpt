#!/usr/bin/env python3
"""Detached A survival tests. No export, main-report rewrite, retry or login.

The parent only publishes a small immutable job. Business work starts after
successful final delivery and acquisition of fresh website/report leases.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from opensms_browser_session import resolve_configured_browser
from opensms_site_execution import SiteExecutionLease, REPORT_LOCK, REQUEST_LOCK
from opensms_storage_policy import config, require_external

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
RUNTIME = Path('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies')
PYTHON, NODE = RUNTIME / 'python/bin/python3', RUNTIME / 'node/bin/node'
JOBS_ROOT = ROOT / 'opensms_artifacts/expired_card_probe_jobs'
SCOPE = 'expired_zones_all_listing_states_v2'
PROGRAMS = ('opensms_expired_card_probe_background.py', 'opensms_expired_card_probe.mjs',
            'opensms_expired_card_probe_lib.mjs', 'opensms_expired_card_probe_audit.py')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def publish(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def update(path, value):
    temporary = Path(str(path) + '.' + uuid.uuid4().hex + '.tmp')
    publish(temporary, value)
    os.replace(temporary, path)
    descriptor = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def isolated_environment(source):
    # Never inherit the parent's live leases, hidden target or task authority.
    allowed = {'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'LC_CTYPE',
               'PYTHONDONTWRITEBYTECODE'}
    return {key: value for key, value in source.items() if key in allowed}


def prepare_and_start(runner, acquire):
    directory = JOBS_ROOT / runner.run_id
    directory.mkdir(parents=True, exist_ok=False)
    proofs = []
    for key in ('phoneDateReceipt', 'dayDateReceipt'):
        original = Path(acquire[key])
        value = read(original)
        if value.get('serverDate') != acquire['serverDate']:
            raise ValueError('EXPIRED_PROBE_PARENT_DATE_PROOF_INVALID')
        copied = directory / (key + '.json')
        with copied.open('xb') as stream:
            stream.write(original.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        proofs.append({'path': str(copied), 'sha256': digest(copied)})
    input_path = directory / 'input.json'
    publish(input_path, {'runId': runner.run_id, 'serverDate': acquire['serverDate'],
                        'serverDateVerified': True, 'dateReceipts': proofs,
                        'scope': SCOPE, 'deadlineFilterEnabled': False})
    settings = config(ROOT)
    if settings:
        require_external(settings)
        receipts = Path(settings['bulkRoot']) / 'expired_card_probe_outputs' / runner.run_id
    else:
        receipts = directory / 'receipts'
    # Separate from the delivered main tree and its immutable manifest.
    receipts.mkdir(parents=True, exist_ok=False)
    job_path, state_path = directory / 'job.json', directory / 'state.json'
    job = {'runId': runner.run_id, 'scope': SCOPE, 'background': True,
           'mainReportIndependent': True, 'deadlineFilterEnabled': False,
           'parentState': str(runner.state_path),
           'parentPid': getattr(runner, 'state', {}).get('runnerPid', os.getpid()),
           'gate': str(directory / 'delivery_complete.json'),
           'inputPath': str(input_path), 'inputSha256': digest(input_path),
           'receipt': str(receipts / 'probe.json'), 'state': str(state_path),
           'programHashes': {name: digest(SCRIPTS / name) for name in PROGRAMS},
           'browser': {key: runner.args.browser_session[key]
                       for key in ('sessionId', 'pid', 'cdp', 'webSocketPath')}}
    publish(job_path, job)
    job_hash = digest(job_path)
    publish(state_path, {'runId': runner.run_id, 'scope': SCOPE,
                        'status': 'waiting_main_delivery', 'jobSha256': job_hash,
                        'onlineStarted': False, 'mainReportIndependent': True})
    log_path = directory / 'background.log'
    with log_path.open('x') as log:
        process = subprocess.Popen(
            [str(PYTHON), str(Path(__file__).resolve()), '--job', str(job_path),
             '--job-sha256', job_hash], cwd=ROOT,
            env=isolated_environment(runner.child_environment()),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    return {'runId': runner.run_id, 'scope': SCOPE, 'status': 'queued',
            'background': True, 'statusIsLaunchSnapshot': True,
            'mainReportIndependent': True, 'deadlineFilterEnabled': False,
            'backgroundJob': str(job_path), 'backgroundJobSha256': job_hash,
            'backgroundState': str(state_path), 'backgroundPid': process.pid,
            'receipt': job['receipt'], 'executed': False, 'waitForCompletion': False}


def verify_dispatch(probe):
    """File-only proof of dispatch, never proof of successful card recovery."""
    job_path = Path(probe['backgroundJob'])
    if digest(job_path) != probe['backgroundJobSha256']:
        raise ValueError('EXPIRED_PROBE_JOB_HASH_CHANGED')
    job = read(job_path)
    if (job['runId'] != probe['runId'] or job['scope'] != SCOPE
            or job.get('deadlineFilterEnabled') is not False
            or job.get('mainReportIndependent') is not True
            or job['state'] != probe['backgroundState'] or job['receipt'] != probe['receipt']
            or probe.get('statusIsLaunchSnapshot') is not True
            or probe.get('waitForCompletion') is not False
            or digest(job['inputPath']) != job['inputSha256']):
        raise ValueError('EXPIRED_PROBE_DISPATCH_CONTRACT_INVALID')
    inputs = read(job['inputPath'])
    if (inputs.get('runId') != job['runId'] or inputs.get('scope') != SCOPE
            or inputs.get('deadlineFilterEnabled') is not False
            or inputs.get('serverDateVerified') is not True
            or len(inputs.get('dateReceipts', [])) != 2):
        raise ValueError('EXPIRED_PROBE_INPUT_INVALID')
    for proof in inputs['dateReceipts']:
        if digest(proof['path']) != proof['sha256'] or read(proof['path'])['serverDate'] != inputs['serverDate']:
            raise ValueError('EXPIRED_PROBE_PARENT_DATE_PROOF_CHANGED')
    return job


def release_after_delivery(probe):
    if not probe or not probe.get('backgroundJob'):
        return
    job = verify_dispatch(probe)
    gate = {'runId': job['runId'], 'jobSha256': probe['backgroundJobSha256'],
            'status': 'main_delivery_complete'}
    publish(job['gate'], gate)


def await_delivery(job, job_hash, *, wait=time.sleep, clock=time.monotonic, timeout=7200):
    until = clock() + timeout
    # Read the potentially large parent checkpoint once, not on every poll.
    parent = read(job['parentState'])
    if parent.get('runId') != job['runId']:
        raise ValueError('EXPIRED_PROBE_PARENT_ID_CHANGED')
    if parent.get('status') == 'failed':
        return False
    while clock() < until:
        if str(read(job['state']).get('status', '')).startswith('failed'):
            return False
        if Path(job['gate']).is_file():
            parent = read(job['parentState'])
            if read(job['gate']) != {'runId': job['runId'], 'jobSha256': job_hash,
                                    'status': 'main_delivery_complete'} or parent.get('status') != 'success' or parent.get('runId') != job['runId']:
                raise ValueError('EXPIRED_PROBE_DELIVERY_GATE_INVALID')
            return True
        try:
            os.kill(job['parentPid'], 0)
        except ProcessLookupError:
            return False  # Parent ended without a verified delivery gate.
        wait(0.5)
    raise ValueError('EXPIRED_PROBE_DELIVERY_WAIT_TIMEOUT_NO_BUSINESS')


def execute_job(job_path, job_hash):
    if (not re.fullmatch(r'\d{8}T\d{6}\+0800', job_path.parent.name)
            or job_path != JOBS_ROOT / job_path.parent.name / 'job.json'
            or digest(job_path) != job_hash):
        raise ValueError('EXPIRED_PROBE_JOB_UNCONFIRMED')
    job = read(job_path)
    state = read(job['state'])
    if state.get('status') != 'waiting_main_delivery' or state.get('jobSha256') != job_hash:
        raise ValueError('EXPIRED_PROBE_WORKER_REPLAY_FORBIDDEN')
    # Consume this exact job even if the process crashes before taking a lease.
    publish(job_path.parent / 'worker_claim.json', {'runId': job['runId'], 'pid': os.getpid(),
                                                  'jobSha256': job_hash})
    site = report = None
    target = Path(job['receipt']).parent / 'target.json'
    env = isolated_environment(os.environ)
    try:
        if not await_delivery(job, job_hash):
            state['status'] = 'cancelled_parent_failed'
            return
        for name, expected in job['programHashes'].items():
            if name not in PROGRAMS or digest(SCRIPTS / name) != expected:
                raise ValueError('EXPIRED_PROBE_PROGRAM_CHANGED_NO_BUSINESS')
        if set(job['programHashes']) != set(PROGRAMS) or digest(job['inputPath']) != job['inputSha256']:
            raise ValueError('EXPIRED_PROBE_INPUT_CHANGED_NO_BUSINESS')
        settings = config(ROOT)
        if settings:
            require_external(settings)
        state['status'] = 'waiting_site'
        update(job['state'], state)
        # Only one worker consumes the persistent one-shot ledger at a time.
        with (JOBS_ROOT / 'worker.lock').open('a+b') as mutex:
            fcntl.flock(mutex.fileno(), fcntl.LOCK_EX)
            site = SiteExecutionLease().acquire()
            report = SiteExecutionLease(file=REPORT_LOCK).acquire()
            if REQUEST_LOCK.exists():
                raise ValueError('EXPIRED_PROBE_REQUEST_OUTCOME_UNCONFIRMED')
            browser = resolve_configured_browser()  # Resolve only; never start/login.
            if browser['status'] != 'ready' or any(browser[k] != v for k, v in job['browser'].items()):
                raise ValueError('EXPIRED_PROBE_BROWSER_CHANGED_NO_BUSINESS')
            env.update(site.environment)
            env.update(OPENSMS_SITE_JOB_LOCK_TOKEN=report.owner['token'],
                       OPENSMS_SITE_JOB_LOCK_PID=str(report.owner['pid']),
                       OPENSMS_RUN_ID=job['runId'], OPENSMS_EXPIRED_PROBE_RUN_ID=job['runId'],
                       OPENSMS_BROWSER_KIND='cloakbrowser', BITBROWSER_CDP=browser['cdp'],
                       OPENSMS_BROWSER_SESSION_ID=browser['sessionId'],
                       OPENSMS_BROWSER_SERVICE_PID=str(browser['pid']),
                       OPENSMS_EXPECTED_BROWSER_WS_PATH=browser['webSocketPath'],
                       OPENSMS_EXACT_CDP_ONLY='1', OPENSMS_FORCE_RAW_CDP='1', OPENSMS_SILENT_BROWSER='1')
            def command(args):
                result = subprocess.run([str(NODE), *map(str, args)], cwd=ROOT, env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True)
                if result.returncode:
                    raise ValueError('EXPIRED_PROBE_CHILD_FAILED_NO_REPLAY')
                return json.loads(result.stdout)
            created = command([SCRIPTS / 'opensms_api_target_guard_cli.mjs', 'create',
                               '--run-id', job['runId'], '--receipt', target])
            if not created.get('success') or not created.get('hidden') or created.get('receipt') != str(target):
                raise ValueError('EXPIRED_PROBE_TARGET_UNCONFIRMED')
            env['OPENSMS_RAW_API_TARGET_ID'] = created['targetId']
            state.update(status='running', onlineStarted=True, pid=os.getpid())
            update(job['state'], state)
            result = command([SCRIPTS / 'opensms_expired_card_probe.mjs', job['inputPath'], job['receipt']])
            from opensms_expired_card_probe_audit import verify_receipt
            verified = verify_receipt(result)
            state.update(status=verified['status'], result=result, independentAuditPassed=True)
    except Exception as exc:
        state.update(status='failed_no_replay', error=str(exc), independentAuditPassed=False)
        if Path(job['receipt']).is_file():
            saved = read(job['receipt'])
            if saved.get('runId') == job['runId'] and saved.get('scope') == SCOPE:
                state.update(receipt=job['receipt'], receiptSha256=digest(job['receipt']))
                if re.fullmatch(r'EXPIRED_PROBE_[A-Z_]+', saved.get('error') or ''):
                    state['error'] = saved['error']
    finally:
        safe = not REQUEST_LOCK.exists()
        if target.is_file():
            try:
                result = subprocess.run([str(NODE), str(SCRIPTS / 'opensms_api_target_guard_cli.mjs'),
                                         'close', '--run-id', job['runId'], '--receipt', str(target)],
                                        cwd=ROOT, env=env, capture_output=True, text=True)
                cleanup = json.loads(result.stdout)
                if result.returncode or not cleanup.get('success') or not cleanup.get('closed'):
                    raise ValueError('EXPIRED_PROBE_TARGET_CLEANUP_UNCONFIRMED')
            except Exception:
                safe = False
                state.update(status='failed_no_replay', cleanupError='EXPIRED_PROBE_TARGET_CLEANUP_UNCONFIRMED')
        if Path(str(target) + '.create_failed.json').exists():
            safe = False
            state.update(status='failed_no_replay', cleanupError='EXPIRED_PROBE_TARGET_CREATE_CLEANUP_UNCONFIRMED')
        if safe:
            if report: report.release()
            if site: site.release()
        state.update(mainReportIndependent=True, completedAt=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        update(job['state'], state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--job-sha256', required=True)
    args = parser.parse_args()
    execute_job(args.job, args.job_sha256)
