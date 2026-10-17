from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from io import BytesIO
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from processor import DocumentProcessor  # noqa: E402
from server import ApiServer, JobManager, resolve_worker_count  # noqa: E402

CLIENT_PATH = SERVICE_ROOT / "opensms_document_api_client.py"
if not CLIENT_PATH.is_file():
    CLIENT_PATH = SERVICE_ROOT.parents[1] / "scripts" / "opensms_document_api_client.py"
CLIENT_SPEC = importlib.util.spec_from_file_location("opensms_document_api_client", CLIENT_PATH)
assert CLIENT_SPEC and CLIENT_SPEC.loader
CLIENT_MODULE = importlib.util.module_from_spec(CLIENT_SPEC)
CLIENT_SPEC.loader.exec_module(CLIENT_MODULE)

DISPATCH_PATH = SERVICE_ROOT / "opensms_process_document.py"
if not DISPATCH_PATH.is_file():
    DISPATCH_PATH = SERVICE_ROOT.parents[1] / "scripts" / "opensms_process_document.py"
sys.path.insert(0, str(DISPATCH_PATH.parent))
DISPATCH_SPEC = importlib.util.spec_from_file_location("opensms_process_document", DISPATCH_PATH)
assert DISPATCH_SPEC and DISPATCH_SPEC.loader
DISPATCH_MODULE = importlib.util.module_from_spec(DISPATCH_SPEC)
DISPATCH_SPEC.loader.exec_module(DISPATCH_MODULE)


TOKEN = "test-token"


def minimal_xlsx(marker: str = "test") -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        archive.writestr("xl/workbook.xml", f"<workbook><marker>{marker}</marker></workbook>")
    return output.getvalue()


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "test-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode())
        chunks.append(b"\r\n")
    for name, (filename, payload) in files.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n")
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class DocumentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        fake_processor = root / "fake_processor.py"
        fake_processor.write_text(
            """
import json
import os
import shutil
import sys
from pathlib import Path

export_path, association_path, output_dir, server_date = sys.argv[1:]
result_path = Path(os.environ["OPENSMS_OUTPUT_PATH"])
receipt_dir = Path(os.environ["OPENSMS_RECEIPT_DIR"])
result_path.parent.mkdir(parents=True, exist_ok=True)
receipt_dir.mkdir(parents=True, exist_ok=True)
shutil.copy2(export_path, result_path)
receipt_path = receipt_dir / "processor_receipt.json"
payload = {
    "result": "成功",
    "output_path": str(result_path),
    "receipt_path": str(receipt_path),
    "checks": {"表头": True},
    "failed_checks": [],
    "task_b_executed": False,
}
receipt_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
""".lstrip(),
            encoding="utf-8",
        )
        processor = DocumentProcessor(
            SERVICE_ROOT,
            root / "data",
            timeout_seconds=30,
            processor_command=[sys.executable, str(fake_processor)],
        )
        self.manager = JobManager(root / "data", processor)
        self.server = ApiServer(("127.0.0.1", 0), self.manager, TOKEN, 10 * 1024 * 1024)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ):
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        if authenticated:
            request.add_header("Authorization", f"Bearer {TOKEN}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        return urllib.request.urlopen(request, timeout=10)

    def submit(self, key: str, association: bytes | None = None) -> dict:
        body, content_type = multipart(
            {"server_date": "2026-08-19"},
            {
                "export4day": ("export4day.xlsx", minimal_xlsx("export")),
                "association": ("google.xlsx", association or minimal_xlsx("google")),
            },
        )
        with self.request(
            "POST",
            "/v1/jobs",
            body,
            {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Idempotency-Key": key,
            },
        ) as response:
            return json.loads(response.read())

    def wait_for_job(self, job_id: str) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.request("GET", f"/v1/jobs/{job_id}") as response:
                job = json.loads(response.read())
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_health_is_public_and_jobs_require_authentication(self) -> None:
        with self.request("GET", "/healthz", authenticated=False) as response:
            self.assertEqual(200, response.status)
            health = json.loads(response.read())
            self.assertFalse(health["task_b_supported"])
            self.assertEqual(1, health["concurrency"]["workers"])
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("GET", "/v1/jobs/" + "0" * 32, authenticated=False)
        self.assertEqual(401, context.exception.code)

    def test_submit_process_and_download_minimal_outputs(self) -> None:
        submitted = self.submit("same-input")
        job = self.wait_for_job(submitted["job_id"])
        self.assertEqual("succeeded", job["status"])
        self.assertFalse(job["task_b_executed"])
        self.assertTrue(job["checks"]["表头"])
        self.assertIn("document_pipeline", job["timings_seconds"])
        self.assertIn("finalization_and_hashing", job["timings_seconds"])
        self.assertEqual(
            {"result", "receipt", "api_receipt"}, set(job["output_files"])
        )

        with self.request("GET", job["links"]["result"]) as response:
            result = response.read()
            expected_hash = response.headers["X-Content-SHA256"]
        self.assertEqual(expected_hash, hashlib.sha256(result).hexdigest())
        self.assertNotIn("bundle", job["links"])

    def test_idempotency_reuses_same_job_and_rejects_changed_input(self) -> None:
        first = self.submit("idempotent-key")
        second = self.submit("idempotent-key")
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.submit("idempotent-key", association=minimal_xlsx("changed"))
        self.assertEqual(409, context.exception.code)

    def test_invalid_xlsx_is_rejected(self) -> None:
        body, content_type = multipart(
            {"server_date": "2026-08-19"},
            {
                "export4day": ("export4day.xlsx", b"not-an-xlsx"),
                "association": ("google.xlsx", minimal_xlsx()),
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request(
                "POST",
                "/v1/jobs",
                body,
                {"Content-Type": content_type, "Content-Length": str(len(body))},
            )
        self.assertEqual(400, context.exception.code)

    def test_client_round_trip_verifies_download(self) -> None:
        root = Path(self.temporary.name) / "client"
        root.mkdir()
        export = root / "export4day.xlsx"
        association = root / "google.xlsx"
        export.write_bytes(minimal_xlsx("client-export"))
        association.write_bytes(minimal_xlsx("client-google"))
        client = CLIENT_MODULE.ApiClient(self.base_url, TOKEN, 10, False)
        submitted = client.submit(export, association, "2026-08-19", "client-round-trip")
        job = client.wait(submitted["job_id"], 0.05, 10)
        self.assertEqual("succeeded", job["status"])
        downloaded = client.download(job, "result", root / "downloads")
        self.assertTrue(downloaded.is_file())
        self.assertEqual(
            job["output_files"]["result"]["sha256"],
            hashlib.sha256(downloaded.read_bytes()).hexdigest(),
        )

    def test_dispatcher_prefers_available_api_end_to_end(self) -> None:
        root = Path(self.temporary.name) / "dispatcher"
        root.mkdir()
        export = root / "export4day.xlsx"
        association = root / "google.xlsx"
        export.write_bytes(minimal_xlsx("dispatcher-export"))
        association.write_bytes(minimal_xlsx("dispatcher-google"))
        args = argparse.Namespace(
            export=export,
            association=association,
            server_date="2026-08-19",
            output_dir=root / "downloads",
            base_url=[self.base_url],
            token=TOKEN,
            api_config=root / "missing.md",
            local_builder=root / "must-not-run.sh",
            force_local=False,
            connect_timeout=1.0,
            request_timeout=10,
            poll_interval=0.05,
            wait_timeout=10,
            idempotency_key="dispatcher-end-to-end",
            insecure=False,
        )
        payload, code = DISPATCH_MODULE.execute(args)
        self.assertEqual(0, code)
        self.assertEqual("remote_api", payload["processing_mode"])
        self.assertTrue(Path(payload["output_path"]).is_file())
        self.assertTrue(Path(payload["receipt_path"]).is_file())
        self.assertTrue(Path(payload["dispatch_receipt_path"]).is_file())

    def test_auto_worker_count_uses_all_logical_cpus(self) -> None:
        self.assertEqual(max(1, os.cpu_count() or 1), resolve_worker_count("auto"))
        self.assertEqual(max(1, os.cpu_count() or 1), resolve_worker_count("max"))
        self.assertEqual(3, resolve_worker_count("3"))


if __name__ == "__main__":
    unittest.main()
