#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from opensms_document_api_client import (
    ApiClient,
    ApiConnectionError,
    ApiHttpError,
    default_idempotency_key,
    download_many,
    sha256_file,
    validate_input,
)


DEFAULT_API_CONFIG = Path.home() / "Downloads" / "OPENSMS_LAN_API.md"
RETRYABLE_HTTP_STATUSES = {401, 403, 404, 408, 413, 429, 500, 502, 503, 504}
URL_PATTERN = re.compile(r"(?:主地址|备用地址)[^`]*`(https?://[^`]+)`")
TOKEN_PATTERN = re.compile(r"Bearer Token[^`]*`([^`]+)`")


class RemoteProcessingError(RuntimeError):
    """The API was available and returned a real processing failure."""


class LocalProcessingError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, Any]):
        super().__init__(message)
        self.diagnostics = diagnostics


def local_failure_details(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Keep business verification errors ahead of harmless stderr warnings."""
    try:
        payload = parse_json_output(completed.stdout)
    except (ValueError, RuntimeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    failed = payload.get("failed_checks")
    failed = [str(item) for item in failed] if isinstance(failed, list) else []
    return {
        "exit_code": completed.returncode,
        "result": payload.get("result"),
        "failed_checks": failed,
        "receipt_path": payload.get("receipt_path"),
        "error": payload.get("error"),
        "stderr_tail": completed.stderr.strip()[-4000:],
        "stdout_tail": completed.stdout.strip()[-4000:] if not payload else None,
    }


def parse_api_config(path: Path) -> tuple[list[str], str | None]:
    if not path.is_file():
        return [], None
    text = path.read_text(encoding="utf-8", errors="replace")
    urls = URL_PATTERN.findall(text)
    token_match = TOKEN_PATTERN.search(text)
    return deduplicate(urls), token_match.group(1).strip() if token_match else None


def deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip().rstrip("/")
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def resolve_api_settings(args: argparse.Namespace) -> tuple[list[str], str | None, str]:
    config_path = args.api_config.expanduser().resolve()
    config_urls, config_token = parse_api_config(config_path)
    if args.base_url:
        urls = args.base_url
        source = "command_line"
    elif os.environ.get("OPENSMS_DOC_API_URLS"):
        urls = os.environ["OPENSMS_DOC_API_URLS"].split(",")
        source = "environment"
    elif os.environ.get("OPENSMS_DOC_API_URL"):
        urls = [os.environ["OPENSMS_DOC_API_URL"]]
        source = "environment"
    else:
        urls = config_urls
        source = str(config_path) if config_urls else "none"
    token = args.token or os.environ.get("OPENSMS_DOC_API_TOKEN") or config_token
    return deduplicate(urls), token, source


def probe_endpoint(base_url: str, timeout: float, insecure: bool) -> dict[str, Any]:
    request = Request(base_url.rstrip("/") + "/healthz", method="GET")
    request.add_header("Accept", "application/json")
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "base_url": base_url,
            "available": True,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "health": payload,
        }
    except HTTPError as exc:
        return {
            "base_url": base_url,
            "available": False,
            "reason": f"health_http_{exc.code}",
        }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", exc)
        return {
            "base_url": base_url,
            "available": False,
            "reason": f"health_unreachable: {reason}",
        }


def find_local_builder(explicit: Path | None) -> Path:
    if explicit:
        candidates = [explicit.expanduser().resolve()]
    else:
        script_dir = Path(__file__).resolve().parent
        candidates = [
            script_dir.parent
            / "services"
            / "opensms_document_api"
            / "pipeline"
            / "build_card_slot_report.sh",
            script_dir / "pipeline" / "build_card_slot_report.sh",
        ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "本地处理脚本不存在: " + ", ".join(str(path) for path in candidates)
    )


def parse_json_output(output: str) -> dict[str, Any]:
    stripped = output.strip()
    if not stripped:
        raise RuntimeError("本地处理没有返回 JSON")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start >= 0:
            return json.loads(stripped[start:])
        raise


def ko_source_contract(environment: dict[str, str]) -> tuple[str, str]:
    """Choose the contract from the bound input, never from returned proof."""
    source = Path(environment.get("OPENSMS_SYSTEM_KOODA_PATH", "")).resolve()
    if (not source.is_file()
            or sha256_file(source) != environment.get("OPENSMS_SYSTEM_KOODA_SHA256")):
        raise RuntimeError("ko系统有-未激活来源文件或SHA256不一致")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("ko系统有-未激活来源格式无效")
    if payload.get("sourceKind") == "verified_10day_export":
        pipeline = Path(__file__).resolve().parents[1] / "services/opensms_document_api/pipeline"
        if str(pipeline) not in sys.path:
            sys.path.insert(0, str(pipeline))
        from report_kooda_source import validate_projection
        validate_projection(payload, environment.get("OPENSMS_RUN_ID"))
        return "task_f_activation_missing_in_10day_export", "current_10day_export.卡槽"
    return "task_f_activation_missing_in_current_system", "current_system_inventory.slotIdentif"


def run_local(
    builder: Path,
    export_path: Path,
    association_path: Path | None,
    output_dir: Path,
    server_date: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    completed = subprocess.run(
        [
            "bash",
            str(builder),
            str(export_path),
            str(association_path) if association_path is not None else "-",
            str(output_dir),
            server_date,
        ],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        diagnostics = local_failure_details(completed)
        detail = ("核验未通过：" + "、".join(diagnostics["failed_checks"])) if diagnostics["failed_checks"] else (
            diagnostics["error"] or diagnostics["stdout_tail"] or diagnostics["stderr_tail"] or "无结构化错误回执"
        )
        raise LocalProcessingError(
            f"本地文档处理失败（退出码 {completed.returncode}）: {detail}", diagnostics
        )
    result = parse_json_output(completed.stdout)
    if result.get("result") not in {"成功", "success"}:
        raise RuntimeError(f"本地文档处理未通过复核: {result.get('failed_checks')}")
    if association_path is None:
        expected = {"enabled": False, "status": "skipped", "reason": "user_disabled",
                    "unused_port_device_scope": "system_source_only"}
        review = result.get("verification", {}).get("google_dependency_review", {})
        if (result.get("process", {}).get("google_dependency") != expected
                or any(review.get(key) != value for key, value in expected.items())
                or review.get("matches") is not True):
            raise RuntimeError("无Google文档缺少生成及独立回读的禁用依赖证明")
    if environment.get("OPENSMS_CHATR_GOOGLE_DATES_PATH"):
        proof = result.get("process", {}).get("google_chatr_date_fallback", {})
        review = result.get("verification", {}).get("google_chatr_date_fallback_review", {})
        if (proof.get("enabled") is not True or proof.get("status") != "passed"
                or proof.get("snapshot_path") != str(Path(environment["OPENSMS_CHATR_GOOGLE_DATES_PATH"]).resolve())
                or proof.get("snapshot_sha256") != environment.get("OPENSMS_CHATR_GOOGLE_DATES_SHA256", "").lower()
                or review.get("matches") is not True or any(review.get(key) != value for key, value in proof.items())):
            raise RuntimeError("Chatr专用Google日期缺少生成及独立回读来源证明")
    if environment.get("OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED") == "1":
        proof = result.get("process", {}).get("google_system_missing_sheet") or {}
        review = result.get("verification", {}).get("google_system_missing_sheet_review") or {}
        if (proof.get("enabled") is not True or review.get("matches") is not True
                or proof.get("run_id") != environment.get("OPENSMS_RUN_ID")
                or proof.get("snapshot_sha256") != environment.get("OPENSMS_CHATR_GOOGLE_DATES_SHA256")
                or any(review.get(key) != value for key, value in proof.items())):
            raise RuntimeError("Google系统差集缺少本轮来源及独立回读证明")
    if environment.get("OPENSMS_KO_UNACTIVATED_REQUIRED") == "1" or result.get("process", {}).get("ko_unactivated_sheet"):
        proof = result.get("process", {}).get("ko_unactivated_sheet") or {}
        review = result.get("verification", {}).get("ko_unactivated_sheet_review") or {}
        expected_scope, expected_slot_source = ko_source_contract(environment)
        if (proof.get("enabled") is not True or proof.get("status") != "passed"
                or proof.get("scope") != expected_scope or review.get("matches") is not True
                or proof.get("sheet") != "ko系统有-未激活"
                or proof.get("slot_source") != expected_slot_source
                or proof.get("run_id") != environment.get("OPENSMS_RUN_ID")
                or proof.get("snapshot_sha256") != environment.get("OPENSMS_REPORT_DATE_CACHE_SHA256")
                or proof.get("system_path") != str(Path(environment.get("OPENSMS_SYSTEM_KOODA_PATH", "")).resolve())
                or proof.get("system_sha256") != environment.get("OPENSMS_SYSTEM_KOODA_SHA256")
                or any(review.get(key) != value for key, value in proof.items())):
            raise RuntimeError("ko系统有-未激活缺少本轮F冻结库与系统交集独立回读证明")
    if environment.get("OPENSMS_CARD_RETENTION_REPORT_REQUIRED") == "1":
        proof = result.get("process", {}).get("card_retention_report") or {}
        review = result.get("verification", {}).get("card_retention_report_review") or {}
        if (proof.get("source_sha256") != environment.get("OPENSMS_CARD_RETENTION_REPORT_SHA256")
                or proof.get("run_id") != environment.get("OPENSMS_RUN_ID")
                or review.get("matches") is not True
                or any(review.get(key) != value for key, value in proof.items())):
            raise RuntimeError("任务A需要保留时间缺少独立来源核验")
    return result


def remote_process(
    urls: list[str],
    token: str | None,
    export_path: Path,
    association_path: Path,
    output_dir: Path,
    server_date: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    attempts: list[dict[str, Any]] = []
    if not urls:
        return None, attempts, "未配置局域网接口地址"
    if not token:
        return None, attempts, "局域网接口 Token 缺失"

    key = args.idempotency_key or default_idempotency_key(
        export_path, association_path, server_date
    )
    last_reason = "局域网接口不可用"
    for base_url in urls:
        probe = probe_endpoint(base_url, args.connect_timeout, args.insecure)
        attempts.append(probe)
        if not probe["available"]:
            last_reason = str(probe["reason"])
            continue

        client = ApiClient(base_url, token, args.request_timeout, args.insecure)
        try:
            operation_started = time.monotonic()
            submit_started = time.monotonic()
            job = client.submit(export_path, association_path, server_date, key)
            submit_seconds = time.monotonic() - submit_started
            probe["submit"] = "accepted"
            probe["job_id"] = job.get("job_id")
            wait_started = time.monotonic()
            job = client.wait(job["job_id"], args.poll_interval, args.wait_timeout)
            wait_seconds = time.monotonic() - wait_started
            probe["remote_status"] = job.get("status")
            if job.get("status") != "succeeded":
                raise RemoteProcessingError(
                    f"远端作业 {job.get('job_id')} 处理失败: {job.get('error')}"
                )
            download_started = time.monotonic()
            downloaded = {
                key_name: str(path)
                for key_name, path in download_many(
                    client, job, ("result", "receipt"), output_dir
                ).items()
            }
            download_seconds = time.monotonic() - download_started
            remote_timings = {
                "submit": round(submit_seconds, 3),
                "wait": round(wait_seconds, 3),
                "download_and_verify": round(download_seconds, 3),
                "total": round(time.monotonic() - operation_started, 3),
                "server": job.get("timings_seconds", {}),
            }
            probe["timings_seconds"] = remote_timings
            return (
                {
                    "processing_mode": "remote_api",
                    "api_base_url": base_url,
                    "job": job,
                    "downloaded": downloaded,
                    "output_path": downloaded["result"],
                    "receipt_path": downloaded["receipt"],
                    "timings_seconds": remote_timings,
                },
                attempts,
                None,
            )
        except RemoteProcessingError:
            raise
        except ApiConnectionError as exc:
            probe["operation_error"] = str(exc)
            last_reason = str(exc)
            continue
        except ApiHttpError as exc:
            probe["operation_error"] = str(exc)
            if exc.status_code in RETRYABLE_HTTP_STATUSES:
                last_reason = str(exc)
                continue
            raise RemoteProcessingError(str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise RemoteProcessingError(f"远端接口返回结构异常: {exc}") from exc
        except RuntimeError as exc:
            raise RemoteProcessingError(str(exc)) from exc
    return None, attempts, last_reason


def unique_receipt_path(output_dir: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    candidate = output_dir / f"opensms_document_dispatch_{stamp}_receipt.json"
    index = 1
    while candidate.exists():
        candidate = output_dir / f"opensms_document_dispatch_{stamp}_{index:02d}_receipt.json"
        index += 1
    return candidate


def write_receipt(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_receipt_path(output_dir)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def validate_server_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"后台服务器日期必须为 YYYY-MM-DD: {value}") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise SystemExit(f"后台服务器日期无效: {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenSMS 系统文档本地生成与独立复核：默认禁用Google，使用本轮系统导出和冻结日期缓存"
    )
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--association", type=Path, help="仅显式旧版Google模式需要")
    parser.add_argument("--no-google", action="store_true", help="禁用完整Google关联；Chatr日期回退仅可由显式本轮快照启用")
    parser.add_argument("--legacy-google-association", action="store_true", help="仅兼容离线旧版关联测试；生产入口始终禁用Google")
    parser.add_argument("--server-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", action="append", help="可重复，按顺序尝试")
    parser.add_argument("--token")
    parser.add_argument(
        "--api-config",
        type=Path,
        default=Path(os.environ.get("OPENSMS_LAN_API_CONFIG", DEFAULT_API_CONFIG)),
    )
    parser.add_argument("--local-builder", type=Path)
    parser.add_argument("--force-local", action="store_true")
    parser.add_argument("--system-kooda", type=Path, help="本轮系统Kooda库存JSON；也可由OPENSMS_SYSTEM_KOODA_PATH传入")
    parser.add_argument("--system-kooda-sha256", help="本轮系统库存SHA256")
    parser.add_argument("--report-date-cache", type=Path, help="本轮文档开始前冻结的只读F日期缓存JSON")
    parser.add_argument("--report-date-cache-sha256", help="本轮冻结日期缓存SHA256")
    parser.add_argument("--chatr-google-dates", type=Path, help="本轮Chatr专用Google日期快照JSON")
    parser.add_argument("--chatr-google-dates-sha256", help="本轮Chatr日期快照SHA256")
    parser.add_argument("--run-id", help="本轮系统库存运行编号")
    parser.add_argument("--connect-timeout", type=float, default=3.0)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--wait-timeout", type=int, default=2400)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--insecure", action="store_true")
    return parser


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    execute_started = time.monotonic()
    no_google = (not bool(getattr(args, "legacy_google_association", False))
                 or bool(getattr(args, "no_google", False))
                 or os.environ.get("OPENSMS_GOOGLE_ASSOCIATION_ENABLED") == "0")
    if no_google:
        os.environ["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"] = "0"
        args.force_local = True
    for attribute, key in (("system_kooda", "OPENSMS_SYSTEM_KOODA_PATH"),
                           ("system_kooda_sha256", "OPENSMS_SYSTEM_KOODA_SHA256"),
                           ("report_date_cache", "OPENSMS_REPORT_DATE_CACHE_PATH"),
                           ("report_date_cache_sha256", "OPENSMS_REPORT_DATE_CACHE_SHA256"),
                           ("chatr_google_dates", "OPENSMS_CHATR_GOOGLE_DATES_PATH"),
                           ("chatr_google_dates_sha256", "OPENSMS_CHATR_GOOGLE_DATES_SHA256"),
                           ("run_id", "OPENSMS_RUN_ID")):
        if getattr(args, attribute, None):
            os.environ[key] = str(getattr(args, attribute))
    if not all(os.environ.get(key) for key in ("OPENSMS_SYSTEM_KOODA_PATH", "OPENSMS_SYSTEM_KOODA_SHA256", "OPENSMS_RUN_ID")):
        raise RuntimeError("报表需要本轮系统Kooda库存、SHA256与运行编号，请使用统一本地巡检入口")
    if not all(os.environ.get(key) for key in ("OPENSMS_REPORT_DATE_CACHE_PATH", "OPENSMS_REPORT_DATE_CACHE_SHA256")):
        raise RuntimeError("报表需要本轮冻结日期缓存与SHA256，禁止直接读取运行中的任务F数据库")
    export_path = validate_input(args.export, "export4day")
    if not no_google and args.association is None:
        raise RuntimeError("旧版Google关联模式必须提供--association；无Google模式请传--no-google")
    association_path = None if no_google else validate_input(args.association, "Google关联表")
    chatr_path = os.environ.get("OPENSMS_CHATR_GOOGLE_DATES_PATH", "")
    chatr_hash = os.environ.get("OPENSMS_CHATR_GOOGLE_DATES_SHA256", "").lower()
    if bool(chatr_path) != bool(chatr_hash) or (chatr_path and not no_google):
        raise RuntimeError("Chatr专用日期快照路径和SHA256必须成对提供，完整Google关联必须关闭")
    if chatr_path:
        chatr_path = str(Path(chatr_path).expanduser().resolve())
        if not Path(chatr_path).is_file():
            raise RuntimeError("Chatr日期快照文件不存在")
        if not re.fullmatch(r"[a-f0-9]{64}", chatr_hash) or sha256_file(Path(chatr_path)) != chatr_hash:
            raise RuntimeError("Chatr日期快照SHA256不匹配")
        os.environ["OPENSMS_CHATR_GOOGLE_DATES_PATH"] = chatr_path
    server_date = validate_server_date(args.server_date)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    urls, token, settings_source = resolve_api_settings(args)

    base_receipt: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "result": "失败",
        "processing_mode": None,
        "api_settings_source": settings_source,
        "api_urls": urls,
        "inputs": {
            "export4day": {"path": str(export_path), "sha256": sha256_file(export_path)},
            "association": None if no_google else {
                "path": str(association_path),
                "sha256": sha256_file(association_path),
            },
            "server_date": server_date,
        },
        "task_b_executed": False,
        "google_dependency": {"enabled": False, "status": "skipped", "reason": "user_disabled",
                              "unused_port_device_scope": "system_source_only"} if no_google else {"enabled": True, "status": "enabled"},
    }
    system_kooda_path = os.environ.get("OPENSMS_SYSTEM_KOODA_PATH")
    base_receipt["inputs"]["google_dependency"] = base_receipt["google_dependency"]
    if chatr_path:
        base_receipt["inputs"]["chatr_google_dates"] = {"path": chatr_path, "sha256": chatr_hash,
            "run_id": os.environ["OPENSMS_RUN_ID"]}
    if system_kooda_path:
        # The system snapshot stays local and is independently hash-checked
        # by the generator and verifier. Never send it to a legacy LAN worker.
        args.force_local = True
        base_receipt["inputs"]["system_kooda"] = {
            "path": system_kooda_path,
            "sha256": os.environ.get("OPENSMS_SYSTEM_KOODA_SHA256"),
            "run_id": os.environ.get("OPENSMS_RUN_ID"),
        }
        base_receipt["inputs"]["report_date_cache"] = {
            "path": os.environ["OPENSMS_REPORT_DATE_CACHE_PATH"],
            "sha256": os.environ["OPENSMS_REPORT_DATE_CACHE_SHA256"],
            "run_id": os.environ["OPENSMS_RUN_ID"],
        }
    attempts: list[dict[str, Any]] = []

    try:
        remote_result = None
        fallback_reason = "已指定强制本地处理" if args.force_local else None
        if not args.force_local:
            remote_result, attempts, fallback_reason = remote_process(
                urls,
                token,
                export_path,
                association_path,
                output_dir,
                server_date,
                args,
            )
        if remote_result:
            remote_receipt_path = Path(
                str((remote_result.get("downloaded") or {}).get("receipt") or "")
            ).expanduser()
            remote_cell_policy = None
            if remote_receipt_path.is_file():
                try:
                    remote_receipt = json.loads(remote_receipt_path.read_text(encoding="utf-8"))
                    remote_cell_policy = remote_receipt.get("cell_type_policy") or (
                        (remote_receipt.get("enrichment") or {}).get("cell_type_policy")
                    )
                except (OSError, json.JSONDecodeError):
                    remote_cell_policy = None
            if not remote_cell_policy or remote_cell_policy.get(
                "numeric_text_conversion_enabled"
            ) is not False:
                remote_result = None
                fallback_reason = "远端文档服务仍使用旧的文本型数字转换策略"
            else:
                base_receipt.update(remote_result)
        if not remote_result:
            builder = find_local_builder(args.local_builder)
            local_result = run_local(
                builder, export_path, association_path, output_dir, server_date
            )
            base_receipt.update(
                {
                    "processing_mode": "local_fallback",
                    "fallback_reason": fallback_reason,
                    "local_builder": str(builder),
                    "local_result": local_result,
                    "output_path": local_result.get("output_path"),
                    "receipt_path": local_result.get("receipt_path"),
                }
            )
            if chatr_path:
                base_receipt["google_chatr_date_fallback"] = local_result["process"]["google_chatr_date_fallback"]
        output_path = Path(str(base_receipt.get("output_path") or "")).expanduser().resolve()
        if not output_path.is_file():
            raise RuntimeError(f"文档处理结果不存在: {output_path}")
        base_receipt["cell_type_policy"] = {
            "numeric_text_conversion_enabled": False,
            "mode": "preserve_source_cell_types",
        }
        base_receipt["output_sha256"] = sha256_file(output_path)
        base_receipt["api_attempts"] = attempts
        base_receipt["result"] = "成功"
        exit_code = 0
    except RemoteProcessingError as exc:
        base_receipt.update(
            {
                "processing_mode": "remote_api_failed_no_local_fallback",
                "api_attempts": attempts,
                "error": str(exc),
            }
        )
        exit_code = 4
    except LocalProcessingError as exc:
        base_receipt.update({"processing_mode": "local_failed", "api_attempts": attempts,
                             "error": str(exc), "local_failure": exc.diagnostics})
        exit_code = 5
    except Exception as exc:
        base_receipt.update(
            {
                "api_attempts": attempts,
                "error": str(exc),
            }
        )
        exit_code = 5

    base_receipt["finished_at"] = datetime.now().astimezone().isoformat()
    base_receipt["duration_seconds"] = round(time.monotonic() - execute_started, 3)
    dispatch_receipt = write_receipt(output_dir, base_receipt)
    base_receipt["dispatch_receipt_path"] = str(dispatch_receipt)
    dispatch_receipt.write_text(
        json.dumps(base_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return base_receipt, exit_code


def main() -> int:
    payload, exit_code = execute(build_parser().parse_args())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
