#!/usr/bin/env python3
"""Explicit bulk-storage migration. No browser, queries, or business writes."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts/opensms_storage_policy.json"


def config(root=ROOT):
    path = root / "scripts/opensms_storage_policy.json"
    return json.loads(path.read_text()) if path.is_file() else None


def require_external(settings):
    volume = Path(settings["externalVolume"])
    info = plistlib.loads(subprocess.check_output(
        ["/usr/sbin/diskutil", "info", "-plist", str(volume)], stderr=subprocess.PIPE))
    if (info.get("MountPoint") != str(volume)
            or info.get("VolumeUUID", "").upper() != settings["externalVolumeUuid"].upper()
            or not volume.is_dir() or not os.path.ismount(volume)
            or not os.access(volume, os.W_OK)):
        raise RuntimeError("zs01 未按配置挂载或不可写；停止，不回退本地磁盘")
    for name in ("bulkRoot", "downloadRoot", "deliveryRoot"):
        path = Path(settings[name])
        if not path.is_relative_to(volume) or path == volume:
            raise RuntimeError("大文件目录超出已核验 zs01")
        path.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(volume):
            raise RuntimeError("大文件目录链接越界")


def manifest(folder):
    result = {}
    for base, dirs, files in os.walk(folder):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink():
                raise RuntimeError(f"迁移源含符号链接，需人工核对: {path}")
        for name in files:
            path = Path(base) / name
            with path.open("rb") as stream:
                result[str(path.relative_to(folder))] = {
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                }
    return result


def verify_copy(target, expected):
    actual = manifest(target)
    if any(actual.get(name) != value for name, value in expected.items()):
        return False
    # macOS generates AppleDouble resource/attribute sidecars on ExFAT.
    # Only genuine, extra AppleDouble files are permitted; every payload byte
    # remains mandatory, including any ._ file that existed in the source.
    for extra in actual.keys() - expected.keys():
        path = target / extra
        with path.open("rb") as stream:
            magic = stream.read(4)
        if not path.name.startswith("._") or magic != b"\x00\x05\x16\x07":
            return False
    return True


def migrate_directory(source, target, receipt_dir, reuse_verified_copy=False):
    """Copy/verify twice, retain old logical paths, delete only verified duplicates."""
    source, target = Path(source), Path(target)
    if source.is_symlink():
        if source.resolve() != target.resolve() or not target.is_dir():
            raise RuntimeError(f"已有链接与策略不一致: {source}")
        return {"source": str(source), "target": str(target), "status": "already_migrated"}
    if not source.is_dir() or (target.exists() and not reuse_verified_copy):
        raise RuntimeError(f"源不存在或目标已存在，拒绝覆盖: {source} -> {target}")
    before = manifest(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copytree(source, target, copy_function=shutil.copy2)
    if not verify_copy(target, before) or manifest(source) != before:
        raise RuntimeError("复制校验失败或源在变化；保留两份，不删除源")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    parked = source.with_name(source.name + ".migration-" + stamp)
    source.rename(parked)
    try:
        source.symlink_to(target, target_is_directory=True)
        if not verify_copy(source, before) or manifest(parked) != before:
            raise RuntimeError("迁移后独立核验失败，保留本地副本")
        record = {"source": str(source), "target": str(target), "status": "verified",
                  "files": before, "bytes": sum(x["bytes"] for x in before.values()),
                  "recovery": "完整数据保存在 target，source 为兼容链接"}
        receipt_dir.mkdir(parents=True, exist_ok=True)
        with (receipt_dir / (stamp + ".json")).open("x") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if source.is_symlink():
            source.unlink()
        if not source.exists():
            parked.rename(source)
        raise
    # Cleanup occurs only after publication and the durable verification receipt.
    # If cleanup fails, keep the verified external link, never restore a partial copy.
    try:
        shutil.rmtree(parked)
    except OSError as error:
        record["cleanupWarning"] = f"本地重复副本未清理完毕: {parked}: {error}"
    return {k: v for k, v in record.items() if k != "files"}


def input_directory(directory, root=ROOT):
    """Keep job/state/logs local; only immutable copied inputs go to zs01."""
    settings = config(root)
    inputs = directory / "inputs"
    if not settings:
        inputs.mkdir()
        return inputs
    require_external(settings)
    if not re.fullmatch(r"\d{8}T\d{6}\+0800", directory.name):
        raise RuntimeError("任务F输入目录运行编号无效")
    target = Path(settings["bulkRoot"]) / "task_f_inputs" / directory.name
    target.mkdir(parents=True, exist_ok=False)
    inputs.symlink_to(target, target_is_directory=True)
    return inputs


def verified_input_root(directory, root=ROOT):
    settings = config(root)
    inputs = directory / "inputs"
    if not inputs.is_symlink():
        return inputs
    if not settings:
        raise RuntimeError("未配置的任务F输入链接")
    require_external(settings)
    expected = (Path(settings["bulkRoot"]) / "task_f_inputs" / directory.name).resolve()
    if (not re.fullmatch(r"\d{8}T\d{6}\+0800", directory.name)
            or inputs.resolve() != expected or not expected.is_dir()):
        raise RuntimeError("任务F输入链接超出本轮固定大文件目录")
    return expected


def output_directory(directory, root=ROOT):
    """Put newly produced F workbooks/snapshots outside, keep receipts local."""
    settings = config(root)
    if not settings:
        return directory
    require_external(settings)
    if not re.fullmatch(r"\d{8}T\d{6}\+0800", directory.name):
        raise RuntimeError("任务F输出目录运行编号无效")
    target = Path(settings["bulkRoot"]) / "task_f_outputs" / directory.name
    target.mkdir(parents=True, exist_ok=False)
    if not target.resolve().is_relative_to(Path(settings["bulkRoot"]).resolve()):
        raise RuntimeError("任务F输出目录链接越界")
    return target


def migration_pairs(settings):
    bulk = Path(settings["bulkRoot"])
    result = [(ROOT / "outputs", bulk / "outputs")]
    result += [(ROOT / "opensms_artifacts" / name, bulk / name)
               for name in ("exports", "reports")]
    jobs = ROOT / "opensms_artifacts/task_f_jobs"
    for directory in sorted(jobs.iterdir()):
        if not directory.is_dir():
            continue
        state = directory / "receipts" / f"opensms_patrol_runner_state_{directory.name}.json"
        if not state.is_file() or json.loads(state.read_text()).get("status") not in ("success", "failed", "complete"):
            raise RuntimeError(f"任务F尚未确认终态，不迁移: {directory.name}")
        if (directory / "inputs").exists():
            result.append((directory / "inputs", bulk / "task_f_inputs" / directory.name))
    return result


def guard(settings):
    require_external(settings)
    bulk = Path(settings["bulkRoot"])
    for source, target in [(ROOT / "outputs", bulk / "outputs"),
                           (ROOT / "opensms_artifacts/exports", bulk / "exports"),
                           (ROOT / "opensms_artifacts/reports", bulk / "reports")]:
        if not source.is_symlink() or source.resolve() != target.resolve() or not target.is_dir():
            raise RuntimeError(f"大文件存储策略尚未完成: {source}")
    return {"status": "passed", "bulkRoot": str(bulk), "coreRoot": str(ROOT),
            "databaseLocal": not (ROOT / "opensms_artifacts/state").is_symlink()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("guard", "download-root", "plan", "migrate"))
    parser.add_argument("--reuse-verified-copy", action="store_true")
    args = parser.parse_args()
    settings = config()
    if not settings:
        raise RuntimeError("存储策略配置缺失")
    require_external(settings)
    if args.command == "download-root":
        guard(settings)
        print(settings["downloadRoot"])
        return
    if args.command == "guard":
        result = guard(settings)
    else:
        pairs = migration_pairs(settings)
        if args.command == "plan":
            result = [{"source": str(a), "target": str(b)} for a, b in pairs]
        else:
            processes = subprocess.check_output(["ps", "-axo", "comm,args"], text=True, errors="replace")
            if any(re.search(r"(?:python|node)", line.split()[0], re.I)
                   and re.search(r"(?:opensms_patrol_runner\.py run|opensms_task_f_background\.py --job|grabandgo_task_f_lookup\.mjs)", line)
                   for line in processes.splitlines() if line.split()):
                raise RuntimeError("仍有 OpenSMS/F 进程，不迁移运行中数据")
            result = [migrate_directory(a, b, ROOT / "recovery-records/storage-migration",
                                       args.reuse_verified_copy) for a, b in pairs]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
