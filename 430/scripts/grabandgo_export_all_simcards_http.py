#!/usr/bin/env python3

"""Read-only full Grab&Go Customer SIM Cards export via BitBrowser 430 auth.

The site uses ASP.NET WebForms rather than a JSON paging endpoint. This script
copies only the live authenticated cookies into memory, submits the site's own
Show All form, then serially posts btnExtendBuffer with the returned ViewState.
Cookies are never printed or persisted. Every newly returned 200-row segment is
written as an atomic part so the job can resume without repeating completed
segments.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import lxml.html  # noqa: E402
import requests  # noqa: E402
import websocket  # noqa: E402


PROFILE_ID = "a53ce74e6c9e4fa08aec35f81ce1e2a3"
PROFILE_DIR = Path(
    "/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache"
) / PROFILE_ID
PORT_FILE = PROFILE_DIR / "DevToolsActivePort"
TARGET_URL = "https://www.grabandgoplus.com/Retailer_CustomerSIMCards"
SHOW_ALL_NAME = "ctl00$GagPRetailerHolder$btnShowAllSimCards"
EXTEND_EVENT_TARGET = "ctl00$GagPRetailerHolder$btnExtendBuffer"
REQUIRED_FIELDS = [
    "lblPurchaseDate",
    "lblActivationRequestDate",
    "lblActivateDate",
    "lblSimCardNumber",
    "lblSKU",
    "lblCost",
    "lnSimDetails",
]
CONTROL_RE = re.compile(
    r"^GagPRetailerHolder_dlCustomerSimCards_(.+)_(\d+)$"
)


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path_value, payload, private=False):
    if not path_value:
        return
    path_obj = Path(path_value).expanduser().resolve()
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    temporary = path_obj.with_name(f"{path_obj.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if private:
        temporary.chmod(0o600)
    temporary.replace(path_obj)


def cdp_cookies(port):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version", timeout=10
    ) as response:
        metadata = json.load(response)
    socket = websocket.create_connection(
        metadata["webSocketDebuggerUrl"], timeout=20, suppress_origin=True
    )
    try:
        socket.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(socket.recv())
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError(f"CDP读取Cookie失败: {message['error']}")
                return message["result"]["cookies"]
    finally:
        socket.close()


def session_from_430():
    if not PORT_FILE.exists():
        raise RuntimeError(f"BitBrowser 430端口文件不存在: {PORT_FILE}")
    port = PORT_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
    if not port.isdigit():
        raise RuntimeError(f"BitBrowser 430端口无效: {port!r}")
    browser_cookies = cdp_cookies(port)
    session = requests.Session()
    cookie_count = 0
    for cookie in browser_cookies:
        domain = cookie.get("domain", "")
        if not domain.endswith("grabandgoplus.com"):
            continue
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=domain,
            path=cookie.get("path", "/"),
        )
        cookie_count += 1
    if not cookie_count:
        raise RuntimeError("BitBrowser 430中没有Grab&Go Cookie")
    return session, port


def parse_document(content):
    return lxml.html.fromstring(content)


def by_id(tree, element_id):
    nodes = tree.xpath(f'//*[@id="{element_id}"]')
    return nodes[0] if nodes else None


def input_value(tree, element_id):
    element = by_id(tree, element_id)
    return (element.get("value") or "") if element is not None else ""


def login_proof(tree):
    body = " ".join(tree.text_content().split())
    show_all = by_id(tree, "GagPRetailerHolder_btnShowAllSimCards")
    password = tree.xpath('//input[translate(@type,"PASSWORD","password")="password"]')
    return {
        "showAllPresent": show_all is not None,
        "showAllText": (show_all.get("value") or "").strip()
        if show_all is not None
        else "",
        "hasCustomerSimMarker": bool(
            re.search(r"Customer\s+SIM\s+Cards", body, re.IGNORECASE)
        ),
        "greetingPresent": bool(re.search(r"\bHello\b", body, re.IGNORECASE)),
        "hasPasswordInput": bool(password),
        "title": (tree.findtext(".//title") or "").strip(),
    }


def form_fields(tree):
    fields = {}
    for element in tree.xpath("//form[1]//input[@name]"):
        input_type = (element.get("type") or "text").lower()
        if input_type in ("hidden", "text"):
            fields[element.get("name")] = element.get("value") or ""
    return fields


def counters(tree):
    total_raw = input_value(tree, "hfTotal")
    rendered_raw = input_value(tree, "hfRendered")
    rows = tree.xpath(
        '//*[contains(concat(" ",normalize-space(@class)," "),'
        '" scrollableDataList ")]'
        '//*[contains(concat(" ",normalize-space(@class)," ")," sim-row ")]'
    )
    return {
        "total": int(total_raw or 0),
        "rendered": int(rendered_raw or 0),
        "rowCount": len(rows),
    }


def clean_text(element):
    return " ".join(element.text_content().replace("\u00a0", " ").split())


def extract_rows(tree):
    row_nodes = tree.xpath(
        '//*[contains(concat(" ",normalize-space(@class)," "),'
        '" scrollableDataList ")]'
        '//*[contains(concat(" ",normalize-space(@class)," ")," sim-row ")]'
    )
    rows = []
    field_names = set()
    for ordinal, row_node in enumerate(row_nodes, start=1):
        fields = {}
        control_index = ""
        for element in row_node.xpath('.//*[@id]'):
            match = CONTROL_RE.match(element.get("id") or "")
            if not match:
                continue
            field_name, index = match.groups()
            if not control_index:
                control_index = index
            field_names.add(field_name)
            fields[field_name] = clean_text(element)
        rows.append(
            {
                "ordinal": ordinal,
                "controlIndex": control_index,
                "fields": fields,
            }
        )
    ordered_fields = [field for field in REQUIRED_FIELDS if field in field_names]
    ordered_fields.extend(sorted(field_names.difference(REQUIRED_FIELDS)))
    return rows, ordered_fields


def post_form(session, payload, timeout_seconds):
    started = time.monotonic()
    response = session.post(
        TARGET_URL,
        data=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.content, {
        "status": response.status_code,
        "elapsedMs": round((time.monotonic() - started) * 1000),
        "responseBytes": len(response.content),
    }


def read_parts(parts_dir, maximum_rendered=None):
    rows = []
    field_names = []
    for part_path in sorted(parts_dir.glob("part_*.json")):
        match = re.match(r"part_(\d+)_(\d+)\.json$", part_path.name)
        if not match:
            continue
        end = int(match.group(2))
        if maximum_rendered is not None and end > maximum_rendered:
            continue
        payload = json.loads(part_path.read_text(encoding="utf-8"))
        rows.extend(payload["rows"])
        for field in payload.get("fieldNames", []):
            if field not in field_names:
                field_names.append(field)
    rows.sort(key=lambda row: row["ordinal"])
    return rows, field_names


def summarize(rows, field_names):
    nonempty = {}
    blank = {}
    for field in field_names:
        count = sum(1 for row in rows if str(row.get("fields", {}).get(field, "")).strip())
        nonempty[field] = count
        blank[field] = len(rows) - count
    sims = [
        str(row.get("fields", {}).get("lblSimCardNumber", "")).strip()
        for row in rows
    ]
    nonempty_sims = [value for value in sims if value]
    return {
        "nonEmptyByField": nonempty,
        "blankByField": blank,
        "simNumberNonEmptyCount": len(nonempty_sims),
        "simNumberUniqueCount": len(set(nonempty_sims)),
        "duplicateSimNumberCount": len(nonempty_sims) - len(set(nonempty_sims)),
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", required=False)
    parser.add_argument("--progress-json", required=False)
    parser.add_argument("--state-json", required=False)
    parser.add_argument("--parts-dir", required=False)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def self_test():
    sample = "GagPRetailerHolder_dlCustomerSimCards_lblSKU_123"
    match = CONTROL_RE.match(sample)
    passed = bool(match and match.groups() == ("lblSKU", "123"))
    print(json.dumps({"status": "passed" if passed else "failed", "liveBrowserUsed": False}))
    return 0 if passed else 2


def main():
    args = parse_args()
    if args.self_test:
        return self_test()
    for name in ("output_json", "progress_json", "state_json", "parts_dir"):
        if not getattr(args, name):
            raise RuntimeError(f"必须指定 --{name.replace('_', '-')}")
    if args.timeout_seconds < 30:
        raise RuntimeError("--timeout-seconds 必须至少为30")

    started_at = iso_now()
    output_path = Path(args.output_json).expanduser().resolve()
    progress_path = Path(args.progress_json).expanduser().resolve()
    state_path = Path(args.state_json).expanduser().resolve()
    parts_dir = Path(args.parts_dir).expanduser().resolve()
    parts_dir.mkdir(parents=True, exist_ok=True)
    session, port = session_from_430()
    state = None
    if args.resume and state_path.exists():
        candidate = json.loads(state_path.read_text(encoding="utf-8"))
        if candidate.get("status") == "running" and candidate.get("formFields"):
            state = candidate
    if state is None:
        get_response = session.get(TARGET_URL, timeout=120)
        get_response.raise_for_status()
        get_tree = parse_document(get_response.content)
        proof = login_proof(get_tree)
        if not (
            proof["showAllPresent"]
            and proof["showAllText"] == "Show All"
            and proof["hasCustomerSimMarker"]
            and not proof["hasPasswordInput"]
        ):
            payload = {
                "status": "login_required",
                "startedAt": started_at,
                "finishedAt": iso_now(),
                "browser": "BitBrowser 430",
                "profileId": PROFILE_ID,
                "port": port,
                "loginProof": proof,
                "sensitiveDataSubmittedCount": 0,
                "instruction": "请在BitBrowser 430登录Grab&Go，确认Customer SIM Cards页面显示Show All后重试",
            }
            atomic_json(output_path, payload)
            print(json.dumps(payload, ensure_ascii=False))
            return 4
        raise RuntimeError("未找到浏览器Show All生成的有效断点；请先运行CDP bootstrap，再使用--resume继续")

    proof = state.get("loginProof") or {}
    if not (
        proof.get("showAllPresent")
        and proof.get("showAllText") == "Show All"
        and proof.get("hasCustomerSimMarker")
        and not proof.get("hasPasswordInput")
    ):
        raise RuntimeError("断点缺少有效登录证明；请重新运行CDP bootstrap")

    while state["rendered"] < state["total"]:
        previous = state["rendered"]
        payload = dict(state["formFields"])
        payload["__EVENTTARGET"] = EXTEND_EVENT_TARGET
        payload["__EVENTARGUMENT"] = ""
        payload.pop(SHOW_ALL_NAME, None)
        content, receipt = post_form(session, payload, args.timeout_seconds)
        tree = parse_document(content)
        current_proof = login_proof(tree)
        page_counts = counters(tree)
        all_rows, fields = extract_rows(tree)
        if not current_proof["showAllPresent"]:
            raise RuntimeError("缓冲扩展回包未保持登录状态")
        if not (
            page_counts["total"] == state["total"]
            and page_counts["rendered"] == page_counts["rowCount"] == len(all_rows)
            and page_counts["rendered"] > previous
            and page_counts["rendered"] <= page_counts["total"]
        ):
            raise RuntimeError(
                f"缓冲扩展计数无效: previous={previous}, {page_counts}, rows={len(all_rows)}"
            )
        new_rows = all_rows[previous : page_counts["rendered"]]
        expected_ordinals = list(range(previous + 1, page_counts["rendered"] + 1))
        actual_ordinals = [row["ordinal"] for row in new_rows]
        if actual_ordinals != expected_ordinals:
            raise RuntimeError(
                f"新增批次序号不连续: expected={expected_ordinals[:1]}..{expected_ordinals[-1:]}, "
                f"actual={actual_ordinals[:1]}..{actual_ordinals[-1:]}"
            )
        part_path = parts_dir / f"part_{previous + 1:06d}_{page_counts['rendered']:06d}.json"
        atomic_json(
            part_path,
            {
                "startOrdinal": previous + 1,
                "endOrdinal": page_counts["rendered"],
                "fieldNames": fields,
                "rows": new_rows,
                "receipt": receipt,
            },
        )
        event_receipt = {
            "event": "buffer_extended",
            "from": previous,
            "to": page_counts["rendered"],
            **receipt,
        }
        network = list(state.get("network", [])) + [event_receipt]
        state = {
            "status": "running",
            "updatedAt": iso_now(),
            "total": page_counts["total"],
            "rendered": page_counts["rendered"],
            "fieldNames": fields,
            "formFields": form_fields(tree),
            "network": network,
        }
        atomic_json(state_path, state, private=True)
        atomic_json(
            progress_path,
            {
                "status": "running",
                "event": "buffer_extended",
                "updatedAt": state["updatedAt"],
                "total": state["total"],
                "rendered": state["rendered"],
                "lastReceipt": event_receipt,
            },
        )
        print(json.dumps({"status": "running", "total": state["total"], "rendered": state["rendered"], **event_receipt}, ensure_ascii=False), flush=True)

    rows, part_fields = read_parts(parts_dir, state["rendered"])
    field_names = [field for field in REQUIRED_FIELDS if field in part_fields]
    field_names.extend(field for field in part_fields if field not in REQUIRED_FIELDS)
    for index, row in enumerate(rows, start=1):
        row["ordinal"] = index
        row["controlIndex"] = str(index - 1)
    missing_required = [field for field in REQUIRED_FIELDS if field not in field_names]
    row_count_matches_total = len(rows) == state["total"]
    control_indexes_unique = len({row["controlIndex"] for row in rows}) == len(rows)
    completeness = summarize(rows, field_names)
    passed = (
        row_count_matches_total
        and control_indexes_unique
        and not missing_required
    )
    payload = {
        "status": "passed" if passed else "failed_validation",
        "startedAt": started_at,
        "finishedAt": iso_now(),
        "sourceUrl": TARGET_URL,
        "browser": "BitBrowser 430",
        "profileId": PROFILE_ID,
        "port": port,
        "silentBrowserMode": True,
        "browserForegroundActivationAttempted": False,
        "loginProof": proof,
        "interface": {
            "type": "ASP.NET WebForms form POST (Show All + btnExtendBuffer)",
            "endpoint": TARGET_URL,
            "method": "POST",
            "showAllField": SHOW_ALL_NAME,
            "extendEventTarget": EXTEND_EVENT_TARGET,
            "batchRows": 200,
            "cookiePersistence": "memory_only",
        },
        "total": state["total"],
        "rendered": state["rendered"],
        "extractedRowCount": len(rows),
        "rowCountMatchesTotal": row_count_matches_total,
        "controlIndexesUnique": control_indexes_unique,
        "missingRequiredFields": missing_required,
        "fieldNames": field_names,
        "completeness": completeness,
        "networkSummary": {
            "requestCount": len(state.get("network", [])),
            "elapsedMsTotal": sum(item.get("elapsedMs", 0) for item in state.get("network", [])),
            "responseBytesTotal": sum(item.get("responseBytes", 0) for item in state.get("network", [])),
        },
        "rows": rows,
    }
    atomic_json(output_path, payload)
    atomic_json(
        progress_path,
        {
            "status": payload["status"],
            "event": "completed",
            "updatedAt": iso_now(),
            "total": payload["total"],
            "extractedRowCount": payload["extractedRowCount"],
            "rowCountMatchesTotal": row_count_matches_total,
            "controlIndexesUnique": control_indexes_unique,
            "missingRequiredFields": missing_required,
            "completeness": completeness,
        },
    )
    state["status"] = payload["status"]
    state["finishedAt"] = payload["finishedAt"]
    state.pop("formFields", None)
    atomic_json(state_path, state, private=True)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "total": payload["total"],
                "extractedRowCount": payload["extractedRowCount"],
                "rowCountMatchesTotal": row_count_matches_total,
                "controlIndexesUnique": control_indexes_unique,
                "missingRequiredFields": missing_required,
                "completeness": completeness,
                "outputJson": str(output_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
