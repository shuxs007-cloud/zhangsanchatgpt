from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def parse_json_output(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(raw):
        if character != "{":
            continue
        try:
            payload, end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if raw[index + end :].strip():
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("处理器未返回可解析的最终 JSON 回执")


class DocumentProcessor:
    def __init__(
        self,
        service_root: Path,
        data_root: Path,
        timeout_seconds: int = 1800,
        processor_command: list[str] | None = None,
    ) -> None:
        self.service_root = service_root.resolve()
        self.data_root = data_root.resolve()
        self.timeout_seconds = timeout_seconds
        self._custom_command = processor_command is not None
        self.processor_command = processor_command or self._default_command()

    @classmethod
    def from_environment(cls, service_root: Path, data_root: Path) -> "DocumentProcessor":
        configured = os.environ.get("OPENSMS_PROCESSOR_COMMAND", "").strip()
        command = shlex.split(configured) if configured else None
        return cls(
            service_root=service_root,
            data_root=data_root,
            timeout_seconds=int(os.environ.get("OPENSMS_PROCESS_TIMEOUT_SECONDS", "1800")),
            processor_command=command,
        )

    def _default_command(self) -> list[str]:
        script = self.service_root / "pipeline" / "build_card_slot_report.sh"
        return [str(script)]

    def prepare_runtime(self) -> None:
        if self._custom_command:
            return
        script = self.service_root / "pipeline" / "build_card_slot_report.sh"
        if not script.is_file():
            raise RuntimeError(f"缺少文档处理入口: {script}")
        script.chmod(script.stat().st_mode | 0o111)

        node_modules = self.service_root / "node_modules"
        if (node_modules / "@oai" / "artifact-tool").is_dir():
            return
        if node_modules.exists() or node_modules.is_symlink():
            raise RuntimeError(f"现有 node_modules 缺少 @oai/artifact-tool: {node_modules}")

        configured = os.environ.get("OPENSMS_NODE_MODULES_DIR", "").strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.append(
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
        )
        node_bin = os.environ.get("NODE_BIN", "").strip()
        if node_bin:
            candidates.append(Path(node_bin).expanduser().resolve().parent.parent / "node_modules")

        for candidate in candidates:
            artifact_tool = candidate / "@oai" / "artifact-tool"
            if artifact_tool.is_dir():
                node_modules.symlink_to(candidate.resolve(), target_is_directory=True)
                return
        raise RuntimeError(
            "找不到 @oai/artifact-tool；请安装 Codex 文档运行时或设置 "
            "OPENSMS_NODE_MODULES_DIR"
        )

    def run(
        self,
        job: dict[str, Any],
        update: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        run_started = time.perf_counter()
        self.prepare_runtime()
        job_id = job["job_id"]
        job_root = (self.data_root / "jobs" / job_id).resolve()
        input_dir = job_root / "input"
        output_dir = job_root / "output"
        snapshot_dir = job_root / "artifacts" / "snapshots"
        log_dir = job_root / "logs"
        for directory in (output_dir, snapshot_dir, log_dir):
            directory.mkdir(parents=True, exist_ok=True)

        export_path = input_dir / "export4day.xlsx"
        association_path = input_dir / "google_card_slots.xlsx"
        result_path = output_dir / f"opensms_card_slot_report_{job_id}.xlsx"
        stdout_path = log_dir / "processor.stdout.log"
        stderr_path = log_dir / "processor.stderr.log"

        command = [
            *self.processor_command,
            str(export_path),
            str(association_path),
            str(output_dir),
            job["server_date"],
        ]
        env = os.environ.copy()
        env.update(
            {
                "OPENSMS_OUTPUT_PATH": str(result_path),
                "OPENSMS_RECEIPT_DIR": str(snapshot_dir),
            }
        )
        update(
            {
                "status": "running",
                "started_at": utc_now(),
                "stage": "processing",
                "processor_command": [Path(command[0]).name, *command[1:]],
            }
        )

        processing_started = time.perf_counter()
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                completed = subprocess.run(
                    command,
                    cwd=self.service_root,
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    check=False,
                    timeout=self.timeout_seconds,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"文档处理超过 {self.timeout_seconds} 秒，已终止"
            ) from exc
        processing_seconds = round(time.perf_counter() - processing_started, 3)

        raw_stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        raw_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            tail = raw_stderr.strip().splitlines()[-8:]
            detail = " | ".join(tail) or f"退出码 {completed.returncode}"
            raise RuntimeError(f"文档处理失败: {detail}")

        receipt = parse_json_output(raw_stdout)
        if receipt.get("result") != "成功" or receipt.get("failed_checks"):
            raise RuntimeError(
                "文档复核未通过: " + ", ".join(receipt.get("failed_checks") or ["未知检查"])
            )
        receipt_path = Path(str(receipt.get("receipt_path", ""))).resolve()
        actual_result = Path(str(receipt.get("output_path", ""))).resolve()
        if actual_result != result_path.resolve() or not actual_result.is_file():
            raise RuntimeError("处理器返回的结果文件不在本作业隔离目录")
        if not self._is_within(receipt_path, snapshot_dir) or not receipt_path.is_file():
            raise RuntimeError("处理器返回的复核回执不在本作业隔离目录")

        update({"stage": "finalizing"})
        finalizing_started = time.perf_counter()
        service_receipt = {
            "api_version": "v1",
            "job_id": job_id,
            "server_date": job["server_date"],
            "created_at": job["created_at"],
            "processed_at": utc_now(),
            "input_files": job["input_files"],
            "processor_receipt": receipt,
            "timings_seconds": {
                "document_pipeline": processing_seconds,
            },
            "task_b_executed": False,
        }
        service_receipt_path = snapshot_dir / "api_processing_receipt.json"
        atomic_write_json(service_receipt_path, service_receipt)

        output_paths = {
            "result": actual_result,
            "receipt": receipt_path,
            "api_receipt": service_receipt_path,
        }
        with ThreadPoolExecutor(max_workers=len(output_paths)) as executor:
            records = executor.map(
                lambda item: (item[0], self._file_record(item[1], job_root)),
                output_paths.items(),
            )
            output_files = dict(records)
        finalizing_seconds = round(time.perf_counter() - finalizing_started, 3)
        total_seconds = round(time.perf_counter() - run_started, 3)
        return {
            "status": "succeeded",
            "stage": "complete",
            "completed_at": utc_now(),
            "output_files": output_files,
            "checks": receipt.get("checks", {}),
            "failed_checks": [],
            "timings_seconds": {
                "document_pipeline": processing_seconds,
                "finalization_and_hashing": finalizing_seconds,
                "total": total_seconds,
                "pipeline_stages": receipt.get("stage_timings_seconds", {}),
            },
            "task_b_executed": False,
            "error": None,
        }

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _file_record(path: Path, job_root: Path) -> dict[str, Any]:
        return {
            "name": path.name,
            "path": path.relative_to(job_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
