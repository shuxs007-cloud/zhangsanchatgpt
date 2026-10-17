#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import ssl
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TERMINAL_STATUSES = {"succeeded", "failed"}


class ApiError(RuntimeError):
    pass


class ApiConnectionError(ApiError):
    pass


class ApiHttpError(ApiError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"接口返回 HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: int, insecure: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def request_json(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request = Request(self.base_url + path, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urlopen(request, timeout=self.timeout, context=self.context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("error", {}).get("message", detail)
            except json.JSONDecodeError:
                message = detail
            raise ApiHttpError(exc.code, message) from exc
        except URLError as exc:
            raise ApiConnectionError(f"无法连接文档处理服务: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ApiConnectionError(f"无法连接文档处理服务: {exc}") from exc

    def submit(
        self,
        export_path: Path,
        association_path: Path,
        server_date: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body, content_type = multipart_body(
            {"server_date": server_date},
            {"export4day": export_path, "association": association_path},
        )
        return self.request_json(
            "POST",
            "/v1/jobs",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Idempotency-Key": idempotency_key,
            },
        )

    def status(self, job_id: str) -> dict[str, Any]:
        return self.request_json("GET", f"/v1/jobs/{job_id}")

    def wait(self, job_id: str, poll_interval: float, wait_timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + wait_timeout
        previous_stage = None
        while True:
            job = self.status(job_id)
            stage = job.get("stage")
            if stage != previous_stage:
                print(
                    json.dumps(
                        {"job_id": job_id, "status": job.get("status"), "stage": stage},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                previous_stage = stage
            if job.get("status") in TERMINAL_STATUSES:
                return job
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待作业超过 {wait_timeout} 秒，可稍后用 job_id 继续查询")
            time.sleep(poll_interval)

    def download(self, job: dict[str, Any], file_key: str, output_dir: Path) -> Path:
        record = job.get("output_files", {}).get(file_key)
        if not record:
            raise RuntimeError(f"远端作业没有文件: {file_key}")
        request = Request(
            self.base_url + f"/v1/jobs/{job['job_id']}/files/{file_key}",
            method="GET",
        )
        request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urlopen(request, timeout=self.timeout, context=self.context) as response:
                payload = response.read()
                header_hash = response.headers.get("X-Content-SHA256")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiHttpError(exc.code, f"下载失败: {detail}") from exc
        except URLError as exc:
            raise ApiConnectionError(f"下载时无法连接文档处理服务: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ApiConnectionError(f"下载时无法连接文档处理服务: {exc}") from exc
        actual_hash = hashlib.sha256(payload).hexdigest()
        expected_hash = record["sha256"]
        if actual_hash != expected_hash or (header_hash and header_hash != expected_hash):
            raise RuntimeError(f"下载文件 SHA-256 不一致: {file_key}")
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = unique_destination(output_dir / record["name"], actual_hash)
        if destination.exists():
            return destination
        destination.write_bytes(payload)
        if sha256_file(destination) != actual_hash:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"下载落盘复核失败: {destination}")
        return destination


def multipart_body(
    fields: dict[str, str], files: dict[str, Path]
) -> tuple[bytes, str]:
    boundary = f"opensms-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, path in files.items():
        filename = path.name.replace('"', "_")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def unique_destination(path: Path, expected_hash: str) -> Path:
    if not path.exists():
        return path
    if path.is_file() and sha256_file(path) == expected_hash:
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def download_many(
    client: ApiClient,
    job: dict[str, Any],
    file_keys: list[str] | tuple[str, ...],
    output_dir: Path,
) -> dict[str, Path]:
    keys = list(file_keys)
    with ThreadPoolExecutor(max_workers=max(1, len(keys))) as executor:
        futures = {
            key: executor.submit(client.download, job, key, output_dir) for key in keys
        }
        return {key: futures[key].result() for key in keys}


def validate_input(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(f"{label} 不存在: {resolved}")
    if resolved.suffix.lower() != ".xlsx":
        raise SystemExit(f"{label} 必须为 .xlsx: {resolved}")
    return resolved


def default_idempotency_key(export: Path, association: Path, server_date: str) -> str:
    digest = hashlib.sha256()
    digest.update(sha256_file(export).encode())
    digest.update(sha256_file(association).encode())
    digest.update(server_date.encode())
    return "opensms-doc-" + digest.hexdigest()


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENSMS_DOC_API_URL", "http://127.0.0.1:8787"),
    )
    parser.add_argument("--token", default=os.environ.get("OPENSMS_DOC_API_TOKEN"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--insecure", action="store_true", help="仅用于可信内网的自签名 HTTPS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提交并下载 OpenSMS 文档处理作业")
    add_connection_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    process_parser = subparsers.add_parser("process", help="上传、等待并下载结果")
    process_parser.add_argument("--export", type=Path, required=True)
    process_parser.add_argument("--association", type=Path, required=True)
    process_parser.add_argument("--server-date", required=True)
    process_parser.add_argument("--output-dir", type=Path, required=True)
    process_parser.add_argument("--idempotency-key")
    process_parser.add_argument("--poll-interval", type=float, default=5.0)
    process_parser.add_argument("--wait-timeout", type=int, default=2400)

    submit_parser = subparsers.add_parser("submit", help="只上传并返回 job_id")
    submit_parser.add_argument("--export", type=Path, required=True)
    submit_parser.add_argument("--association", type=Path, required=True)
    submit_parser.add_argument("--server-date", required=True)
    submit_parser.add_argument("--idempotency-key")

    status_parser = subparsers.add_parser("status", help="查询作业状态")
    status_parser.add_argument("job_id")

    wait_parser = subparsers.add_parser("wait", help="等待作业完成")
    wait_parser.add_argument("job_id")
    wait_parser.add_argument("--poll-interval", type=float, default=5.0)
    wait_parser.add_argument("--wait-timeout", type=int, default=2400)

    download_parser = subparsers.add_parser("download", help="下载已完成作业")
    download_parser.add_argument("job_id")
    download_parser.add_argument("--output-dir", type=Path, required=True)
    download_parser.add_argument(
        "--files",
        nargs="+",
        choices=["result", "receipt", "api_receipt"],
        default=["result", "receipt"],
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.token:
        raise SystemExit("缺少 Token：设置 OPENSMS_DOC_API_TOKEN 或传入 --token")
    client = ApiClient(args.base_url, args.token, args.timeout, args.insecure)

    if args.command in {"process", "submit"}:
        export = validate_input(args.export, "export4day")
        association = validate_input(args.association, "Google关联表")
        key = args.idempotency_key or default_idempotency_key(
            export, association, args.server_date
        )
        job = client.submit(export, association, args.server_date, key)
        if args.command == "submit":
            print_json(job)
            return 0
        job = client.wait(job["job_id"], args.poll_interval, args.wait_timeout)
        if job.get("status") != "succeeded":
            print_json(job)
            return 2
        downloaded = {
            key: str(path)
            for key, path in download_many(
                client, job, ("result", "receipt"), args.output_dir
            ).items()
        }
        print_json({"job": job, "downloaded": downloaded})
        return 0

    if args.command == "status":
        print_json(client.status(args.job_id))
        return 0
    if args.command == "wait":
        job = client.wait(args.job_id, args.poll_interval, args.wait_timeout)
        print_json(job)
        return 0 if job.get("status") == "succeeded" else 2
    if args.command == "download":
        job = client.status(args.job_id)
        if job.get("status") != "succeeded":
            print_json(job)
            return 2
        downloaded = {
            key: str(path)
            for key, path in download_many(
                client, job, tuple(args.files), args.output_dir
            ).items()
        }
        print_json({"job_id": args.job_id, "downloaded": downloaded})
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
