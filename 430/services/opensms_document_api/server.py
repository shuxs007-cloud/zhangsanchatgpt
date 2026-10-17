#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import logging
import os
import queue
import re
import ssl
import tempfile
import threading
import uuid
import zipfile
from datetime import date
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from processor import DocumentProcessor, atomic_write_json, sha256_file, utc_now


LOG = logging.getLogger("opensms_document_api")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SERVER_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")


def resolve_worker_count(value: str | int | None) -> int:
    raw = str(value if value is not None else "auto").strip().lower()
    if raw in {"auto", "max", "0"}:
        return max(1, os.cpu_count() or 1)
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("--workers 必须是正整数或 auto") from exc
    if workers < 1:
        raise ValueError("--workers 必须大于等于 1")
    return workers


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class JobManager:
    def __init__(self, data_root: Path, processor: DocumentProcessor, worker_count: int = 1) -> None:
        self.data_root = data_root.resolve()
        self.jobs_root = self.data_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.processor = processor
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self.worker_count = max(1, worker_count)
        self._load_existing()
        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"document-worker-{index + 1}",
                daemon=True,
            )
            for index in range(self.worker_count)
        ]
        for worker in self._workers:
            worker.start()

    def _load_existing(self) -> None:
        for metadata_path in sorted(self.jobs_root.glob("*/job.json")):
            try:
                job = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                LOG.warning("忽略损坏的作业元数据: %s", metadata_path)
                continue
            job_id = str(job.get("job_id", ""))
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            if job.get("status") == "running":
                job.update(
                    {
                        "status": "failed",
                        "stage": "interrupted",
                        "completed_at": utc_now(),
                        "error": "服务重启时作业仍在运行，请使用新的 Idempotency-Key 重新提交",
                    }
                )
                atomic_write_json(metadata_path, job)
            self._jobs[job_id] = job
            key = job.get("idempotency_key")
            if key:
                self._idempotency[str(key)] = job_id
            if job.get("status") == "queued":
                self._queue.put(job_id)

    def create_job(
        self,
        export_payload: bytes,
        export_name: str,
        association_payload: bytes,
        association_name: str,
        server_date: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        input_hashes = {
            "export4day": hashlib.sha256(export_payload).hexdigest(),
            "association": hashlib.sha256(association_payload).hexdigest(),
        }
        with self._lock:
            if idempotency_key and idempotency_key in self._idempotency:
                existing = self._jobs[self._idempotency[idempotency_key]]
                same = (
                    existing.get("server_date") == server_date
                    and existing.get("input_files", {}).get("export4day", {}).get("sha256")
                    == input_hashes["export4day"]
                    and existing.get("input_files", {}).get("association", {}).get("sha256")
                    == input_hashes["association"]
                )
                if not same:
                    raise ApiProblem(
                        HTTPStatus.CONFLICT,
                        "idempotency_conflict",
                        "相同 Idempotency-Key 已用于不同输入",
                    )
                return dict(existing), False

            job_id = uuid.uuid4().hex
            job_root = self.jobs_root / job_id
            input_dir = job_root / "input"
            input_dir.mkdir(parents=True)
            export_path = input_dir / "export4day.xlsx"
            association_path = input_dir / "google_card_slots.xlsx"
            self._atomic_write_bytes(export_path, export_payload)
            self._atomic_write_bytes(association_path, association_payload)
            job = {
                "api_version": "v1",
                "job_id": job_id,
                "status": "queued",
                "stage": "queued",
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "server_date": server_date,
                "idempotency_key": idempotency_key,
                "task_b_executed": False,
                "input_files": {
                    "export4day": {
                        "original_name": export_name,
                        "stored_name": export_path.name,
                        "size": len(export_payload),
                        "sha256": input_hashes["export4day"],
                    },
                    "association": {
                        "original_name": association_name,
                        "stored_name": association_path.name,
                        "size": len(association_payload),
                        "sha256": input_hashes["association"],
                    },
                },
                "output_files": {},
                "checks": {},
                "failed_checks": [],
                "error": None,
            }
            self._jobs[job_id] = job
            if idempotency_key:
                self._idempotency[idempotency_key] = job_id
            self._save(job)
            self._queue.put(job_id)
            return dict(job), True

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ApiProblem(HTTPStatus.NOT_FOUND, "job_not_found", "作业不存在")
            return dict(job)

    def file_for(self, job_id: str, file_key: str) -> tuple[Path, dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ApiProblem(HTTPStatus.NOT_FOUND, "job_not_found", "作业不存在")
            if job.get("status") != "succeeded":
                raise ApiProblem(HTTPStatus.CONFLICT, "job_not_ready", "作业尚未成功完成")
            record = job.get("output_files", {}).get(file_key)
            if not record:
                raise ApiProblem(HTTPStatus.NOT_FOUND, "file_not_found", "作业文件不存在")
            job_root = (self.jobs_root / job_id).resolve()
            path = (job_root / record["path"]).resolve()
            try:
                path.relative_to(job_root)
            except ValueError as exc:
                raise ApiProblem(HTTPStatus.INTERNAL_SERVER_ERROR, "unsafe_path", "作业文件路径异常") from exc
            if not path.is_file():
                raise ApiProblem(HTTPStatus.GONE, "file_missing", "作业文件已丢失")
            if sha256_file(path) != record["sha256"]:
                raise ApiProblem(HTTPStatus.INTERNAL_SERVER_ERROR, "checksum_mismatch", "作业文件校验失败")
            return path, dict(record)

    def public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        result = dict(job)
        result.pop("processor_command", None)
        result["links"] = {
            "self": f"/v1/jobs/{job_id}",
            "files": f"/v1/jobs/{job_id}/files",
        }
        if job.get("status") == "succeeded":
            result["links"].update(
                {
                    "result": f"/v1/jobs/{job_id}/files/result",
                    "receipt": f"/v1/jobs/{job_id}/files/receipt",
                    "api_receipt": f"/v1/jobs/{job_id}/files/api_receipt",
                }
            )
        return result

    def concurrency(self) -> dict[str, int]:
        with self._lock:
            counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
            for job in self._jobs.values():
                status = str(job.get("status", ""))
                if status in counts:
                    counts[status] += 1
        return {
            "workers": self.worker_count,
            "queue_depth": self._queue.qsize(),
            **counts,
        }

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                job = self.get(job_id)
                result = self.processor.run(job, lambda patch: self._update(job_id, patch))
                self._update(job_id, result)
            except Exception as exc:  # noqa: BLE001 - persist all processor failures
                LOG.exception("作业 %s 失败", job_id)
                self._update(
                    job_id,
                    {
                        "status": "failed",
                        "stage": "failed",
                        "completed_at": utc_now(),
                        "error": str(exc),
                        "task_b_executed": False,
                    },
                )
            finally:
                self._queue.task_done()

    def _update(self, job_id: str, patch: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(patch)
            self._save(job)

    def _save(self, job: dict[str, Any]) -> None:
        atomic_write_json(self.jobs_root / job["job_id"] / "job.json", job)

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temporary_path = Path(handle.name)
        temporary_path.replace(path)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        manager: JobManager,
        token: str | None,
        max_upload_bytes: int,
    ) -> None:
        super().__init__(address, ApiHandler)
        self.manager = manager
        self.token = token
        self.max_upload_bytes = max_upload_bytes


class ApiHandler(BaseHTTPRequestHandler):
    server: ApiServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "opensms-document-api",
                        "api_version": "v1",
                        "task_b_supported": False,
                        "concurrency": self.server.manager.concurrency(),
                    },
                )
                return
            self._require_auth()
            match = re.fullmatch(r"/v1/jobs/([0-9a-f]{32})", path)
            if match:
                job = self.server.manager.get(match.group(1))
                self._send_json(HTTPStatus.OK, self.server.manager.public_job(job))
                return
            match = re.fullmatch(r"/v1/jobs/([0-9a-f]{32})/files", path)
            if match:
                job = self.server.manager.get(match.group(1))
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "job_id": job["job_id"],
                        "status": job["status"],
                        "files": job.get("output_files", {}),
                    },
                )
                return
            match = re.fullmatch(
                r"/v1/jobs/([0-9a-f]{32})/files/(result|receipt|api_receipt)",
                path,
            )
            if match:
                self._send_file(match.group(1), match.group(2))
                return
            raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
        except ApiProblem as problem:
            self._send_problem(problem)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("GET 处理失败")
            self._send_problem(ApiProblem(500, "internal_error", str(exc)))

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlsplit(self.path).path
            self._require_auth()
            if path != "/v1/jobs":
                raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
            fields, files = self._read_multipart()
            server_date = fields.get("server_date", "").strip()
            self._validate_server_date(server_date)
            export_name, export_payload = self._required_upload(files, "export4day")
            association_name, association_payload = self._required_upload(files, "association")
            self._validate_xlsx(export_payload, "export4day")
            self._validate_xlsx(association_payload, "association")
            idempotency_key = self.headers.get("Idempotency-Key", "").strip() or None
            if idempotency_key and len(idempotency_key) > 128:
                raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_idempotency_key", "Idempotency-Key 最长 128 字符")
            job, created = self.server.manager.create_job(
                export_payload,
                export_name,
                association_payload,
                association_name,
                server_date,
                idempotency_key,
            )
            status = HTTPStatus.ACCEPTED if created else HTTPStatus.OK
            self._send_json(
                status,
                self.server.manager.public_job(job),
                extra_headers={"Location": f"/v1/jobs/{job['job_id']}"},
            )
        except ApiProblem as problem:
            self._send_problem(problem)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("POST 处理失败")
            self._send_problem(ApiProblem(500, "internal_error", str(exc)))

    def _require_auth(self) -> None:
        expected = self.server.token
        if expected is None:
            return
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise ApiProblem(HTTPStatus.UNAUTHORIZED, "unauthorized", "Bearer Token 无效")

    def _read_multipart(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise ApiProblem(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type", "必须使用 multipart/form-data")
        raw_length = self.headers.get("Content-Length")
        if not raw_length or not raw_length.isdigit():
            raise ApiProblem(HTTPStatus.LENGTH_REQUIRED, "length_required", "缺少 Content-Length")
        content_length = int(raw_length)
        if content_length <= 0 or content_length > self.server.max_upload_bytes:
            raise ApiProblem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "upload_too_large", "上传内容超过服务限制")
        body = self.rfile.read(content_length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
        )
        if not message.is_multipart():
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_multipart", "无法解析上传内容")
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in message.iter_parts():
            disposition = part.get("Content-Disposition", "")
            if not disposition.startswith("form-data"):
                continue
            name = part.get_param("name", header="Content-Disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if not name:
                continue
            if filename is not None:
                files[name] = (Path(filename).name or f"{name}.xlsx", payload)
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8")
        return fields, files

    @staticmethod
    def _required_upload(files: dict[str, tuple[str, bytes]], name: str) -> tuple[str, bytes]:
        value = files.get(name)
        if not value or not value[1]:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "missing_file", f"缺少上传文件字段: {name}")
        return value

    @staticmethod
    def _validate_server_date(value: str) -> None:
        if not SERVER_DATE_RE.fullmatch(value):
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_server_date", "server_date 必须为 YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_server_date", "server_date 不是有效日期") from exc

    @staticmethod
    def _validate_xlsx(payload: bytes, label: str) -> None:
        stream = io.BytesIO(payload)
        if not zipfile.is_zipfile(stream):
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_xlsx", f"{label} 不是有效 XLSX")
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_xlsx", f"{label} 缺少工作簿结构")
                if archive.testzip() is not None:
                    raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_xlsx", f"{label} ZIP 校验失败")
        except zipfile.BadZipFile as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_xlsx", f"{label} ZIP 损坏") from exc

    def _send_file(self, job_id: str, file_key: str) -> None:
        path, record = self.server.manager.file_for(job_id, file_key)
        mime = "application/json" if path.suffix.lower() == ".json" else "application/octet-stream"
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", record["name"]) or "download.bin"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(record["size"]))
        self.send_header("X-Content-SHA256", record["sha256"])
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(record['name'])}",
        )
        self.end_headers()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                self.wfile.write(chunk)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_problem(self, problem: ApiProblem) -> None:
        self._send_json(
            problem.status,
            {"error": {"code": problem.code, "message": problem.message}},
        )

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format_string % args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenSMS 文档异步处理接口")
    parser.add_argument("--host", default=os.environ.get("OPENSMS_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENSMS_API_PORT", "8787")))
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("OPENSMS_API_DATA_ROOT", "./data")),
    )
    parser.add_argument("--token", default=os.environ.get("OPENSMS_DOC_API_TOKEN"))
    parser.add_argument("--allow-unauthenticated", action="store_true")
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=int(os.environ.get("OPENSMS_MAX_UPLOAD_MB", "100")),
    )
    parser.add_argument(
        "--workers",
        default=os.environ.get("OPENSMS_DOCUMENT_WORKERS", "auto"),
        help="并发作业数；auto 使用全部逻辑 CPU",
    )
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--log-level", default=os.environ.get("OPENSMS_API_LOG_LEVEL", "INFO"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.token and not args.allow_unauthenticated:
        raise SystemExit("必须设置 OPENSMS_DOC_API_TOKEN，或显式使用 --allow-unauthenticated")
    if bool(args.tls_cert) != bool(args.tls_key):
        raise SystemExit("--tls-cert 与 --tls-key 必须同时提供")
    service_root = Path(__file__).resolve().parent
    data_root = args.data_root.expanduser().resolve()
    processor = DocumentProcessor.from_environment(service_root, data_root)
    processor.prepare_runtime()
    try:
        worker_count = resolve_worker_count(args.workers)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    manager = JobManager(data_root, processor, worker_count=worker_count)
    server = ApiServer(
        (args.host, args.port),
        manager=manager,
        token=args.token,
        max_upload_bytes=args.max_upload_mb * 1024 * 1024,
    )
    scheme = "http"
    if args.tls_cert and args.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    LOG.info("OpenSMS 文档接口已启动: %s://%s:%s", scheme, args.host, args.port)
    LOG.info("数据目录: %s", data_root)
    LOG.info("并发文档作业数: %s（逻辑 CPU: %s）", worker_count, os.cpu_count() or 1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到停止信号")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
