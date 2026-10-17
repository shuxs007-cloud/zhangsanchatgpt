#!/usr/bin/env python3
"""Network-denied, same-input verifier A/B benchmark with independent receipts.

Historical inputs are read-only fixtures, never a substitute for fresh delivery.
This measures local verification only, not online export or end-to-end patrol.
"""
from __future__ import annotations

import argparse
import contextlib
import cProfile
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pstats
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def worker(args):
    directory = args.worker_dir.resolve()
    state = json.loads(args.state.read_text())
    receipt = json.loads(args.receipt.read_text())
    acquisition = state["stages"]["acquire"]
    cache = state["reportDateCache"]
    process = receipt["process"]
    if process.get("google_dependency", {}).get("enabled") is not False:
        raise RuntimeError("This benchmark requires an explicit no-Google receipt")
    os.environ.update({
        "OPENSMS_GOOGLE_ASSOCIATION_ENABLED": "0", "OPENSMS_RUN_ID": state["runId"],
        "OPENSMS_SYSTEM_KOODA_PATH": acquisition["processingSystemKooda"],
        "OPENSMS_SYSTEM_KOODA_SHA256": acquisition["systemKoodaSha256"],
        "OPENSMS_REPORT_DATE_CACHE_PATH": cache["path"],
        "OPENSMS_REPORT_DATE_CACHE_SHA256": cache["sha256"],
        "PROCESS_JSON": json.dumps(process), "ENRICH_JSON": json.dumps(receipt["enrichment"]),
        "EXPORT4DAY_SERVER_DATE": acquisition["serverDate"], "PYTHONDONTWRITEBYTECODE": "1",
    })
    sys.dont_write_bytecode = True
    def inside(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(path)).resolve()
            if target != directory and directory not in target.parents:
                raise RuntimeError(f"Benchmark write outside isolated output denied: {target}")
    def audit(event, values):
        if event.startswith("socket.") and event != "socket.__new__":
            raise RuntimeError("Benchmark network denied")
        if event == "subprocess.Popen":
            raise RuntimeError("Benchmark worker subprocess denied")
        if event == "open":
            path, mode, flags = values
            if (mode and any(char in mode for char in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                inside(path)
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            inside(values[0])
        if event in {"os.rename", "os.replace"}:
            inside(values[0]); inside(values[1])
    sys.addaudithook(audit)
    spec = importlib.util.spec_from_file_location("benchmarked_verifier", args.script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.argv = [str(args.script), receipt["output_path"], acquisition["processingSource"]]
    capture = io.StringIO()
    profile = cProfile.Profile()
    started = time.perf_counter()
    with contextlib.redirect_stdout(capture):
        if args.profile:
            result = profile.runcall(module.main)
        else:
            result = module.main()
    elapsed = time.perf_counter() - started
    if result:
        raise RuntimeError(f"Verifier returned {result}")
    payload = json.loads(capture.getvalue())
    save(directory / "verification.json", payload)
    if args.profile:
        report = io.StringIO()
        pstats.Stats(profile, stream=report).sort_stats("cumulative").print_stats(70)
        (directory / "profile.txt").write_text(report.getvalue(), encoding="utf-8")
        profile.dump_stats(str(directory / "profile.pstats"))
    print(json.dumps({"elapsedSeconds": elapsed, "verification": str(directory / "verification.json"),
                      "verificationSha256": digest(directory / "verification.json")}))


def main(args):
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("Benchmark output directory must be new")
    output.mkdir(parents=True)
    state = json.loads(args.state.read_text())
    receipt = json.loads(args.receipt.read_text())
    acquisition = state["stages"]["acquire"]
    paths = [args.state, args.receipt, Path(receipt["output_path"]), Path(acquisition["processingSource"]),
             Path(acquisition["processingSystemKooda"]), Path(state["reportDateCache"]["path"])]
    hashes = {str(path.resolve()): digest(path) for path in paths}
    scripts = {"baseline": args.baseline or CURRENT, "current": args.current or CURRENT}
    frozen = {}
    for name, script in scripts.items():
        frozen[name] = output / f"{name}_verifier.py"
        shutil.copyfile(script, frozen[name])
    records = []
    results = {}
    for iteration in range(args.repeat):
        for label in ("baseline", "current") if iteration % 2 == 0 else ("current", "baseline"):
            directory = output / f"{iteration + 1}-{label}"
            directory.mkdir()
            command = [sys.executable, str(Path(__file__).resolve()), "--worker-dir", str(directory),
                       "--state", str(args.state.resolve()), "--receipt", str(args.receipt.resolve()),
                       "--script", str(frozen[label])]
            if args.profile:
                command.append("--profile")
            process = subprocess.run(command, capture_output=True, text=True, timeout=120,
                                     env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            (directory / "stdout.json").write_text(process.stdout)
            (directory / "stderr.txt").write_text(process.stderr)
            if process.returncode:
                raise RuntimeError(f"{label} benchmark failed: {process.stderr}")
            result = json.loads(process.stdout)
            results[label] = json.loads(Path(result["verification"]).read_text())
            records.append({"side": label, "iteration": iteration + 1, "scriptSha256": digest(frozen[label]),
                            "argv": command, "returncode": process.returncode, **result})
            save(output / "progress.json", records)
    if {str(path.resolve()): digest(path) for path in paths} != hashes:
        raise RuntimeError("Read-only source changed during benchmark")
    median = {name: statistics.median(row["elapsedSeconds"] for row in records if row["side"] == name)
              for name in scripts}
    complete_equal = len({row["verificationSha256"] for row in records}) == 1
    result = {"scope": "offline-local-document-verification-only", "sourceRunId": state["runId"],
              "networkDeniedInWorkers": True, "historicalInputsUsedOnlyForBenchmark": True,
              "inputSha256": hashes, "inputsUnchanged": True, "repeats": args.repeat,
              "profilingEnabled": args.profile, "medianSeconds": median,
              "reductionPercent": (1 - median["current"] / median["baseline"]) * 100,
              "completeVerificationJsonEqual": complete_equal,
              "records": records}
    save(output / "benchmark.json", result)
    print(json.dumps({key: value for key, value in result.items() if key not in {"inputSha256", "records"}}, ensure_ascii=False))
    if not complete_equal:
        raise RuntimeError("A/B verification evidence differs; performance result is not accepted")


def full_pipeline(args):
    """Replay only local document processing, under an inherited OS sandbox."""
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.is_file():
        raise RuntimeError("OS isolation is required for complete pipeline replay")
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("Development replay output must be new")
    output.mkdir(parents=True)
    temporary = output / "tmp"
    temporary.mkdir()
    state = json.loads(args.state.read_text())
    old_receipt = json.loads(args.receipt.read_text())
    source = state["stages"]["acquire"]
    cache = state["reportDateCache"]
    inputs = [args.state, args.receipt, Path(source["processingSource"]), Path(source["sourceArchive"]),
              Path(source["processingSystemKooda"]), Path(cache["path"]), Path(old_receipt["output_path"])]
    hashes = {str(path.resolve()): digest(path) for path in inputs}
    protected = {str(path.resolve().parent) for path in inputs}
    protected.update({str(ROOT / "opensms_artifacts/state"), str(ROOT / "opensms_artifacts/logs/patrol_runner"),
                      "/Volumes/zs01/chatgpt/OpenSMS"})
    # The same deny-network / protect-production policy as the offline suite.
    # macOS Bash heredocs require OS temporary file operations; deny-all-writes
    # blocks those even with TMPDIR. Historical files remain explicitly read-only.
    profile = '(version 1)(allow default)(deny network*)' + ''.join(
        f'(deny file-write* (subpath {json.dumps(path)}))' for path in sorted(protected))
    command = [str(sandbox), "-p", profile, sys.executable, str(ROOT / "scripts/opensms_process_document.py"),
               "--no-google", "--force-local", "--export", source["processingSource"],
               "--server-date", source["serverDate"], "--output-dir", str(output / "document"),
               "--api-config", str(output / "no-api-config"), "--system-kooda", source["processingSystemKooda"],
               "--system-kooda-sha256", source["systemKoodaSha256"], "--report-date-cache", cache["path"],
               "--report-date-cache-sha256", cache["sha256"], "--run-id", state["runId"]]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OPENSMS_")}
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(temporary),
                        "PYTHON_BIN": sys.executable})
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True, timeout=180, env=environment)
    elapsed = time.perf_counter() - started
    (output / "stdout.json").write_text(completed.stdout, encoding="utf-8")
    (output / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    unchanged = hashes == {str(path.resolve()): digest(path) for path in inputs}
    result = json.loads(completed.stdout) if completed.stdout.strip().startswith("{") else {}
    replay = {"scope": "offline-development-full-document-replay-not-live-delivery", "sourceRunId": state["runId"],
              "networkDeniedByOS": True, "productionAndHistoricalWritesDeniedByOS": True,
              "protectedDirectories": sorted(protected), "privateTempDir": str(temporary),
              "osTemporaryFileOperationsAllowed": True,
              "inputSha256": hashes, "inputsUnchanged": unchanged, "argv": command,
              "returncode": completed.returncode, "elapsedSeconds": elapsed, "result": result}
    save(output / "replay.json", replay)
    if completed.returncode or not unchanged or result.get("result") not in {"成功", "success"}:
        raise RuntimeError(f"Isolated replay failed; inspect {output / 'replay.json'}")
    print(json.dumps({"scope": replay["scope"], "elapsedSeconds": elapsed,
                      "inputsUnchanged": unchanged, "receipt": str(output / "replay.json")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--full-pipeline", action="store_true")
    parser.add_argument("--worker-dir", type=Path)
    parser.add_argument("--script", type=Path)
    arguments = parser.parse_args()
    if arguments.worker_dir:
        worker(arguments)
    elif arguments.full_pipeline:
        if not arguments.output:
            parser.error("--output is required")
        full_pipeline(arguments)
    else:
        if not arguments.output or arguments.repeat < 1:
            parser.error("--output and positive --repeat are required")
        main(arguments)
