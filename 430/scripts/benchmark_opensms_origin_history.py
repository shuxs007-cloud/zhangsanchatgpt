#!/usr/bin/env python3
"""Read-only historical origin comparison; OS-denied network and business writes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from opensms_verify_fixed_rules import isolated_environment, sandbox_profile


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def worker(args):
    from unittest.mock import patch
    from opensms_export_scan_cache import load_scan_cache
    import patch_0500_origins_and_restore as history
    from opensms_generate_e_reply_recovery import as_int, DEFAULT_E_CARRIERS
    before = {path: digest(path) for path in (args.source, args.cache)}
    scan = load_scan_cache(args.cache, expected_source=args.source)
    names = [str(value or "").strip() for value in scan["headers"]]
    import re
    latest = max(name[:5] for name in names if re.fullmatch(r"\d{2}-\d{2}发送窗口数", name))
    headers = {name: i for i, name in enumerate(names) if name}
    keys = set()
    for values in scan["rows"]:
        def value(name):
            index = headers.get(name)
            return values[index] if index is not None and index < len(values) else None
        if str(value("运营商") or "").strip() in DEFAULT_E_CARRIERS and as_int(value(latest + "回复窗口数")) > 0:
            keys.add(history.key_for({"电话": value("电话"), "卡槽": value("卡槽")}))
    original = history.load_json
    records, reference, history_hashes = [], None, {}
    for label, wanted, workers in (("legacy_full_serial", None, 1), ("optimized_needed_parallel4", keys, 4)):
        reads = []
        def read(path):
            reads.append(path)
            return original(path)
        started = time.monotonic()
        with patch.object(history, "load_json", side_effect=read):
            result = history.build_origin_map(keys=wanted, workers=workers)
        elapsed = time.monotonic() - started
        narrowed = {key: result[key] for key in sorted(keys) if key in result}
        if reference is None:
            reference = narrowed
        elif narrowed != reference:
            raise ValueError("历史归属逐字段不一致")
        records.append({"mode": label, "seconds": elapsed, "historyFilesRead": len(reads)})
        for path in set(reads):
            history_hashes.setdefault(path, digest(path))
    unchanged = all(digest(path) == expected for path, expected in (before | history_hashes).items())
    if not unchanged:
        raise ValueError("只读测试输入发生变化")
    receipt = {"status": "passed", "source": args.source, "requiredIdentityCount": len(keys),
        "records": records, "resultsExactlyEqual": True, "inputsUnchanged": unchanged,
        "historyFilesVerified": len(history_hashes), "networkDeniedByOS": True,
        "businessSubmitted": False, "measurementScope": "history-step-only-offline-single-pair"}
    Path(args.output, "benchmark.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(receipt, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if sys.platform != "darwin":
        raise RuntimeError("缺少macOS隔离，拒绝运行")
    output = Path(tempfile.mkdtemp(prefix="origin-history-", dir=ROOT / "outputs")).resolve()
    command = ["/usr/bin/sandbox-exec", "-p", sandbox_profile(output), sys.executable, "-B", __file__,
        "--worker", "--output", str(output), "--source", str(Path(args.source).resolve()),
        "--cache", str(Path(args.cache).resolve())]
    result = subprocess.run(command, env=isolated_environment(output), cwd=ROOT, timeout=180, capture_output=True, text=True)
    print(json.dumps({"receipt": str(output / "benchmark.json"), "exitCode": result.returncode,
        "result": result.stdout.strip(), "error": result.stderr.strip()}, ensure_ascii=False))
    if result.returncode:
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
