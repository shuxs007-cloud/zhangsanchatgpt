#!/usr/bin/env python3
"""Deterministic offline regression: OS-denied network and production data writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies')
PYTHON = RUNTIME / 'python/bin/python3'
NODE = RUNTIME / 'node/bin/node'


def main():
    sandbox = Path('/usr/bin/sandbox-exec')
    if sys.platform != 'darwin' or not sandbox.is_file():
        raise RuntimeError('缺少离线操作系统隔离，不允许退回无保护回归')
    profile = ('(version 1)(allow default)(deny network*)'
               '(deny file-write* (subpath "/Users/zhangsan/Downloads/430/opensms_artifacts/state"))'
               '(deny file-write* (subpath "/Users/zhangsan/Downloads/430/opensms_artifacts/logs/patrol_runner"))'
               '(deny file-write* (subpath "/Volumes/zs01/chatgpt/OpenSMS"))')
    stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S_%f%z')
    output = ROOT / 'outputs/development_validation' / stamp
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    commands = {
        'python': [str(PYTHON), '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
        'node': [str(NODE), '--test', 'tests/test_grabandgo_task_f_concurrency.mjs',
                 'tests/test_opensms_task_f_inventory.mjs',
                 'tests/opensms_task_d_filtered_retention.test.mjs',
                 'tests/opensms_task_d_filtered_guards.test.mjs',
                 'tests/opensms_task_d_expiry.test.mjs',
                 'tests/opensms_task_d_preparation_continuation.test.mjs',
                 'tests/test_opensms_task_d_retention_population.mjs'],
        'static': [str(PYTHON), 'scripts/opensms_patrol_runner.py', 'check'],
    }
    def execute(item):
        name, command = item
        log = output / f'{name}.log'
        before = time.monotonic()
        with log.open('x', encoding='utf-8') as handle:
            completed = subprocess.run([str(sandbox), '-p', profile, *command], cwd=ROOT,
                                       stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                       timeout=600, check=False)
        content = log.read_text(encoding='utf-8')
        tests = re.search(r'Ran (\d+) tests', content) if name == 'python' else re.search(r'ℹ tests (\d+)', content)
        return {'name': name, 'exitCode': completed.returncode, 'passed': completed.returncode == 0,
                'testCount': int(tests.group(1)) if tests else None,
                'elapsedSeconds': round(time.monotonic() - before, 3), 'log': str(log)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, commands.items()))
    payload = {'status': 'passed' if all(row['passed'] for row in results) else 'failed',
               'createdAt': datetime.now().astimezone().isoformat(),
               'networkDeniedByOS': True, 'productionDataWritesDeniedByOS': True,
               'historicalDeliveryWritesDeniedByOS': True,
               'elapsedSeconds': round(time.monotonic() - started, 3), 'results': results,
               'scope': 'offline-tests-not-live-patrol'}
    receipt = output / 'verification.json'
    with receipt.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({**payload, 'receipt': str(receipt)}, ensure_ascii=False))
    return 0 if payload['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
