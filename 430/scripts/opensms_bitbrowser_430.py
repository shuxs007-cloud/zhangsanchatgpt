"""Resolve only the existing BitBrowser 430 profile; never launch a browser."""
from pathlib import Path

PROFILE_ID = "a53ce74e6c9e4fa08aec35f81ce1e2a3"
SESSION_POLICY = "existing-bitbrowser-430"
ACTIVE_PORT = Path("/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache") / PROFILE_ID / "DevToolsActivePort"


def resolve_current_430_cdp(*, active_port=ACTIVE_PORT):
    try:
        lines = Path(active_port).read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        if not 1 <= port <= 65535 or len(lines) != 2 or not lines[1].startswith("/devtools/browser/"):
            raise ValueError("invalid profile endpoint")
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError("OPENSMS_TASK_F_430_SESSION_UNAVAILABLE: 请打开比特430并登录Grab&Go") from exc
    return f"http://127.0.0.1:{port}"
