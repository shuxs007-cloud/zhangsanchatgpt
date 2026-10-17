#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


EXCLUDED_NAMES = {"FILES.tsv", "SHA256SUMS.txt"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def included_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in EXCLUDED_NAMES
        and not path.name.startswith(("._", ".~"))
    )


def build_manifest(root: Path, workers: int = 4) -> dict:
    started = time.monotonic()
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"交付目录不存在: {root}")

    files = included_files(root)
    concurrency = max(1, min(int(workers), 8, len(files) or 1))

    def describe(path: Path) -> tuple[str, int, str]:
        before = path.stat()
        relative = path.relative_to(root).as_posix()
        digest = sha256_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError(f"生成清单期间文件发生变化: {relative}")
        return relative, after.st_size, digest

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="opensms-manifest") as pool:
        rows = list(pool.map(describe, files))

    files_path = root / "FILES.tsv"
    sums_path = root / "SHA256SUMS.txt"
    files_path.write_text(
        "relative_path\tsize_bytes\tsha256\n"
        + "".join(f"{relative}\t{size}\t{digest}\n" for relative, size, digest in rows),
        encoding="utf-8",
    )
    sums_path.write_text(
        "".join(f"{digest}  {relative}\n" for relative, _, digest in rows),
        encoding="utf-8",
    )

    def verify(row: tuple[str, int, str]) -> str | None:
        relative, size, expected = row
        path = root / relative
        if not path.is_file() or path.stat().st_size != size or sha256_file(path) != expected:
            return relative
        return None

    # This is a separate complete reread, after both manifests are written.
    # Hashes from the generation pass must never substitute this verification.
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="opensms-manifest-verify") as pool:
        failures = [item for item in pool.map(verify, rows) if item is not None]
    if included_files(root) != files:
        failures.append("交付文件集合在清单生成期间发生变化")

    return {
        "result": "成功" if not failures else "失败",
        "delivery_dir": str(root),
        "file_count": len(rows),
        "files_tsv": str(files_path),
        "sha256sums": str(sums_path),
        "verification_failures": failures,
        "workers": concurrency,
        "independentHashPasses": 2,
        "durationSeconds": round(time.monotonic() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并复核OpenSMS最终交付文件清单")
    parser.add_argument("delivery_dir", type=Path)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    result = build_manifest(args.delivery_dir, args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["verification_failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
