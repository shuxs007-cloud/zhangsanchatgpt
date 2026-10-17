#!/usr/bin/env python3
"""Serially restore a prepared rest-zone rollback and independently read it back."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SETTER = ROOT / "opensms_set_carrier_candidates.mjs"
VERIFIER = ROOT / "opensms_verify_carrier_candidates_readonly.mjs"
BITBROWSER_430_PORT_FILE = (
    Path.home()
    / "Library/Application Support/BitBrowser/BrowserCache"
    / "a53ce74e6c9e4fa08aec35f81ce1e2a3"
    / "DevToolsActivePort"
)
FORCE_ENV_KEYS = (
    "OPENSMS_FORCE_TASK_B", "OPENSMS_FORCE_TASK_C", "OPENSMS_FORCE_TASK_D",
    "OPENSMS_FORCE_TASK_E", "OPENSMS_ENABLE_TASK_D", "OPENSMS_EXECUTE_TASK_D",
    "OPENSMS_ENABLE_TASK_E", "OPENSMS_EXECUTE_TASK_E",
)


def run_json(command: list[str], env: dict[str, str], stdout_path: Path, stderr_path: Path) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {}
    return completed.returncode, payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("authorization") != "current_user_explicit":
        raise SystemExit("回滚清单缺少当前用户明确授权标记")
    outputs = manifest.get("outputs") or []
    if sum(int(item.get("count") or 0) for item in outputs) != int(manifest.get("uniquePhoneCount") or 0):
        raise SystemExit("回滚清单分组数量与唯一号码数不一致")

    port_lines = BITBROWSER_430_PORT_FILE.read_text(encoding="utf-8").splitlines()
    if not port_lines or not port_lines[0].isdigit():
        raise SystemExit("BitBrowser 430 DevToolsActivePort 无效")
    env = os.environ.copy()
    for key in FORCE_ENV_KEYS:
        env.pop(key, None)
    env["BITBROWSER_CDP"] = f"http://127.0.0.1:{port_lines[0]}"
    env["OPENSMS_FORCE_RAW_CDP"] = "1"

    output_dir = manifest_path.parent
    group_results: list[dict[str, Any]] = []
    overall_passed = True
    for item in outputs:
        label = str(item["label"])
        candidate = str(item["candidate"])
        target = str(item["targetCarrier"])
        write_stdout = output_dir / f"{label}_write.json"
        write_stderr = output_dir / f"{label}_write.stderr.txt"
        write_code, write_data = run_json(
            ["node", str(SETTER), candidate, target, label],
            env,
            write_stdout,
            write_stderr,
        )
        result: dict[str, Any] = {
            **item,
            "writeExitCode": write_code,
            "writeOutput": str(write_stdout),
            "writeError": str(write_stderr),
            "writeReceipt": write_data.get("path"),
            "validatedCandidate": write_data.get("validatedCandidatePath"),
            "candidateCount": int((write_data.get("before") or {}).get("candidateCount") or 0),
            "eligibleCount": int((write_data.get("before") or {}).get("eligibleCount") or 0),
            "alreadyTargetCount": int((write_data.get("before") or {}).get("alreadyTargetCount") or 0),
            "nonSourceCount": int((write_data.get("before") or {}).get("nonSourceCount") or 0),
            "missingCount": int((write_data.get("before") or {}).get("missingCount") or 0),
            "writeTargetCount": int((write_data.get("after") or {}).get("targetCount") or 0),
            "writeStillNotTargetCount": int((write_data.get("after") or {}).get("stillNotTargetCount") or 0),
        }
        if write_code != 0:
            overall_passed = False
            group_results.append(result)
            break

        verify_stdout = output_dir / f"{label}_verify.json"
        verify_stderr = output_dir / f"{label}_verify.stderr.txt"
        verify_code, verify_data = run_json(
            ["node", str(VERIFIER), candidate, target, f"{label}_independent"],
            env,
            verify_stdout,
            verify_stderr,
        )
        result.update({
            "verifyExitCode": verify_code,
            "verifyOutput": str(verify_stdout),
            "verifyError": str(verify_stderr),
            "verifyReceipt": verify_data.get("path"),
            "verifyTotal": int(verify_data.get("total") or 0),
            "verifyTargetCount": int(verify_data.get("targetCount") or 0),
            "verifyStillNotTargetCount": int(verify_data.get("stillNotTargetCount") or 0),
            "verifyMissingCount": int(verify_data.get("missingCount") or 0),
            "verifyDuplicateBackendCount": int(verify_data.get("duplicateBackendCount") or 0),
            "verifyPassed": verify_code == 0 and verify_data.get("passed") is True,
        })
        group_passed = (
            result["candidateCount"] == int(item["count"])
            and result["nonSourceCount"] == 0
            and result["missingCount"] == 0
            and result["writeStillNotTargetCount"] == 0
            and result["verifyPassed"]
            and result["verifyTargetCount"] == int(item["count"])
        )
        result["passed"] = group_passed
        overall_passed = overall_passed and group_passed
        group_results.append(result)
        if not group_passed:
            break

    expected = int(manifest.get("uniquePhoneCount") or 0)
    verified = sum(item.get("verifyTargetCount", 0) for item in group_results if item.get("verifyPassed"))
    actual_writes = sum(item.get("eligibleCount", 0) for item in group_results)
    already_target = sum(item.get("alreadyTargetCount", 0) for item in group_results)
    completed_all_groups = len(group_results) == len(outputs)
    overall_passed = overall_passed and completed_all_groups and verified == expected
    summary = {
        "status": "success" if overall_passed else "failed",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "rollbackRunId": manifest.get("rollbackRunId"),
        "sourceManifest": str(manifest_path),
        "expectedPhoneCount": expected,
        "groupCount": len(outputs),
        "completedGroupCount": len(group_results),
        "actualWriteCount": actual_writes,
        "alreadyTargetCount": already_target,
        "independentVerifiedCount": verified,
        "groups": group_results,
    }
    summary_path = output_dir / "opensms_rest_zone_rollback_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report_lines = [
        "# OpenSMS 休息区误转回滚报告",
        "",
        f"- 状态：{summary['status']}",
        f"- 原巡检运行编号：`{summary['rollbackRunId']}`",
        f"- 应恢复号码：{expected}",
        f"- 实际写入：{actual_writes}",
        f"- 原本已恢复：{already_target}",
        f"- 独立回读通过：{verified}",
        "",
        "| 休息区来源 | 恢复目标 | 应恢复 | 实际写入 | 已在目标 | 独立回读 | 结果 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in group_results:
        report_lines.append(
            f"| {item['sourceRestCarrier']} | {item['targetCarrier']} | {item['count']} | "
            f"{item.get('eligibleCount', 0)} | {item.get('alreadyTargetCount', 0)} | "
            f"{item.get('verifyTargetCount', 0)} | {'通过' if item.get('passed') else '失败'} |"
        )
    report_lines.extend([
        "",
        "## 范围与安全约束",
        "",
        "- 仅处理指定运行编号当轮写入 E-T/E-C 的备份号码。",
        "- 号码当前不在对应休息区且也不在备份目标时，不覆盖其现状并判定失败。",
        "- 各原运营商分组严格串行写入，每组写入后独立只读回读。",
        "- 任务B、任务C、任务D、任务E均未执行。",
        "",
        f"- 结构化回执：`{summary_path}`",
    ])
    report_path = output_dir / "OpenSMS_休息区误转回滚报告.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(json.dumps({**summary, "summary": str(summary_path), "report": str(report_path)}, ensure_ascii=False, indent=2))
    if not overall_passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
