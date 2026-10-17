#!/usr/bin/env python3
"""Same historical input, real local jobs; OS-denied network/private writes.

Only replays three allowlisted local calculation commands from the bound run.
This never exports live data, submits candidates, writes a business DB, or tunes
production defaults. Historical sources remain read-only test fixtures.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from opensms_benchmark_local import sandbox_profile
from opensms_verify_fixed_rules import isolated_environment
from opensms_local_jobs import run_local_jobs
from opensms_scope_comparison import build
from opensms_patrol_runner import compact_launcher_result, parse_last_json
from opensms_chat_summary import render_summary


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def normalize(value, directory):
    if isinstance(value, dict):
        return {k: normalize(v, directory) for k, v in value.items()
                if k not in {'createdAt', 'analysis_time_bj'}}
    if isinstance(value, list):
        return [normalize(v, directory) for v in value]
    if isinstance(value, str):
        return value.replace(str(directory), '<private-output>')
    return value


def worker(state, output, repeat):
    run_id, acquire = state['runId'], state['stages']['acquire']
    logs = ROOT / 'opensms_artifacts/logs/patrol_runner' / run_id
    commands = {}
    for name, script in (('analysis', 'opensms_analyze_current.py'),
                         ('mainRouting', 'opensms_generate_opr_zero_candidates.py'),
                         ('eRecovery', 'opensms_generate_e_reply_recovery.py')):
        label = {'mainRouting': 'main_candidates', 'eRecovery': 'e_recovery'}.get(name, name)
        with (logs / (label + '.log')).open() as stream:
            line = stream.readline(16384)
        argv = shlex.split(line.removeprefix('$ '))
        if len(argv) < 3 or Path(argv[1]).resolve() != ROOT / script:
            raise ValueError('拒绝重放非固定本地计算脚本')
        required = {'analysis': ('--scan-cache',),
                    'mainRouting': ('--scan-cache', '--out-dir', '--c2-daily-state-dir'),
                    'eRecovery': ('--scan-cache', '--out')}[name]
        if (any(flag not in argv for flag in required)
                or Path(argv[2]).resolve() != Path(acquire['processingSource']).resolve()
                or Path(argv[argv.index('--scan-cache') + 1]).resolve() != Path(acquire['scanCache']).resolve()):
            raise ValueError('本地计算没有绑定同一源表/扫描缓存或私有写入路径')
        commands[name] = [sys.executable, *argv[1:]]
    records, expected = [], None
    for index in range(repeat):
        modes = ['legacy3_then_scope', 'parallel3', 'parallel4']
        if index % 2:
            modes.reverse()
        for mode in modes:
            folder = output / f'{index + 1}_{mode}'
            folder.mkdir()

            def job(name):
                directory = folder / name
                directory.mkdir()
                argv = list(commands[name])
                for flag, target in (('--out-dir', directory),
                                     ('--out', directory / 'e.json'),
                                     ('--c2-daily-state-dir', directory / 'c2')):
                    if flag in argv:
                        argv[argv.index(flag) + 1] = str(target)
                env = isolated_environment(directory)
                env['OPENSMS_ARTIFACTS_DIR'] = str(directory / 'artifacts')
                completed = subprocess.run(argv, cwd=ROOT, env=env, text=True,
                                           capture_output=True, check=False, timeout=90)
                (directory / 'command.log').write_text(completed.stdout + '\n' + completed.stderr)
                if completed.returncode:
                    raise RuntimeError(f'{name}失败；详见私有command.log')
                parse_last_json(completed.stdout)  # Structured success is required.
                # Compare every generated JSON, including all candidate rows,
                # not just candidate totals. Ignore only creation timestamps.
                return {str(file.relative_to(directory)): json.loads(file.read_text())
                        for file in sorted(directory.rglob('*.json')) if not file.name.startswith('._')}

            def scope():
                return build(acquire['scanCache'], acquire['processingSource'],
                             acquire['exportReceipt'], run_id, acquire['serverDate'], folder / 'scope')

            jobs = {name: lambda name=name: job(name) for name in ('mainRouting', 'eRecovery', 'analysis')}
            began = time.perf_counter()
            if mode == 'legacy3_then_scope':
                results, schedule = run_local_jobs(jobs, 3)
                results['scopeComparison'] = scope()
            else:
                results, schedule = run_local_jobs({**jobs, 'scopeComparison': scope},
                                                   4 if mode == 'parallel4' else 3)
            duration = time.perf_counter() - began
            semantic = normalize(results, folder)
            encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, allow_nan=False)
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            (folder / 'semantics.json').write_text(encoded)
            if expected is not None and expected != fingerprint:
                raise RuntimeError('串并行业务结果不一致，拒绝宣称提速；保留semantics.json')
            expected = fingerprint
            records.append({'mode': mode, 'iteration': index + 1, 'seconds': round(duration, 6),
                            'semanticSha256': fingerprint, 'schedule': schedule})
    full = state['summary']
    before = copy.deepcopy(full)
    compact = compact_launcher_result(full)
    if full != before or render_summary(full) != render_summary(compact):
        raise RuntimeError('精简结果改变完整证据或聊天摘要')
    sizes = {'fullSummaryBytes': len(json.dumps(full, ensure_ascii=False, indent=2).encode()),
             'compactStdoutBytes': len(json.dumps(compact, ensure_ascii=False, separators=(',', ':')).encode()),
             'sizeMeasurement': 'UTF-8-bytes-not-model-billed-tokens'}
    medians = {mode: statistics.median(r['seconds'] for r in records if r['mode'] == mode)
               for mode in modes}
    return {'records': records, 'medianSeconds': medians, 'allBusinessResultsEqual': True,
            'sevenSectionChatUnchanged': True, 'completeSummaryUnmodified': True, **sizes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    run_id = state.get('runId')
    if (state.get('status') != 'success' or not run_id
            or args.state.name != f'opensms_patrol_runner_state_{run_id}.json'
            or not 1 <= args.repeat <= 5):
        raise ValueError('需要精确成功状态及1至5次对照；不接受未终态业务')
    if args.worker:
        result = worker(state, args.output, args.repeat)
        (args.output / 'worker.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return
    output_root = (ROOT / 'outputs').resolve()
    directory = Path(tempfile.mkdtemp(prefix='development-local-jobs-', dir=output_root)).resolve()
    inputs = [args.state, Path(state['stages']['acquire']['processingSource']),
              Path(state['stages']['acquire']['scanCache']), Path(state['stages']['acquire']['exportReceipt'])]
    hashes = {str(path): sha(path) for path in inputs}
    env = isolated_environment(directory)
    command = ['/usr/bin/sandbox-exec', '-p', sandbox_profile(directory), sys.executable, '-B',
               str(Path(__file__).resolve()), '--state', str(args.state.resolve()),
               '--output', str(directory), '--repeat', str(args.repeat), '--worker']
    with (directory / 'benchmark.log').open('x') as log:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   check=False, timeout=300)
    unchanged = all(sha(path) == hashes[str(path)] for path in inputs)
    result = json.loads((directory / 'worker.json').read_text()) if completed.returncode == 0 else {}
    result.update(status='passed' if completed.returncode == 0 and unchanged else 'failed',
                  runId=run_id, scope='historical-input-offline-local-calculations-not-live-patrol',
                  networkDeniedByOS=True, writesRestrictedToPrivateDirectoryByOS=True,
                  inputSha256=hashes, inputsUnchanged=unchanged, businessSubmitted=False,
                  fixtureHistory='private-test-history-and-ledger-not-production-replay',
                  configurationChanged=False, receipt=str(directory / 'benchmark.json'))
    (directory / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    sys.exit(main())
