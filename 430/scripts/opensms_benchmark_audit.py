#!/usr/bin/env python3
"""Same-input offline audit benchmark; never exports or changes live business.

Run against an exact successful state and a saved pre-change audit script.
Each subprocess is network-denied and can write only its own private output.
The full independent auditor runs at several bounded concurrency levels.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

from opensms_benchmark_local import ROOT, SANDBOX, digest, sandbox_profile, validate_workers
from opensms_verify_fixed_rules import isolated_environment


def save(path, data):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def load_script(path):
    spec = importlib.util.spec_from_file_location('audit_benchmark_target', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audit_semantics(audit):
    rows = []
    for item in audit['checks']:
        details = dict(item['details'])
        # This field deliberately changes with the benchmark concurrency level.
        if item['name'] == '最终交付校验':
            details.pop('hash_workers', None)
        rows.append((item['name'], item['status'], item['message'], details))
    return rows


def scan_worker(args):
    module = load_script(args.baseline)
    started = time.perf_counter()
    structure = module.inspect_result_workbook(args.source, args.server_date, False)
    structure_seconds = time.perf_counter() - started
    started = time.perf_counter()
    carriers = module.xlsx_main_phone_carriers(args.source)
    result = {'structureSeconds': structure_seconds, 'carrierSeconds': time.perf_counter() - started,
              'structureSha256': fingerprint(structure), 'carriersSha256': fingerprint(carriers),
              'rows': structure['main_rows'], 'phoneCount': len(carriers)}
    save(args.output / 'scan.json', result)


def run(args):
    if sys.platform != 'darwin' or not SANDBOX.is_file():
        raise RuntimeError('OS isolation unavailable; no unsafe fallback')
    state = json.loads(args.state.read_text())
    if state.get('status') != 'success':
        raise ValueError('Requires an exact successful state')
    source = Path(state['summary']['finalWorkbook'])
    if digest(source) != state['stages']['refresh']['finalWorkbookSha256']:
        raise ValueError('Workbook hash does not match the successful state')
    output = args.output.resolve()
    relative = output.relative_to((ROOT / 'outputs').resolve(strict=True))
    if not relative.parts or not relative.parts[0].startswith('development_'):
        raise ValueError('Output requires a private development_ directory')
    output.mkdir(parents=True, exist_ok=False)
    config = Path(state['stages']['finalize']['config'])
    original_source = Path(state['summary']['sourceWorkbook'])
    if digest(original_source) != state['stages']['acquire']['sourceSha256']:
        raise ValueError('Fresh source hash does not match the successful state')
    inputs = {str(path): digest(path) for path in (args.state, source, config, original_source)}
    save(output / 'input_hashes_before.json', inputs)
    local_source = output / 'fixture.xlsx'
    shutil.copyfile(source, local_source)
    scripts = {}
    for label, path in (('baseline', args.baseline), ('optimized', ROOT / 'scripts/opensms_parallel_audit.py')):
        scripts[label] = output / f'{label}.py'
        shutil.copyfile(path, scripts[label])
    script_hashes = {label: digest(path) for label, path in scripts.items()}
    # The auditor loads this local helper relative to its own file for Task D.
    shutil.copyfile(ROOT / 'scripts/opensms_task_d_retention_summary.py',
                    output / 'opensms_task_d_retention_summary.py')
    records, scans, audits = [], [], []

    def execute(label, command):
        folder = output / label
        folder.mkdir()
        start = time.perf_counter()
        environment = isolated_environment(folder)
        environment['PYTHONPATH'] += os.pathsep + str(ROOT / 'services/opensms_document_api/pipeline')
        completed = subprocess.run([str(SANDBOX), '-p', sandbox_profile(folder), *map(str, command(folder))],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180)
        elapsed = time.perf_counter() - start
        (folder / 'stdout.txt').write_text(completed.stdout)
        (folder / 'stderr.txt').write_text(completed.stderr)
        record = {'label': label, 'seconds': elapsed, 'exitCode': completed.returncode}
        records.append(record)
        print(json.dumps(record), flush=True)
        if completed.returncode != 0:
            raise RuntimeError(f'Offline benchmark failed; see {folder}')
        return folder

    for iteration in range(args.repeat):
        labels = list(scripts) if iteration % 2 == 0 else list(reversed(scripts))
        for label in labels:
            folder = execute(f'scan-{label}-{iteration}', lambda d, label=label: [sys.executable, __file__,
                '--scan-worker', '--baseline', scripts[label], '--source', local_source,
                '--server-date', state['summary']['serverDate'], '--output', d])
            scans.append({'label': label, **json.loads((folder / 'scan.json').read_text())})
        levels = args.workers if iteration % 2 == 0 else list(reversed(args.workers))
        for label in labels:
            for count in levels:
                folder = execute(f'audit-{label}-{count}-{iteration}', lambda d, label=label, count=count: [
                    sys.executable, scripts[label], '--config', config, '--delivery-dir', state['delivery'],
                    '--output', d / 'audit.json', '--workers', count, '--json',
                    *(['--executor', args.optimized_executor] if label == 'optimized' else [])])
                audit = json.loads((folder / 'audit.json').read_text())
                semantic = audit_semantics(audit)
                audits.append({'label': label, 'workers': count, 'outcomeSha256': fingerprint(semantic),
                               'scheduling': audit['timing'].get('local_scheduling')})
    if len({(x['structureSha256'], x['carriersSha256']) for x in scans}) != 1:
        raise RuntimeError('Old/new workbook checks differ')
    if len({x['outcomeSha256'] for x in audits}) != 1:
        raise RuntimeError('Independent audit outcomes/details differ')
    if any(digest(Path(path)) != value for path, value in inputs.items()) or digest(local_source) != inputs[str(source)]:
        raise RuntimeError('Fixture changed; reject measurements')
    medians = {label: {str(count): statistics.median(x['seconds'] for x in records
        if x['label'].startswith(f'audit-{label}-{count}-')) for count in args.workers} for label in scripts}
    result = {'status': 'passed', 'scope': 'offline_same_input_not_fresh_export', 'sourceRunId': state['runId'],
        'networkDeniedByOS': True, 'inputsUnchanged': True, 'checksAndDetailsEqual': True,
        'inputSha256': inputs, 'scriptSha256': script_hashes, 'auditMedianSeconds': medians,
        'scanRecords': scans, 'records': records, 'auditRecords': audits,
        'optimizedExecutor': args.optimized_executor, 'configurationChanged': False}
    save(output / 'benchmark.json', result)
    print(json.dumps({k: v for k, v in result.items() if k not in {'records', 'inputSha256', 'scriptSha256', 'scanRecords', 'auditRecords'}}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, choices=range(1, 6), default=2)
    parser.add_argument('--workers', type=validate_workers, default=validate_workers('1,2,4,8'))
    parser.add_argument('--optimized-executor', choices=('threads', 'hybrid'), default='threads')
    parser.add_argument('--scan-worker', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--server-date')
    args = parser.parse_args()
    if args.scan_worker:
        scan_worker(args)
    elif args.state:
        run(args)
    else:
        parser.error('--state is required')


if __name__ == '__main__':
    main()
