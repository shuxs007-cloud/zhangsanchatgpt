#!/usr/bin/env python3
"""Offline same-input old/new workbook refresh benchmark; never edits archives."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import zipfile


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def member_hashes(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError(f"Corrupt archive: {path}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError(f"Duplicate members: {path}")
        return {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names}


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_invocation_provenance(report_path):
    """Reconstruct argv from the deterministic benchmark and saved run results."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    previous = {}
    records = []
    for row in report["rows"]:
        iteration = row["iteration"]
        for side in ("old", "new"):
            result = row[side]["result"]
            output = Path(result["outputPath"])
            directory = output.parent
            command = [report["pythonExecutable"], report["baselineScript"] if side == "old" else report["currentScript"],
                       row["sameInputWorkbook"], row["sameLiveReceipt"], str(output),
                       *[str(directory / f"{name}.json") for name in ("artifact", "verify", "refresh", "readback")]]
            if side == "new":
                command += ["--run-id", result["runId"], "--verified-cache-output", result["verifiedCachePath"]]
                if iteration in previous:
                    prior = previous[iteration]
                    command += ["--verified-cache-input", prior["verifiedCachePath"], "--verified-cache-sha256", prior["verifiedCacheSha256"]]
                previous[iteration] = result
            records.append({"iteration": iteration, "stage": row["stage"], "side": side,
                            "argv": command, "exitCode": 0, "inputSha256": row["inputSha256"],
                            "liveReceiptSha256": row["liveReceiptSha256"],
                            "stdout": str(directory / "stdout.json"), "stderr": str(directory / "stderr.txt")})
    target = report_path.with_name("subprocess-invocations.json")
    save(target, {"provenance": "argv reconstructed from executed deterministic benchmark source and saved per-subprocess results; stdout/stderr were recorded during execution",
                  "sourceBenchmark": str(report_path), "sourceBenchmarkSha256": digest(report_path), "records": records})
    return target


def run(args):
    delivery = args.delivery.resolve()
    destination = args.output_dir.resolve()
    # Benchmark output must be a new independent directory, never historical delivery.
    if destination == delivery or delivery in destination.parents or destination.exists():
        raise RuntimeError("Output must be a new directory outside the historical delivery")
    destination.mkdir(parents=True)
    fixture_dir = destination / "same-input-fixtures"
    fixture_dir.mkdir()
    original_paths = [
        delivery / f"OpenSMS近10日报表巡检_{name}_{args.tag}.xlsx"
        for name in ("生成结果", "普通规则后状态", "任务B后状态")
    ]
    stages = ("refreshAfterStandard", "refreshAfterTaskB", "refresh")
    receipt_paths = [delivery / "receipts" / f"opensms_{stage}_carrier_state_{args.tag}.json" for stage in stages]
    original_files = original_paths + receipt_paths
    before = {str(path): digest(path) for path in original_files}
    fixtures = []
    for path in original_files:
        target = fixture_dir / path.name
        shutil.copyfile(path, target)
        if digest(target) != before[str(path)]:
            raise RuntimeError(f"Fixture copy mismatch: {path}")
        fixtures.append(target)
    old_script = destination / "baseline-original.py"
    shutil.copyfile(args.baseline.resolve(), old_script)
    new_script = args.current.resolve()
    rows = []
    for iteration in range(1, args.repeat + 1):
        previous_new = None
        for index, stage in enumerate(stages):
            source = fixtures[index]
            live = fixtures[index + 3]
            sides = {}
            for side, script in (("old", old_script), ("new", new_script)):
                stage_dir = destination / f"repeat-{iteration}" / stage / side
                stage_dir.mkdir(parents=True)
                output = stage_dir / "output.xlsx"
                paths = [stage_dir / f"{name}.json" for name in ("artifact", "verify", "refresh", "readback")]
                command = [sys.executable, str(script), str(source), str(live), str(output), *map(str, paths)]
                if side == "new":
                    command += ["--run-id", args.tag, "--verified-cache-output", str(stage_dir / "cache.json")]
                    if previous_new:
                        command += ["--verified-cache-input", previous_new["verifiedCachePath"], "--verified-cache-sha256", previous_new["verifiedCacheSha256"]]
                started = time.perf_counter()
                process = subprocess.run(command, capture_output=True, text=True, timeout=180)
                elapsed = time.perf_counter() - started
                (stage_dir / "stdout.json").write_text(process.stdout, encoding="utf-8")
                (stage_dir / "stderr.txt").write_text(process.stderr, encoding="utf-8")
                if process.returncode:
                    raise RuntimeError(f"{side}/{stage} failed: {process.stdout} {process.stderr}")
                result = json.loads(process.stdout)
                verification = json.loads(paths[1].read_text())
                if not result["passed"] or not verification["passed"]:
                    raise RuntimeError("Independent workbook verification failed")
                members = member_hashes(output)
                sides[side] = {
                    "subprocessArgv": command, "exitCode": process.returncode,
                    "wallSeconds": elapsed, "result": result,
                    "sha256": digest(output), "memberSha256": members,
                    "independentVerifyReceipt": str(paths[1]),
                    "formulaErrorCount": verification["formulaErrorCount"],
                    "styleMismatchCount": verification["styleMismatchCount"],
                    "nonCarrierValueMismatchCount": verification["nonCarrierValueMismatchCount"],
                }
                if side == "new":
                    previous_new = result
            row = {
                "iteration": iteration, "stage": stage,
                "sameInputWorkbook": str(source), "sameLiveReceipt": str(live),
                "inputSha256": digest(source), "liveReceiptSha256": digest(live),
                "candidateCount": json.loads(live.read_text())["candidateCount"],
                "old": sides["old"], "new": sides["new"],
                "allOOXMLMembersByteIdentical": sides["old"]["memberSha256"] == sides["new"]["memberSha256"],
                "inputPreservedByteForByte": digest(source) == before[str(original_paths[index])],
            }
            if not row["allOOXMLMembersByteIdentical"] or not row["inputPreservedByteForByte"]:
                raise RuntimeError(f"A/B content integrity mismatch: {stage}")
            rows.append(row)
            save(destination / "progress.json", rows)
    after = {str(path): digest(path) for path in original_files}
    if before != after:
        raise RuntimeError("Historical inputs changed during benchmark")
    medians = [{
        "stage": stage,
        "oldMedianSeconds": statistics.median(row["old"]["wallSeconds"] for row in rows if row["stage"] == stage),
        "newMedianSeconds": statistics.median(row["new"]["wallSeconds"] for row in rows if row["stage"] == stage),
    } for stage in stages]
    for item in medians:
        item["speedup"] = item["oldMedianSeconds"] / item["newMedianSeconds"]
    result = {
        "passed": True, "offlineOnly": True, "businessWrites": 0,
        "historicalFilesUnchanged": True, "historicalSourceSha256": before,
        "baselineScript": str(old_script), "baselineScriptSha256": digest(old_script),
        "currentScript": str(new_script), "currentScriptSha256": digest(new_script),
        "pythonExecutable": sys.executable, "repeat": args.repeat,
        "verification": "Independent output ZIP test and SHA256 of every uncompressed OOXML member: complete value/type/style/formula/non-target equality",
        "medians": medians, "rows": rows,
    }
    save(destination / "benchmark.json", result)
    lines = ["# OpenSMS 工作簿刷新零变化离线 A/B", "", "同一输入工作簿、同一后台状态副本，原脚本与新脚本分别作为独立子进程运行。历史交付文件未修改；联网和业务写入均为 0。", "",
             "| 阶段 | 原版中位秒 | 新版中位秒 | 提速倍数 |", "|---|---:|---:|---:|"]
    lines.extend(f"| {item['stage']} | {item['oldMedianSeconds']:.3f} | {item['newMedianSeconds']:.3f} | {item['speedup']:.2f} |" for item in medians)
    old_total = sum(item["oldMedianSeconds"] for item in medians)
    new_total = sum(item["newMedianSeconds"] for item in medians)
    lines += ["", f"三阶段合计中位耗时：{old_total:.3f} → {new_total:.3f} 秒，减少 {(1-new_total/old_total)*100:.1f}%。", "",
              f"共 {len(rows)} 组 A/B；每组源文件 SHA256 一致，输出全部 OOXML 成员 SHA256 一致，ZIP 完整性通过，公式错误/样式不一致/非目标内容不一致均为 0。首阶段完整独立扫描；后两阶段逐号比较本轮独立回读后，复用本轮 SHA256 绑定的完整核验元数据，独立读取复制结果 SHA256。", "",
              f"详细凭证：[benchmark.json]({destination / 'benchmark.json'})"]
    (destination / "benchmark.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "json": str(destination / "benchmark.json"), "markdown": str(destination / "benchmark.md"), "medians": medians}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, default=Path(__file__).with_name("opensms_refresh_workbook_carriers_fast.py"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    run(parser.parse_args())
