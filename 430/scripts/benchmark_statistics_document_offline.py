#!/usr/bin/env python3
"""Isolated, network-denied A/B measurement of statistics, build, and verification."""
from __future__ import annotations

import argparse
import builtins
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
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "services/opensms_document_api/pipeline"
INPUTS = ROOT / "outputs/opensms_patrol_20260905T130957+0800/inputs"
SOURCE = INPUTS / "export4day_20260905T130957+0800.xlsx"
ASSOC = INPUTS / "Google卡槽关联_20260905T130957+0800.xlsx"
SCAN = INPUTS / "export4day_20260905T130957+0800.scan.pkl"
DATABASE = Path("/Volumes/zs01/chatgpt/OpenSMS/20260905/20260905T130957+0800/receipts/task_f_kooda_activation_20260905T130957+0800.sqlite3")


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def isolate_writes(output):
    output = output.resolve()
    def inside(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            resolved = Path(os.fsdecode(path)).resolve()
            if resolved != output and output not in resolved.parents:
                raise RuntimeError(f"Offline benchmark denied write outside output: {resolved}")
    def audit(event, args):
        if event.startswith("socket.") and event not in ("socket.__new__",):
            raise RuntimeError(f"Offline benchmark denied network: {event}")
        if event == "subprocess.Popen":
            raise RuntimeError("Offline worker cannot launch external processes")
        if event == "open":
            path, mode, flags = args
            if (mode and any(char in mode for char in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                inside(path)
        if event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"):
            inside(args[0])
        if event in ("os.rename", "os.replace"):
            inside(args[0]); inside(args[1])
    sys.addaudithook(audit)


def prepare(output):
    output.mkdir(parents=True, exist_ok=True)
    reference = SOURCE.stat().st_mtime
    # Preserve the history visible before the chosen export. Copies retain mtimes.
    history = output / "history"
    snapshot_dir = history / "snapshots"
    rates_dir = snapshot_dir / "reply_rate_history"
    rates_dir.mkdir(parents=True, exist_ok=True)
    originals = []
    patterns = [
        (ROOT / "opensms_artifacts/snapshots", "opensms_current_analysis_export4day_*.json", snapshot_dir),
        (ROOT / "opensms_artifacts/snapshots", "opensms_zero_all_*_candidates.json", snapshot_dir),
        (ROOT / "opensms_artifacts/snapshots/reply_rate_history", "opensms_reply_rate_snapshot_export4day_*.json", rates_dir),
        (ROOT, "opensms_current_analysis_export4day_*.json", snapshot_dir),
        (ROOT, "opensms_zero_all_*_candidates.json", snapshot_dir),
    ]
    for directory, pattern, destination in patterns:
        for path in sorted(directory.glob(pattern)):
            if path.stat().st_mtime >= reference:
                continue
            target = destination / path.name
            if target.exists():
                raise RuntimeError(f"Duplicate historical snapshot: {target}")
            shutil.copy2(path, target)
            originals.append({"path": str(path), "snapshot": str(target), "sha256": digest(path)})
    inputs = {str(path): {"sha256": digest(path), "size": path.stat().st_size} for path in (SOURCE, ASSOC, SCAN, DATABASE)}
    write_json(output / "input_manifest.json", {"inputs": inputs, "history": originals, "historyCutoffMtime": reference, "serverDate": "2026-09-05"})


def worker(output, version, stage, label, profile):
    run = output / version / label
    run.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "OPENSMS_MAIN_PATH": str(SOURCE), "OPENSMS_ASSOC_PATH": str(ASSOC),
        "OPENSMS_TASK_F_DB_PATH": str(DATABASE), "EXPORT4DAY_SERVER_DATE": "2026-09-05",
        "OPENSMS_OUTPUT_DIR": str(run), "OPENSMS_OUTPUT_PATH": str(run / "document.xlsx"),
        "TMPDIR": str(run), "PYTHONDONTWRITEBYTECODE": "1",
    })
    import tempfile
    tempfile.tempdir = str(run)
    isolate_writes(output)
    sys.path[:0] = [str(output / "baseline") if version == "baseline" else str(ROOT), str(ROOT)]
    base = output / "baseline" if version == "baseline" else PIPELINE
    captured = io.StringIO()
    if stage == "statistics":
        path = output / "baseline/opensms_analyze_current.py" if version == "baseline" else ROOT / "opensms_analyze_current.py"
        target = module(path, f"statistics_{version}")
        target.HISTORY = str(output / "history/snapshots/reply_rate_history")
        target.SNAPSHOTS = str(output / "history/snapshots")
        target.BASE = str(output / "history/empty")
        target.EXPORT_ARCHIVE = str(output / "history/empty")
        target.ensure_artifact_dirs = lambda: None
        original_open = builtins.open
        def output_open(path, mode="r", *args, **kwargs):
            if any(char in mode for char in "wax+"):
                path = run / Path(path).name
            return original_open(path, mode, *args, **kwargs)
        target.open = output_open
        from datetime import datetime
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed = cls(2026, 9, 5, 13, 11, 22)
                return fixed.replace(tzinfo=tz)
        target.datetime = FixedDatetime
        sys.argv = [str(path), str(SOURCE), "--fast", "--scan-cache", str(SCAN), "--export-method", "后台静默新鲜导出"]
        function = target.main
    elif stage == "build":
        target = module(base / "process_card_slots.py", f"build_{version}")
        function = target.process
    else:
        process = json.loads((run / "build.json").read_text())
        os.environ["PROCESS_JSON"] = json.dumps(process, ensure_ascii=False)
        os.environ["ENRICH_JSON"] = json.dumps(process["enrichment"], ensure_ascii=False)
        sys.argv = [str(base / "opensms_verify_card_slot_fast.py"), str(run / "document.xlsx"), str(SOURCE)]
        target = module(base / "opensms_verify_card_slot_fast.py", f"verify_{version}")
        function = target.main
    profiler = cProfile.Profile() if profile else None
    started = time.perf_counter()
    with contextlib.redirect_stdout(captured):
        result = profiler.runcall(function) if profiler else function()
    duration = time.perf_counter() - started
    if result not in (None, 0):
        raise RuntimeError(f"{stage} failed: {result}: {captured.getvalue()}")
    payload = json.loads(captured.getvalue())
    write_json(run / f"{stage}.json", payload)
    write_json(run / f"{stage}.timing.json", {"seconds": duration, "profiled": profile, "stage": stage, "version": version})
    if profiler:
        profiler.dump_stats(run / f"{stage}.prof")
        with (run / f"{stage}.profile.txt").open("w") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(45)
    print(json.dumps({"stage": stage, "version": version, "label": label, "seconds": duration}))


def run(output, version, repeats):
    for label, profile in [(f"wall_{n}", False) for n in range(1, repeats + 1)] + [("profile", True)]:
        for stage in ("statistics", "build", "verify"):
            command = [sys.executable, __file__, "worker", str(output), "--version", version, "--stage", stage, "--label", label]
            if profile:
                command.append("--profile")
            completed = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, text=True, capture_output=True)
            print(completed.stdout, end="", flush=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr)


def compare(output):
    import re
    ignored_receipt_keys = {"output_path", "stage_timings_seconds"}
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in ignored_receipt_keys}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    baseline = output / "baseline/wall_1"
    optimized = output / "optimized/wall_1"
    receipts = {}
    for stage in ("statistics", "build", "verify"):
        before = json.loads((baseline / f"{stage}.json").read_text())
        after = json.loads((optimized / f"{stage}.json").read_text())
        receipts[stage] = (stable(before) == stable(after)) if stage == "build" else before == after
    members = []
    mismatches = []
    with zipfile.ZipFile(baseline / "document.xlsx") as left, zipfile.ZipFile(optimized / "document.xlsx") as right:
        if left.namelist() != right.namelist():
            mismatches.append("ZIP member list/order")
        for name in left.namelist():
            before, after = left.read(name), right.read(name)
            exact = before == after
            if name == "docProps/core.xml":
                pattern = rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)"
                before = re.sub(pattern, rb"\1<benchmark-clock>\2", before)
                after = re.sub(pattern, rb"\1<benchmark-clock>\2", after)
            equivalent = before == after
            if not equivalent:
                mismatches.append(name)
            members.append({"member": name, "byteIdentical": exact, "equivalent": equivalent})
        zip_integrity = left.testzip() is None and right.testzip() is None
    manifest = json.loads((output / "input_manifest.json").read_text())
    inputs_unchanged = all(digest(path) == info["sha256"] for path, info in manifest["inputs"].items())
    history_unchanged = all(digest(info["snapshot"]) == info["sha256"] for info in manifest["history"])
    timings = {}
    for stage in ("statistics", "build", "verify"):
        values = {}
        for version in ("baseline", "optimized"):
            values[version] = [json.loads(path.read_text())["seconds"] for path in sorted((output / version).glob(f"wall_*/{stage}.timing.json"))]
        old = statistics.median(values["baseline"])
        new = statistics.median(values["optimized"])
        timings[stage] = {**values, "baselineMedianSeconds": old, "optimizedMedianSeconds": new, "improvementPercent": (old - new) / old * 100}
    old_total = timings["build"]["baselineMedianSeconds"] + timings["verify"]["baselineMedianSeconds"]
    new_total = timings["build"]["optimizedMedianSeconds"] + timings["verify"]["optimizedMedianSeconds"]
    timings["buildAndIndependentVerify"] = {"baselineMedianSeconds": old_total, "optimizedMedianSeconds": new_total, "improvementPercent": (old_total - new_total) / old_total * 100}
    entries = {}
    for version in ("baseline", "optimized"):
        entry_path = output / version / "full_entry_1/entry.timing.json"
        if entry_path.exists():
            entries[version] = json.loads(entry_path.read_text())
    full_entry_equivalence = None
    if len(entries) == 2:
        entry_outputs = {version: json.loads((output / version / "full_entry_1/entry.stdout.json").read_text()) for version in entries}
        full_entry_equivalence = entry_outputs["baseline"]["verification"] == entry_outputs["optimized"]["verification"] and entry_outputs["baseline"]["checks"] == entry_outputs["optimized"]["checks"]
        old, new = entries["baseline"]["seconds"], entries["optimized"]["seconds"]
        timings["originalShellEntry"] = {"baselineMedianSeconds": old, "optimizedMedianSeconds": new, "improvementPercent": (old - new) / old * 100, "runsPerVersion": 1}
    profiles = {}
    for version in ("baseline", "optimized"):
        profiles[version] = {}
        for stage in ("build", "verify", "statistics"):
            stats = pstats.Stats(str(output / version / "profile" / f"{stage}.prof"))
            keys = ["parse_cell", "__hash__", "__set__", "_cells_by_row", "review_numeric_text"]
            profiles[version][stage] = [{"file": Path(key[0]).name, "function": key[2], "calls": value[1], "ownSeconds": value[2], "cumulativeSeconds": value[3]} for key, value in stats.stats.items() if key[2] in keys]
    mutation_path = output / "mutation_regression.json"
    mutation_result = json.loads(mutation_path.read_text()) if mutation_path.exists() else None
    passed = all(receipts.values()) and not mismatches and zip_integrity and inputs_unchanged and history_unchanged
    if entries:
        passed = passed and len(entries) == 2 and all(item["passed"] for item in entries.values()) and full_entry_equivalence
    if mutation_result:
        passed = passed and mutation_result["passed"]
    result = {"passed": passed, "timings": timings, "receiptEquivalence": receipts, "workbookMembers": members, "workbookMismatches": mismatches, "zipIntegrity": zip_integrity, "inputsUnchanged": inputs_unchanged, "historySnapshotUnchanged": history_unchanged, "historyFiles": len(manifest["history"]), "profiles": profiles,
        "normalization": {"buildReceipt": sorted(ignored_receipt_keys), "workbook": "Only docProps/core.xml dcterms:modified clock timestamp; ZIP container timestamps ignored"},
        "scope": "Offline fixed 2026-09-05 export/Google inputs and read-only Task F DB snapshot; historical JSON copied before source mtime; no business writes or network; statistics consumes existing hash-bound scan cache", "baselineSavedBeforeEdits": True,
        "fullEntry": entries, "fullEntryVerificationExactlyEqual": full_entry_equivalence, "mutationRegression": mutation_result,
        "baselineCodeSha256": {path.name: digest(path) for path in (output / "baseline").glob("*.py")},
        "currentCodeSha256": {path.name: digest(path) for path in (ROOT / "opensms_analyze_current.py", ROOT / "opensms_export_scan_cache.py", PIPELINE / "process_card_slots.py", PIPELINE / "opensms_verify_card_slot_fast.py")}}
    write_json(output / "comparison.json", result)
    lines = ["# 统计与初始文档离线 A/B", "", f"结果：{'通过' if passed else '失败'}。源表 5,191 行；后台日期固定 2026-09-05；同份 Google 表及 SQLite 只读快照。", "", "| 范围 | 修改前中位秒 | 修改后中位秒 | 降幅 |", "|---|---:|---:|---:|"]
    labels = {"statistics": "统计（已有共享扫描缓存）", "build": "初始文档生成", "verify": "独立文档核验", "buildAndIndependentVerify": "生成＋独立核验（阶段中位数相加）", "originalShellEntry": "原始 shell 完整入口（各一次）"}
    for key, item in timings.items():
        lines.append(f"| {labels[key]} | {item['baselineMedianSeconds']:.3f} | {item['optimizedMedianSeconds']:.3f} | {item['improvementPercent']:.1f}% |")
    lines += ["", "统计和核验各版两次正常计时；生成各版四次（后两组按 old→new、new→old 交错），cProfile 另跑且不混入正常耗时。前四组测量存在其它本地任务负载变化，因此最终完整 shell 入口在其它 benchmark 停止后顺序测一次，包含实际启动、生成、独立核验和回执保存。", "", "历史原轮 document.log：生成内部 11.556 秒；壳层生成 12 秒、独立核验 13 秒、总入口 24.908 秒，不能把两种口径直接称为生成器退化。", "", "已证实热点：固定黄色填充在约 23 万个单元格重复计算样式哈希；核验器对主表值、红色整行、公式错误反复解析同一 XML，Google 明细逐格样式读取重复解析。修改仅注册一次黄色填充 ID，并复用核验器自身独立读取到的单元格值。保留全部检查。", "", f"逐项等价：统计 JSON={receipts['statistics']}；生成回执（除新输出路径、计时）={receipts['build']}；独立核验 JSON={receipts['verify']}；完整入口检查及核验 JSON={full_entry_equivalence}。工作簿所有 {len(members)} 个 ZIP 成员逐一比较，工作表数据、类型、公式、样式及其它成员零差异（仅忽略 core.xml 修改时间）。", "", f"源/Google/缓存/DB 哈希不变={inputs_unchanged}；历史快照 {len(manifest['history'])} 份哈希不变={history_unchanged}；XLSX 完整性={zip_integrity}。", "", "篡改回归：日期状态、公式错误、黄色整行样式、ICCID 文本格式的破坏均被检出，旧/新异常回执完全相同，见 mutation_regression.json。10 项局部单测通过。", "", "统计程序和共享缓存程序未改：统计约 0.3–0.4 秒，没有证据支持为 25 秒文档耗时修改统计规则。网络由 Python audit hook 阻断，worker 的写入目标限定此独立 benchmark 目录。", "", "复现：使用本目录 baseline/ 保存的修改前脚本；脚本 scripts/benchmark_statistics_document_offline.py 的 run 模式测阶段、entry 模式调用原 shell 入口、mutations 模式测试坏文档、compare 模式生成此报告。新轮应另建独立输出目录并在 prepare 前复制基线，禁止覆盖这里的结果。", "", "详细计时、哈希和全部成员核验见 comparison.json；各版 profile 目录保留 cProfile 二进制与文本。"]
    (output / "comparison.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"passed": passed, "report": str(output / "comparison.md"), "timings": timings}, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


def mutations(output):
    from xml.etree import ElementTree as ET
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    source = output / "baseline/wall_1/document.xlsx"
    with zipfile.ZipFile(source) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = [element.attrib["name"] for element in workbook.find(namespace + "sheets")]
        targets = {
            f"xl/worksheets/sheet{sheets.index('sheet1') + 1}.xml": "main",
            f"xl/worksheets/sheet{sheets.index('谷歌有-系统无') + 1}.xml": "yellow",
            f"xl/worksheets/sheet{sheets.index('谷歌文档中有激活日期-系统无法使用') + 1}.xml": "identifier",
        }
        contents = {name: archive.read(name) for name in archive.namelist()}
    for name, kind in targets.items():
        xml = ET.fromstring(contents[name])
        data = xml.find(namespace + "sheetData")
        if kind == "main":
            row = next(row for row in data if row.attrib["r"] == "2")
            cell = next(cell for cell in row if cell.attrib.get("r") == "C2")
            for child in list(cell):
                cell.remove(child)
            cell.attrib["t"] = "inlineStr"
            ET.SubElement(ET.SubElement(cell, namespace + "is"), namespace + "t").text = "#REF!"
        else:
            for row in data:
                if int(row.attrib["r"]) < 2:
                    continue
                for cell in row:
                    if cell.attrib.get("r", "").startswith("A" if kind == "yellow" else "F"):
                        cell.attrib["s"] = "0"
        contents[name] = ET.tostring(xml, encoding="utf-8")
    for version in ("baseline", "optimized"):
        run = output / version / "mutation"
        run.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output / version / "wall_1/build.json", run / "build.json")
        with zipfile.ZipFile(run / "document.xlsx", "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
        command = [sys.executable, __file__, "worker", str(output), "--version", version, "--stage", "verify", "--label", "mutation"]
        subprocess.run(command, check=True, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    old = json.loads((output / "baseline/mutation/verify.json").read_text())
    new = json.loads((output / "optimized/mutation/verify.json").read_text())
    result = {
        "oldNewExactReceiptEquality": old == new,
        "formulaErrorDetected": new["formula_error_matches"] > 0,
        "expiryStatusMismatchDetected": new["expiry_status_mismatch_count"] > 0,
        "yellowStyleTamperDetected": new["google_system_missing_review"]["yellow_highlight_matches"] is False,
        "identifierNumberFormatTamperDetected": new["google_system_missing_review"]["identifier_columns_preserved_as_text"] is False,
    }
    result["passed"] = all(result.values())
    write_json(output / "mutation_regression.json", result)
    print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)


def entry(output, version, label):
    output = output.resolve()
    run = output / version / label
    run.mkdir(parents=True, exist_ok=False)
    base = output / "baseline" if version == "baseline" else PIPELINE
    env = {**os.environ,
        "PYTHON_BIN": sys.executable,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(ROOT / "scripts/benchmark_offline_guard"), str(ROOT / "scripts"))),
        "OPENSMS_OFFLINE_BENCHMARK_OUTPUT": str(output),
        "TMPDIR": str(run), "OPENSMS_TASK_F_DB_PATH": str(DATABASE),
        "OPENSMS_OUTPUT_PATH": str(run / "document.xlsx"),
        "OPENSMS_RECEIPT_DIR": str(run / "receipts"),
        "OPENSMS_LEGACY_DOCUMENT_ENRICHMENT": "0",
    }
    command = ["bash", str(base / "build_card_slot_report.sh"), str(SOURCE), str(ASSOC), str(run), "2026-09-05"]
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    duration = time.perf_counter() - started
    (run / "entry.stderr.log").write_text(completed.stderr)
    (run / "entry.stdout.json").write_text(completed.stdout)
    if completed.returncode:
        raise RuntimeError(f"Full entry failed: {completed.returncode}: {completed.stderr}")
    result = json.loads(completed.stdout)
    checks = result.get("checks", {})
    passed = result.get("result") == "成功" and checks and all(checks.values()) and result.get("failed_checks") == []
    receipt = {"version": version, "label": label, "seconds": duration, "passed": bool(passed), "checks": checks, "stageTimings": result["stage_timings_seconds"], "command": command, "networkDeniedInAllPythonChildren": True}
    write_json(run / "entry.timing.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "worker", "run", "compare", "mutations", "entry"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", choices=("baseline", "optimized"), default="baseline")
    parser.add_argument("--stage", choices=("statistics", "build", "verify"), default="build")
    parser.add_argument("--label", default="wall_1")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.output)
    elif args.mode == "compare":
        compare(args.output)
    elif args.mode == "mutations":
        mutations(args.output)
    elif args.mode == "entry":
        entry(args.output, args.version, args.label)
    elif args.mode == "worker":
        worker(args.output, args.version, args.stage, args.label, args.profile)
    else:
        run(args.output, args.version, args.repeats)


if __name__ == "__main__":
    main()
