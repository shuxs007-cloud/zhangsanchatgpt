#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_THREAD_ID = "019f0968-107b-7bf2-8377-4b201aaf5bcd"
CODE_EXTENSIONS = {
    ".py",
    ".mjs",
    ".js",
    ".sh",
    ".swift",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
}
SKIP_PARTS = {
    "__pycache__",
    ".venv",
    "node_modules",
    "opensms_artifacts",
    "outputs",
    "data",
    ".git",
}
SENSITIVE_PATTERNS = {
    "possible_api_key": re.compile(r"(?i)(api[_-]?key|secret[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}"),
    "possible_password": re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{4,}"),
    "possible_bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}"),
    "admin_token_literal": re.compile(r"(?i)admin-token\s*[:=]\s*['\"][^'\"]{8,}"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(stat.S_IMODE(source.stat().st_mode))


def is_apple_metadata(path: Path) -> bool:
    return path.name == ".DS_Store" or path.name.startswith("._")


def strip_apple_metadata(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or is_apple_metadata(path):
            continue
        if hasattr(os, "listxattr"):
            try:
                for attribute in os.listxattr(path):
                    os.removexattr(path, attribute)
            except OSError:
                pass
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and is_apple_metadata(path):
            path.unlink()


def find_session(thread_id: str, codex_home: Path) -> Path:
    candidates = list((codex_home / "sessions").rglob(f"*{thread_id}.jsonl"))
    candidates.extend((codex_home / "archived_sessions").glob(f"*{thread_id}.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"找不到线程 {thread_id} 的原始会话文件")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def content_text(
    content: list[dict[str, Any]],
    attachment_dir: Path,
    attachment_prefix: str,
) -> tuple[str, list[str]]:
    chunks: list[str] = []
    attachments: list[str] = []
    image_index = 0
    for item in content:
        item_type = item.get("type")
        if item_type in {"input_text", "output_text", "text"}:
            value = item.get("text")
            if isinstance(value, str):
                chunks.append(value)
        elif item_type in {"input_image", "image"}:
            image_index += 1
            image_url = item.get("image_url") or item.get("url")
            if isinstance(image_url, str) and image_url.startswith("data:"):
                match = re.match(r"^data:([^;,]+);base64,(.*)$", image_url, re.DOTALL)
                if match:
                    mime_type, encoded = match.groups()
                    extension = mimetypes.guess_extension(mime_type) or ".bin"
                    filename = f"{attachment_prefix}_{image_index:02d}{extension}"
                    target = attachment_dir / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base64.b64decode(encoded))
                    relative = f"attachments/{filename}"
                    chunks.append(f"![会话图片]({relative})")
                    attachments.append(relative)
                    continue
            chunks.append("[会话图片保存在原始 JSONL 中，未能独立解码]")
    return "\n\n".join(chunks).strip(), attachments


def render_conversation(raw_session: Path, conversation_dir: Path) -> dict[str, Any]:
    markdown_path = conversation_dir / "CHAT_USER_ASSISTANT.md"
    messages_path = conversation_dir / "CHAT_USER_ASSISTANT.jsonl"
    attachment_dir = conversation_dir / "attachments"
    role_counts: Counter[str] = Counter()
    content_counts: Counter[str] = Counter()
    raw_type_counts: Counter[str] = Counter()
    first_timestamp = None
    last_timestamp = None
    session_meta: dict[str, Any] = {}
    readable_messages = 0
    attachment_count = 0

    with raw_session.open("r", encoding="utf-8", errors="replace") as source, \
        markdown_path.open("w", encoding="utf-8") as markdown, \
        messages_path.open("w", encoding="utf-8") as message_stream:
        markdown.write("# 当前 Codex 会话聊天记录\n\n")
        markdown.write(
            "本文件只呈现用户与助手的可见消息。完整底层事件、工具调用、"
            "系统元数据和原始图片保存在 `RAW_CODEX_SESSION.jsonl`。\n\n"
        )
        for line_number, line in enumerate(source, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = event.get("timestamp")
            if timestamp:
                first_timestamp = first_timestamp or timestamp
                last_timestamp = timestamp
            event_type = str(event.get("type", "unknown"))
            raw_type_counts[event_type] += 1
            if event_type == "session_meta" and not session_meta:
                session_meta = dict(event.get("payload") or {})
            if event_type != "response_item":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = str(payload.get("role", "unknown"))
            role_counts[role] += 1
            for item in payload.get("content") or []:
                content_counts[str(item.get("type", "unknown"))] += 1
            if role not in {"user", "assistant"}:
                continue
            readable_messages += 1
            attachment_prefix = f"message_{readable_messages:06d}"
            text, attachments = content_text(
                payload.get("content") or [], attachment_dir, attachment_prefix
            )
            attachment_count += len(attachments)
            record = {
                "sequence": readable_messages,
                "timestamp": timestamp,
                "role": role,
                "text": text,
                "attachments": attachments,
                "source_line": line_number,
            }
            message_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            label = "用户" if role == "user" else "助手"
            markdown.write(f"## {readable_messages}. {label}\n\n")
            if timestamp:
                markdown.write(f"时间：`{timestamp}`\n\n")
            markdown.write((text or "[空消息]") + "\n\n")

    summary = {
        "session_id": session_meta.get("id") or session_meta.get("session_id"),
        "source_path": str(raw_session),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "readable_message_count": readable_messages,
        "role_counts": dict(role_counts),
        "content_type_counts": dict(content_counts),
        "raw_event_type_counts": dict(raw_type_counts),
        "attachment_count": attachment_count,
        "session_meta": {
            key: session_meta.get(key)
            for key in (
                "id",
                "session_id",
                "timestamp",
                "cwd",
                "originator",
                "cli_version",
                "source",
                "model_provider",
            )
        },
    }
    write_text(
        conversation_dir / "CONVERSATION_SUMMARY.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )
    return summary


def should_copy_current(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in SKIP_PARTS for part in relative.parts):
        return False
    if path.name == ".env" or path.suffix in {".pyc", ".xlsx", ".xls", ".zip"}:
        return False
    return path.is_file() or path.is_symlink()


def should_copy_legacy(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in SKIP_PARTS for part in relative.parts):
        return False
    if not path.is_file():
        return False
    if relative.parts[0] == "opensms-10day-audit-skill":
        return path.suffix in CODE_EXTENSIONS
    if relative.parts[0] == "scripts":
        return path.suffix in CODE_EXTENSIONS
    return len(relative.parts) == 1 and path.name.startswith("opensms") and path.suffix in CODE_EXTENSIONS


def copy_filtered_tree(source: Path, destination: Path, predicate) -> int:
    if not source.is_dir():
        return 0
    copied = 0
    for path in source.rglob("*"):
        if not predicate(path, source):
            continue
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            continue
        copy_file(path, target)
        copied += 1
    return copied


def copy_skills(codex_home: Path, home: Path, destination: Path, legacy_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    codex_skills = codex_home / "skills"
    target = destination / "user_codex"
    count = 0
    if codex_skills.is_dir():
        for child in codex_skills.iterdir():
            if child.name == ".system" or not child.is_dir():
                continue
            count += copy_filtered_tree(child, target / child.name, lambda p, r: p.is_file())
    counts["user_codex"] = count

    agent_skills = home / ".agents" / "skills"
    counts["agents"] = copy_filtered_tree(
        agent_skills, destination / "agents", lambda p, r: p.is_file()
    )
    memory_skills = codex_home / "memories" / "skills"
    counts["memory_skills"] = copy_filtered_tree(
        memory_skills, destination / "memory_skills", lambda p, r: p.is_file()
    )
    workspace_skill = legacy_root / "opensms-10day-audit-skill"
    counts["opensms_workspace"] = copy_filtered_tree(
        workspace_skill,
        destination / "workspace" / "opensms-10day-audit-skill",
        lambda p, r: p.is_file(),
    )
    return counts


def copy_bundled_skill_snapshots(codex_home: Path, destination: Path) -> dict[str, int]:
    snapshot_root = destination / "bundled_snapshots"
    snapshot_count = 0
    copied_files = 0

    system_root = codex_home / "skills" / ".system"
    if system_root.is_dir():
        for skill_path in sorted(system_root.rglob("SKILL.md")):
            skill_dir = skill_path.parent
            relative = skill_dir.relative_to(system_root)
            copied_files += copy_filtered_tree(
                skill_dir,
                snapshot_root / "system" / relative,
                lambda path, root: path.is_file(),
            )
            snapshot_count += 1

    plugin_root = codex_home / "plugins" / "cache"
    if plugin_root.is_dir():
        seen: set[str] = set()
        for skill_path in sorted(plugin_root.rglob("SKILL.md")):
            skill_dir = skill_path.parent
            resolved = str(skill_dir.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            relative = skill_dir.relative_to(plugin_root)
            copied_files += copy_filtered_tree(
                skill_dir,
                snapshot_root / "plugin_cache" / relative,
                lambda path, root: path.is_file(),
            )
            snapshot_count += 1

    return {"snapshot_count": snapshot_count, "file_count": copied_files}


def skill_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                return line.lstrip("# ").strip()
    except OSError:
        pass
    return path.parent.name


def write_skill_inventory(codex_home: Path, home: Path, destination: Path) -> int:
    roots = [
        codex_home / "skills",
        home / ".agents" / "skills",
        codex_home / "memories" / "skills",
        codex_home / "plugins" / "cache",
    ]
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for skill_path in root.rglob("SKILL.md"):
            resolved = str(skill_path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            source_kind = (
                "plugin_or_bundled"
                if "plugins/cache" in resolved or "/skills/.system/" in resolved
                else "local"
            )
            rows.append((skill_title(skill_path), source_kind, resolved, sha256_file(skill_path)))
    inventory = destination / "SKILLS_INSTALLED.tsv"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    with inventory.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["skill", "source_kind", "original_path", "skill_md_sha256"])
        writer.writerows(sorted(rows))
    return len(rows)


def copy_automations(codex_home: Path, destination: Path) -> int:
    source = codex_home / "automations"
    count = 0
    if not source.is_dir():
        return count
    for config in source.glob("*/automation.toml"):
        copy_file(config, destination / config.parent.name / "automation.toml")
        count += 1
    return count


def copy_latest_api_package(workspace: Path, destination: Path) -> list[str]:
    archives = sorted(
        (workspace / "dist").glob("opensms_document_api_*.zip"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not archives:
        return []
    latest = archives[-1]
    copied: list[str] = []
    for source in (latest, Path(str(latest) + ".sha256")):
        if source.is_file():
            target = destination / source.name
            copy_file(source, target)
            copied.append(str(target))
    return copied


def scan_sensitive(root: Path, output: Path) -> int:
    findings: list[tuple[str, int, str]] = []
    excluded = {"RAW_CODEX_SESSION.jsonl", "CHAT_USER_ASSISTANT.jsonl", "CHAT_USER_ASSISTANT.md"}
    for path in root.rglob("*"):
        if not path.is_file() or path.name in excluded or path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in SENSITIVE_PATTERNS.items():
                if pattern.search(line):
                    findings.append((path.relative_to(root).as_posix(), line_number, label))
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["path", "line", "finding"])
        writer.writerows(findings)
    return len(findings)


def write_handoff(root: Path, thread_id: str, summary: dict[str, Any]) -> None:
    write_text(
        root / "CONTEXT_HANDOFF.md",
        f"""# OpenSMS 会话迁移交接

## 会话

- 线程 ID：`{thread_id}`
- 标题：`10日报表巡检`
- 原工作目录：`/Users/zhangsan/Downloads/430`
- 可读消息数：{summary['readable_message_count']}
- 原始记录截至：`{summary['last_timestamp']}`

## 当前功能状态

- OpenSMS 10 日报表巡检规则、卡槽关联、运营商流转、E 区备份恢复、余量检查和任务B授权规则均保存在原始会话与自动化模板中。
- 新增远程文档处理接口：上传本轮 export4day、Google 关联表和后台服务器日期，远端生成并完整复核，之后下载 Excel、回执与完整 ZIP。
- 远程文档接口是只读处理器，不登录后台、不写运营商、不执行任务B。
- 任务B必须继续满足“当前交互明确授权 + 环境变量”双重条件。

## 迁移后继续工作

1. 先阅读 `conversation/CHAT_USER_ASSISTANT.md` 和本文件。
2. 将 `code/current_430` 恢复到新工作目录。
3. 按 `RESTORE_GUIDE.md` 安装 Skill；自动化先保持关闭。
4. 在新电脑重新登录 BitBrowser 430，并重新配置验证码服务。不要迁移 Cookie、Admin-Token 或浏览器登录数据库。
5. 将 `automations/opensms-10-2/automation.toml` 与新电脑路径核对后再启用，同时停用旧电脑上的同一自动化，防止双机重复写入。

## 目录异常记录

本次开发期间，原 `430` 目录被外部操作移至 `/Users/zhangsan/.Trash/430`。迁移包将其中的 OpenSMS 程序代码作为 `code/legacy_430_code` 收录，但不会收录旧报表、回执、日志、Cookie 或浏览器状态。
""",
    )
    write_text(
        root / "SECURITY_NOTICE.md",
        """# 安全说明

- `conversation/RAW_CODEX_SESSION.jsonl` 是完整原始会话，可能包含业务号码、后台路径、工具输出和历史敏感信息。只通过可信加密介质传输。
- 可读版聊天没有主动删改业务内容，但不展示系统/开发者消息和内部推理；完整内容仍在原始 JSONL。
- 迁移包刻意不包含 `.env`、Cookie、Admin-Token、BitBrowser 配置、浏览器用户目录、验证码图片和历史报表数据。
- `SENSITIVE_SCAN.tsv` 是对代码和自动化模板的启发式扫描结果；迁移前仍应人工复核。
""",
    )


def write_restore_files(root: Path) -> None:
    write_text(
        root / "RESTORE_GUIDE.md",
        """# 恢复指南

## 1. 校验

```bash
shasum -a 256 -c SHA256SUMS.txt
```

## 2. 恢复工作目录、聊天归档与 Skill

```bash
./restore_migration.sh "$HOME/Downloads/430"
```

恢复脚本使用 `--ignore-existing`，不会覆盖目标电脑已有文件。冲突文件需人工比较。聊天、交接文档、自动化模板、全部 Skill 快照和文档接口部署包会复制到工作目录下的 `migration_bundle/`。

## 3. 会话

Codex 目前没有安全、稳定的“导入原始 JSONL 并还原同一任务”公开接口。不要直接把原始 JSONL 塞进新电脑的内部会话索引。

在新电脑创建一个新任务，附上 `CONTEXT_HANDOFF.md` 和 `conversation/CHAT_USER_ASSISTANT.md`；`RAW_CODEX_SESSION.jsonl` 作为完整归档保留。

## 4. 自动化

`automations/` 中是模板。先核对目录、时区、BitBrowser 环境和外接盘路径，再手工放入新电脑的 `~/.codex/automations/`。启用新电脑前先停用旧电脑上的同名任务。

## 5. Codex 自带与插件 Skill

`skills/bundled_snapshots/` 保留了本机当前所有 Codex 自带及插件 Skill 的离线源码快照。恢复脚本不会把它们直接注入 Codex 内部插件缓存；应优先在新电脑安装清单中的同名插件。快照用于离线查阅、版本比对和手工恢复。

## 6. 文档接口

部署包位于 `deliverables/`。解压后依照包内 `README.md` 执行 `install.sh`，并在新电脑重新设置 Token。
""",
    )
    restore = """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTINATION="${1:-$HOME/Downloads/430}"
MIGRATION_DESTINATION="$DESTINATION/migration_bundle"
mkdir -p "$DESTINATION" "$MIGRATION_DESTINATION" "$HOME/.codex/skills" "$HOME/.agents/skills" "$HOME/.codex/memories/skills"

rsync -a --ignore-existing "$ROOT/code/current_430/" "$DESTINATION/"
rsync -a --ignore-existing "$ROOT/code/legacy_430_code/" "$DESTINATION/"
rsync -a --ignore-existing "$ROOT/skills/user_codex/" "$HOME/.codex/skills/"
rsync -a --ignore-existing "$ROOT/skills/agents/" "$HOME/.agents/skills/"
rsync -a --ignore-existing "$ROOT/skills/memory_skills/" "$HOME/.codex/memories/skills/"
rsync -a --ignore-existing "$ROOT/conversation/" "$MIGRATION_DESTINATION/conversation/"
rsync -a --ignore-existing "$ROOT/automations/" "$MIGRATION_DESTINATION/automations/"
rsync -a --ignore-existing "$ROOT/deliverables/" "$MIGRATION_DESTINATION/deliverables/"
rsync -a --ignore-existing "$ROOT/skills/" "$MIGRATION_DESTINATION/skills/"

for document in CONTEXT_HANDOFF.md RESTORE_GUIDE.md SECURITY_NOTICE.md MIGRATION_SUMMARY.json SENSITIVE_SCAN.tsv SKILLS_INSTALLED.tsv FILES.tsv SHA256SUMS.txt; do
  if [[ -f "$ROOT/$document" ]]; then
    rsync -a --ignore-existing "$ROOT/$document" "$MIGRATION_DESTINATION/$document"
  fi
done

printf '工作目录、聊天归档与 Skill 已恢复（未覆盖已有文件）。\n'
printf '聊天与交接文档位于：%s\n' "$MIGRATION_DESTINATION"
printf '自动化模板未自动启用，请阅读 RESTORE_GUIDE.md。\n'
"""
    write_text(root / "restore_migration.sh", restore)
    (root / "restore_migration.sh").chmod(0o755)


def file_manifest(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    excluded = {"FILES.tsv", "SHA256SUMS.txt"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in excluded or is_apple_metadata(path):
            continue
        records.append(
            {
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "path": path.relative_to(root).as_posix(),
            }
        )
    with (root / "FILES.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["sha256", "size", "path"])
        writer.writerows((r["sha256"], r["size"], r["path"]) for r in records)
    write_text(
        root / "SHA256SUMS.txt",
        "".join(f"{r['sha256']}  {r['path']}\n" for r in records),
    )
    return records


def build_zip(source_root: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or is_apple_metadata(path):
                continue
            archive_name = (Path(source_root.name) / path.relative_to(source_root)).as_posix()
            archive.write(path, archive_name)
    with zipfile.ZipFile(archive_path) as archive:
        broken = archive.testzip()
        if broken:
            raise RuntimeError(f"迁移 ZIP 校验失败: {broken}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出当前 OpenSMS Codex 会话迁移包")
    parser.add_argument("--thread-id", default=DEFAULT_THREAD_ID)
    parser.add_argument("--session", type=Path)
    parser.add_argument("--visible-only", action="store_true", help="合并同一会话所有本地段，只导出可见聊天、脚本与技能；不导出原始内部事件")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("/Volumes/zs01/chatgpt/OpenSMS-Migration"),
    )
    parser.add_argument(
        "--workspace", type=Path, default=Path("/Users/zhangsan/Downloads/430")
    )
    parser.add_argument(
        "--legacy-workspace", type=Path, default=Path("/Users/zhangsan/.Trash/430")
    )
    return parser


BACKUP_SKIP={"node_modules",".git",".venv","__pycache__","private-session","profile","profiles","recovery-records","opensms_artifacts","dist","data"}
BACKUP_CODE_EXT={".py",".mjs",".js",".cjs",".sh",".zsh",".bash",".swift",".ts",".tsx",".md",".toml",".yaml",".yml"}
REDACTIONS=[
    ("api-key",re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b')),
    ("jwt",re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{15,}\b')),
    ("bearer",re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}')),
    ("credential-literal",re.compile(r'''(?ix)((?:["']?)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|admin[_-]?token|password|passwd|pwd|cookie|sessionid)(?:["']?)\s*[:=]\s*)(["'])([^"'\r\n]{4,})\2''')),
    ("credential-header",re.compile(r'(?im)^\s*(?:Cookie|Admin-Token|Authorization)\s*:\s*[^\r\n]{8,}$')),
]

def redact_backup_text(text: str) -> tuple[str, dict[str,int]]:
    counts={}
    for label,pattern in REDACTIONS:
        if label=="credential-literal":replacement=lambda m:m[1]+m[2]+"[REDACTED]"+m[2]
        elif label=="bearer":replacement=lambda m:m[1]+"[REDACTED]"
        else:replacement="[REDACTED_CREDENTIAL]"
        text,count=pattern.subn(replacement,text)
        if count:counts[label]=count
    return text,counts

def backup_paths(root: Path, code_only: bool=False) -> list[Path]:
    if not root.is_dir():return []
    result=[]
    for base,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if d not in BACKUP_SKIP]
        for name in names:
            p=Path(base)/name
            if is_apple_metadata(p) or name.startswith('.env') or name.lower() in {'auth.json','credentials.json','storage.json','cookies.json'}:continue
            if code_only and p.suffix not in BACKUP_CODE_EXT and p.name!='opensms':continue
            if p.is_file():result.append(p)
    return sorted(result)

def export_visible_segments(paths: list[Path], destination: Path, thread_id: str) -> dict[str,Any]:
    """Only user-visible messages; never serialize raw reasoning or instructions."""
    messages={};segments=[];images={};skipped=Counter();sensitive=Counter()
    for n,path in enumerate(paths,1):
        before=path.stat();snapshot_bytes=before.st_size;count=0;invalid=0
        digest=hashlib.sha256()
        with path.open('rb') as source:
            first=source.readline();digest.update(first);meta=json.loads(first).get('payload',{})
            if meta.get('id')!=thread_id:continue
            while source.tell()<snapshot_bytes:
                line_number=count+2;line=source.readline()
                if not line:break
                if source.tell()>snapshot_bytes:break
                digest.update(line);count+=1
                # Large screenshots/tool traces are not parsed or retained.
                prefix=line[:600]
                if b'"response_item"' not in prefix or b'"message"' not in prefix:continue
                try:event=json.loads(line)
                except json.JSONDecodeError:invalid+=1;continue
                payload=event.get('payload',{})
                if payload.get('type')!='message' or payload.get('role') not in {'user','assistant'}:continue
                role=payload['role'];channel=payload.get('channel');phase=payload.get('phase')
                if role=='assistant' and (channel not in (None,'final','commentary') or phase not in (None,'final','final_answer','commentary')):
                    skipped['non-visible-assistant']+=1;continue
                chunks=[];media=[]
                for item in payload.get('content') or []:
                    if item.get('type') in {'input_text','output_text','text'}:
                        value=item.get('text','')
                        if not isinstance(value,str):continue
                        if value.lstrip().startswith(('<environment_context>','<permissions instructions>','<multi_agent_role>','# AGENTS.md instructions')):
                            skipped['environment-metadata']+=1;continue
                        clean,redacted=redact_backup_text(value);sensitive.update(redacted);chunks.append(clean)
                    elif item.get('type') in {'input_image','image','output_image'}:
                        url=item.get('image_url') or item.get('url')
                        if isinstance(url,dict):url=url.get('url')
                        match=re.match(r'^data:(image/(?:png|jpeg|gif|webp));base64,(.+)$',url or '',re.S)
                        if match:
                            raw=base64.b64decode(match[2]);key=hashlib.sha256(raw).hexdigest();extension=mimetypes.guess_extension(match[1]) or '.bin'
                            images[key]=(raw,extension);ref='attachments/'+key+extension;media.append(ref);chunks.append('[会话图片：'+ref+']')
                        else:chunks.append('[外部图片引用，未下载]')
                text='\n\n'.join(chunks).strip()
                if not text and not media:continue
                identifier=payload.get('id') or hashlib.sha256((str(event.get('timestamp'))+role+text).encode()).hexdigest()
                record=dict(messageId=identifier,timestamp=event.get('timestamp'),role=role,phase=phase or channel or 'message',text=text,attachments=media,
                    sourceFile=path.name,sourceLine=line_number)
                if identifier in messages:
                    if messages[identifier]['text']!=text:skipped['message-id-content-variant']+=1
                else:messages[identifier]=record
        segments.append(dict(path=str(path),snapshotBytes=snapshot_bytes,readBytes=source.tell() if not source.closed else snapshot_bytes,
            sha256=digest.hexdigest(),eventLines=count+1,invalidJsonLines=invalid,sessionId=meta['id']))
        if n%10==0:print(json.dumps(dict(event='backup_chat_progress',segments=n,total=len(paths),visibleMessages=len(messages))),flush=True)
    destination.mkdir(parents=True)
    for key,(raw,extension) in images.items():
        p=destination/'attachments'/(key+extension);p.parent.mkdir(exist_ok=True);p.write_bytes(raw)
    ordered=sorted(messages.values(),key=lambda m:(m['timestamp'] or '',m['sourceFile'],m['sourceLine']))
    with (destination/'CHAT_USER_ASSISTANT.jsonl').open('w') as stream,(destination/'聊天记录.txt').open('w') as readable:
        readable.write('10日报表巡检：用户可见聊天归档\n\n时间显示为北京时间（UTC+8）。已排除内部推理、系统/开发者指令、工具原始输出；凭据文本脱敏。\n\n')
        for sequence,message in enumerate(ordered,1):
            message['sequence']=sequence
            stream.write(json.dumps(message,ensure_ascii=False)+'\n')
            timestamp=datetime.fromisoformat(message['timestamp'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Hong_Kong')).isoformat() if message['timestamp'] else '未知'
            readable.write(f"[{sequence}] {timestamp} {'用户' if message['role']=='user' else '助手'}\n{message['text']}\n\n")
    summary=dict(threadId=thread_id,segments=len(segments),messageCount=len(ordered),roleCounts=dict(Counter(m['role'] for m in ordered)),images=len(images),
        firstTimestamp=ordered[0]['timestamp'] if ordered else None,lastTimestamp=ordered[-1]['timestamp'] if ordered else None,
        excluded=dict(skipped),credentialRedactions=dict(sensitive),sourceSegments=segments,
        scope='all-locally-available-segments-with-exact-session-id; user-visible content only')
    write_text(destination/'CONVERSATION_SUMMARY.json',json.dumps(summary,ensure_ascii=False,indent=2))
    return summary

def build_visible_backup(args) -> int:
    home=Path('/Users/zhangsan');codex_home=home/'.codex'
    mounts=subprocess.check_output(['mount'],text=True)
    if ' on /Volumes/zs01 (' not in mounts:raise RuntimeError('zs01未挂载，停止备份；不回退本地')
    output=args.output_root.resolve()
    if not str(output).startswith('/Volumes/zs01/'):raise ValueError('备份输出固定在zs01')
    stamp=datetime.now(ZoneInfo('Asia/Hong_Kong')).strftime('%Y%m%dT%H%M%S%z')
    root=output/('OpenSMS_会话备份_'+stamp);root.mkdir(parents=True,exist_ok=False)
    paths=[]
    for folder in (codex_home/'sessions',codex_home/'archived_sessions'):
        if folder.exists():paths.extend(p for p in folder.rglob('*.jsonl') if args.thread_id in p.name)
    paths=sorted(set(paths))
    if not paths:raise FileNotFoundError('找不到本会话的本地记录')
    conversation=export_visible_segments(paths,root/'conversation',args.thread_id)
    copied=[];redactions=[];errors=[]
    def safe_copy(source: Path, target: Path, category: str):
        try:
            raw=source.read_bytes();original_hash=hashlib.sha256(raw).hexdigest()
            try:
                text=raw.decode('utf-8');clean,findings=redact_backup_text(text);data=clean.encode('utf-8')
            except UnicodeDecodeError:data=raw;findings={}
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            target.chmod(stat.S_IMODE(source.stat().st_mode))
            copied.append(dict(source=str(source),backup=target.relative_to(root).as_posix(),category=category,sourceSha256=original_hash,
                backupSha256=hashlib.sha256(data).hexdigest(),redacted=bool(findings)))
            if findings:redactions.append(dict(path=target.relative_to(root).as_posix(),patterns=findings))
        except (OSError,ValueError) as error:errors.append(dict(path=str(source),error=str(error)))
    code_roots=[(args.workspace.resolve(),'current_430'),(home/'Documents/数据分析','shared_数据分析')]
    for source_root,label in code_roots:
        for p in backup_paths(source_root,code_only=True):safe_copy(p,root/'code'/label/p.relative_to(source_root),'code')
    # Include local personal skills and installed bundled/plugin skill sources.
    skill_roots=[(codex_home/'skills','system_and_user'),(home/'.agents/skills','agents'),(codex_home/'memories/skills','opensms_local'),
        (codex_home/'plugins/cache','plugin_cache'),(home/'.cache/codex-runtimes/codex-primary-runtime/plugins','runtime_plugins')]
    seen=set();skill_inventory=[]
    for skill_root,label in skill_roots:
        if not skill_root.exists():continue
        found=subprocess.run(['rg','--files','--hidden',str(skill_root),'-g','SKILL.md','-g','!**/node_modules/**'],capture_output=True,text=True,check=False)
        for name in sorted(found.stdout.splitlines()):
            manifest=Path(name);resolved=str(manifest.resolve())
            if resolved in seen:continue
            seen.add(resolved);directory=manifest.parent;target=root/'skills'/label/directory.relative_to(skill_root)
            for p in backup_paths(directory):safe_copy(p,target/p.relative_to(directory),'skill')
            skill_inventory.append(dict(name=directory.name,source=str(manifest),backup=(target/'SKILL.md').relative_to(root).as_posix()))
    # Spreadsheet/PDF helpers can be adjacent to (not inside) skills folders.
    for plugin in ('spreadsheets','documents','pdf','presentations','template-creator'):
        plugin_root=home/'.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime/plugins'/plugin
        for child in ('container_tools','scripts','assets'):
            for p in backup_paths(plugin_root/child):safe_copy(p,root/'skills'/'runtime_support'/plugin/child/p.relative_to(plugin_root/child),'skill-helper')
    # User-provided local attachments referenced by visible chat only.
    attachments=set()
    for line in (root/'conversation/CHAT_USER_ASSISTANT.jsonl').read_text().splitlines():
        text=json.loads(line)['text']
        attachments.update(re.findall(r'/Users/zhangsan/\.codex/attachments/[^\r\n"<>]+',text))
    for name in attachments:
        p=Path(name.strip().rstrip('`）)'))
        if p.is_file() and p.suffix.lower() in {'.txt','.csv','.tsv','.png','.jpg','.jpeg','.webp','.pdf','.xlsx','.docx'}:
            safe_copy(p,root/'conversation/user_files'/p.relative_to(codex_home/'attachments'),'user-attachment')
    write_text(root/'skills/SKILLS_INVENTORY.json',json.dumps(skill_inventory,ensure_ascii=False,indent=2))
    write_text(root/'SOURCE_MAP.json',json.dumps(copied,ensure_ascii=False,indent=2))
    write_text(root/'SECURITY_SCAN.json',json.dumps(dict(redactions=redactions,readErrors=errors,
        excluded=['internal reasoning','system/developer instructions','raw tool output','browser profiles','cookie/auth/.env files','business databases','Task F database','Task I database','historical business reports']),ensure_ascii=False,indent=2))
    write_text(root/'README_备份说明.txt',f'''OpenSMS 会话备份\n线程：{args.thread_id}\n创建时间：{stamp}（北京时间）\n\n包含：\n1. conversation/聊天记录.txt 与 CHAT_USER_ASSISTANT.jsonl：本机可获取的{conversation['segments']}段同一会话记录合并、按消息ID去重，{conversation['messageCount']}条可见消息，{conversation['images']}张内嵌会话图片。\n2. code/：当前430与共享数据分析目录中的可恢复代码、测试、规则文档。保留目录结构。\n3. skills/：{len(skill_inventory)}个本地、系统、插件技能源码快照及辅助工具。\n4. SOURCE_MAP.json、FILES.tsv、SHA256SUMS.txt：来源映射、文件清单、校验值。\n\n限制：\n- 仅包括本地现存会话段；不承诺恢复已删除或从未落盘的消息。\n- 不包含内部推理、系统/开发者指令、工具原始输出；没有导出原始内部JSONL。\n- 登录凭据文本已脱敏，浏览器登录目录、业务数据库、F库、I索引库和历史报表不在此包中。\n- 有些旧脚本已不存在；本包是当前代码快照，不是每个历史修订版本。\n- Skills快照包含自带/插件资源，不能只复制它们就恢复账号连接。恢复时优先重新安装同名插件，参考本包内源码。\n- 脚本内绝对路径、外接盘、Python/Node依赖需在新电脑重新核对，登录需重新进行。\n- 请勿将聊天归档直接写入Codex内部注册数据库；聊天记录用于阅读和新会话接续。\n- 本包含业务号码与聊天内容，请存放在可信加密介质。\n\n校验：解压后在包目录运行 shasum -a 256 -c SHA256SUMS.txt\n官方说明：https://learn.chatgpt.com/docs/config-file/config-advanced 与 https://learn.chatgpt.com/docs/build-skills\n''')
    result=dict(status='passed' if not errors else 'passed-with-read-errors',threadId=args.thread_id,title='10日报表巡检',exportedAt=stamp,
        conversation={k:v for k,v in conversation.items() if k!='sourceSegments'},codeFiles=sum(r['category']=='code' for r in copied),
        skillCount=len(skill_inventory),skillFiles=sum(r['category'] in ('skill','skill-helper') for r in copied),
        readErrors=errors,redactedFiles=len(redactions),businessWrites=0,root=str(root))
    write_text(root/'BACKUP_SUMMARY.json',json.dumps(result,ensure_ascii=False,indent=2))
    records=file_manifest(root);archive=output/(root.name+'.zip');build_zip(root,archive)
    sha=sha256_file(archive);write_text(Path(str(archive)+'.sha256'),f'{sha}  {archive.name}\n')
    with zipfile.ZipFile(archive) as z:
        for r in records:
            if hashlib.sha256(z.read(root.name+'/'+r['path'])).hexdigest()!=r['sha256']:raise RuntimeError('备份归档SHA256不一致')
        if any('RAW_CODEX_SESSION' in n or n.lower().endswith(('/auth.json','/cookies.json','.sqlite3','.db')) for n in z.namelist()):raise RuntimeError('备份归档包含禁止项')
    print(json.dumps(dict(result,archive=str(archive),archiveBytes=archive.stat().st_size,archiveSha256=sha,verifiedFiles=len(records)),ensure_ascii=False),flush=True)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.visible_only:return build_visible_backup(args)
    home = Path.home()
    codex_home = home / ".codex"
    session_source = (
        args.session.expanduser().resolve()
        if args.session
        else find_session(args.thread_id, codex_home)
    )
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if not os.access(output_root, os.W_OK):
        raise SystemExit(f"输出目录不可写: {output_root}")

    stamp = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y%m%dT%H%M%S%z")
    run_root = output_root / stamp
    if run_root.exists():
        raise SystemExit(f"拒绝覆盖既有迁移目录: {run_root}")
    run_root.mkdir(parents=True)

    conversation_dir = run_root / "conversation"
    raw_copy = conversation_dir / "RAW_CODEX_SESSION.jsonl"
    source_size_before = session_source.stat().st_size
    copy_file(session_source, raw_copy)
    source_size_after = session_source.stat().st_size
    summary = render_conversation(raw_copy, conversation_dir)
    summary["source_size_before_copy"] = source_size_before
    summary["source_size_after_copy"] = source_size_after
    summary["copied_size"] = raw_copy.stat().st_size
    write_text(
        conversation_dir / "CONVERSATION_SUMMARY.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )

    workspace = args.workspace.expanduser().resolve()
    legacy = args.legacy_workspace.expanduser().resolve()
    current_count = copy_filtered_tree(
        workspace, run_root / "code" / "current_430", should_copy_current
    )
    legacy_count = copy_filtered_tree(
        legacy, run_root / "code" / "legacy_430_code", should_copy_legacy
    )
    deliverables = copy_latest_api_package(workspace, run_root / "deliverables")
    skill_counts = copy_skills(codex_home, home, run_root / "skills", legacy)
    bundled_skill_snapshots = copy_bundled_skill_snapshots(
        codex_home, run_root / "skills"
    )
    skill_inventory_count = write_skill_inventory(codex_home, home, run_root / "skills")
    automation_count = copy_automations(codex_home, run_root / "automations")
    write_handoff(run_root, args.thread_id, summary)
    write_restore_files(run_root)
    sensitive_count = scan_sensitive(run_root, run_root / "SENSITIVE_SCAN.tsv")

    migration_summary = {
        "result": "success",
        "thread_id": args.thread_id,
        "exported_at": stamp,
        "source_session": str(session_source),
        "conversation": summary,
        "code_files": {"current_430": current_count, "legacy_430_code": legacy_count},
        "skill_files": skill_counts,
        "bundled_skill_snapshots": bundled_skill_snapshots,
        "installed_skill_inventory_count": skill_inventory_count,
        "automation_template_count": automation_count,
        "deliverables": deliverables,
        "sensitive_scan_finding_count": sensitive_count,
    }
    write_text(
        run_root / "MIGRATION_SUMMARY.json",
        json.dumps(migration_summary, ensure_ascii=False, indent=2) + "\n",
    )
    strip_apple_metadata(run_root)
    records = file_manifest(run_root)
    strip_apple_metadata(run_root)
    archive_path = output_root / f"opensms_session_migration_{stamp}.zip"
    build_zip(run_root, archive_path)
    if hasattr(os, "listxattr"):
        try:
            for attribute in os.listxattr(archive_path):
                os.removexattr(archive_path, attribute)
        except OSError:
            pass
    archive_sha = sha256_file(archive_path)
    archive_sha_path = Path(str(archive_path) + ".sha256")
    write_text(archive_sha_path, f"{archive_sha}  {archive_path.name}\n")
    if hasattr(os, "listxattr"):
        try:
            for attribute in os.listxattr(archive_sha_path):
                os.removexattr(archive_sha_path, attribute)
        except OSError:
            pass
    for metadata in output_root.glob(f"._{archive_path.name}*"):
        metadata.unlink()

    result = {
        **migration_summary,
        "run_root": str(run_root),
        "archive_path": str(archive_path),
        "archive_size": archive_path.stat().st_size,
        "archive_sha256": archive_sha,
        "manifest_file_count": len(records),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
