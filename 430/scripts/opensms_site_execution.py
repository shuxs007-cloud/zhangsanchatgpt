"""Cross-window 430 job lease; interoperates with the binding entrypoint.

No browser, retries, stale-lock removal, or business authorization here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

BROWSER_ID = "a53ce74e6c9e4fa08aec35f81ce1e2a3"
SITE_LOCK = Path("/Users/zhangsan/.opensms/locks") / f"{BROWSER_ID}.lock"
REPORT_LOCK = SITE_LOCK.with_name("opensms-new-browser-report.lock")
REQUEST_LOCK = Path("/Users/zhangsan/Downloads/430/opensms_artifacts/state/request_pacing/request.lock")


class SiteExecutionLease:
    def __init__(self, *, file=SITE_LOCK, timeout_seconds=7200, on_wait=None, inherited_owner=None):
        self.file = Path(file)
        self.timeout_seconds = timeout_seconds
        self.on_wait = on_wait or (lambda: None)
        self.owner = None
        self.inherited_owner = inherited_owner
        self.inherited = False

    def acquire(self):
        if self.inherited_owner is not None:
            current = json.loads(self.file.read_text(encoding="utf-8"))
            expected = self.inherited_owner
            if (not expected.get("browserSessionId") or not expected.get("token")
                    or any(current.get(key) != expected.get(key) for key in ("pid", "token", "browserSessionId"))):
                raise RuntimeError("OPENSMS_SITE_LOCK_OWNER_CHANGED: 新浏览器会话归属变化")
            os.kill(current["pid"], 0)
            self.owner = current
            self.inherited = True
            return self
        self.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        started, announced = time.monotonic(), False
        while True:
            try:
                fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    owner = json.loads(self.file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    owner = None
                if isinstance(owner, dict) and type(owner.get("pid")) is int and owner["pid"] > 0:
                    try:
                        os.kill(owner["pid"], 0)
                    except ProcessLookupError:
                        raise RuntimeError(f"OPENSMS_SITE_LOCK_ABANDONED: 遗留网站任务锁未确认清理，不抢锁: {self.file}")
                if time.monotonic() - started >= self.timeout_seconds:
                    raise RuntimeError("OPENSMS_SITE_LOCK_TIMEOUT: 其它窗口仍占用网站，未启动联网")
                if not announced:
                    self.on_wait()
                    announced = True
                time.sleep(0.25)
                continue
            self.owner = {"pid": os.getpid(), "token": str(uuid.uuid4()),
                          "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "purpose": "opensms-patrol"}
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.owner, handle)
                handle.flush()
                os.fsync(handle.fileno())
            return self

    @property
    def environment(self):
        if not self.owner:
            raise RuntimeError("网站任务锁尚未取得")
        return {"OPENSMS_SITE_LOCK_TOKEN": self.owner["token"],
                "OPENSMS_SITE_LOCK_PID": str(self.owner["pid"])}

    def release(self):
        if not self.owner:
            return
        current = json.loads(self.file.read_text(encoding="utf-8"))
        if current.get("token") != self.owner["token"] or current.get("pid") != self.owner["pid"]:
            raise RuntimeError("OPENSMS_SITE_LOCK_OWNER_CHANGED: 不删除其它任务的锁")
        if self.inherited:
            self.owner = None
            return
        self.file.unlink()
        self.owner = None
