"""Resolve the configured browser without falling back to a different session."""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "scripts/opensms_execution_settings.json"
STATUS = ROOT / "opensms_artifacts/state/new_browser/status.json"
LOCK = STATUS.with_name("identity.json")
NODE = Path("/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")


def show_pending_manual_login(browser_session):
    """After safe report cleanup only. No export, retries, or business writes."""
    if not browser_session or os.environ.get("OPENSMS_FIXED_RULES_OFFLINE") == "1":
        return None
    try:
        saved = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (saved.get("sessionId") != browser_session.get("sessionId")
            or saved.get("pid") != browser_session.get("pid")
            or saved.get("headless") is not True
            or saved.get("authentication") != "expired"
            or saved.get("manualLoginRequested") is not True):
        return None
    completed = subprocess.run([str(NODE), str(ROOT / "scripts/opensms_new_browser.mjs"), "login"],
                              cwd=ROOT, capture_output=True, text=True, timeout=105)
    payload = json.loads(completed.stdout)
    return {"event": "manual_login_window", "status": payload.get("status"),
            "error": payload.get("error"), "businessRestarted": False}


def start_headless_browser():
    """Reuse the recorded browser; show manual login only on confirmed expiry."""
    if os.environ.get("OPENSMS_FIXED_RULES_OFFLINE") == "1":
        raise RuntimeError("OPENSMS_AUTH_OFFLINE_NO_BROWSER")
    result = subprocess.run([str(NODE), str(ROOT / "scripts/opensms_new_browser.mjs"), "start"],
                            cwd=ROOT, capture_output=True, text=True, timeout=105)
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("OPENSMS_NEW_BROWSER_START_FAILED") from None
    if result.returncode or payload.get("status") != "ready":
        code = payload.get("error", "OPENSMS_NEW_BROWSER_LOGIN_REQUIRED")
        if not isinstance(code, str) or not code.startswith("OPENSMS_") or not code.replace("_", "").isalnum():
            code = "OPENSMS_NEW_BROWSER_START_FAILED"
        raise RuntimeError(f"{code}: 未启动业务；确认失效时已请求人工登录窗口，不自动重跑")


def ensure_configured_browser(*, starter=start_headless_browser, **kwargs):
    """Establish exact-session readiness without replacing a live headed browser."""
    # Invalid fixed settings must fail, never launch another browser.
    settings = json.loads(Path(kwargs.get("settings_path", SETTINGS)).read_text(encoding="utf-8"))
    if settings != {"browser": "cloakbrowser", "requestCompletionGapMs": 1000,
                    "requestConcurrency": 1}:
        raise RuntimeError("OPENSMS_BROWSER_SETTINGS_INVALID")
    try:
        resolved = resolve_configured_browser(**kwargs)
    except RuntimeError:
        resolved = None
    if not resolved or resolved["status"] != "ready":
        starter()
        resolved = resolve_configured_browser(**kwargs)
    if resolved["status"] != "ready":
        raise RuntimeError("OPENSMS_NEW_BROWSER_LOGIN_REQUIRED: 等待人工登录，不自动重跑业务")
    return resolved


def resolve_configured_browser(*, settings_path=SETTINGS, status_path=STATUS,
                               lock_path=LOCK, process_check=None):
    settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    if settings != {"browser": "cloakbrowser", "requestCompletionGapMs": 1000,
                    "requestConcurrency": 1}:
        raise RuntimeError("OPENSMS_BROWSER_SETTINGS_INVALID: 浏览器或限速固定配置不符")
    try:
        session = json.loads(Path(status_path).read_text(encoding="utf-8"))
        owner = json.loads(Path(lock_path).read_text(encoding="utf-8"))
        pid, port = session["pid"], session["port"]
        if (session.get("browser") != "cloakbrowser" or session.get("status") not in {"awaiting-manual-login", "ready", "login-required", "auth-check-failed"}
                or (session.get("status") == "ready" and session.get("authentication") != "verified-readonly-api")
                or type(pid) is not int or pid <= 0 or type(port) is not int or not 1 <= port <= 65535
                or not session.get("sessionId") or not session.get("webSocketPath", "").startswith("/devtools/browser/")
                or owner.get("pid") != pid or owner.get("browserSessionId") != session["sessionId"]
                or not owner.get("token")):
            raise ValueError("浏览器归属不一致")
        (process_check or (lambda value: os.kill(value, 0)))(pid)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("OPENSMS_NEW_BROWSER_LOGIN_REQUIRED: 请启动新浏览器并人工登录；不会回退旧430") from exc
    return {"browser": "cloakbrowser", "cdp": f"http://127.0.0.1:{port}",
            "sessionId": session["sessionId"], "pid": pid,
            "webSocketPath": session["webSocketPath"], "owner": owner,
            "headless": session.get("headless") is True, "status": session["status"]}
