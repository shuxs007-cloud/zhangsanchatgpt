#!/usr/bin/env python3
"""Export local project code only, without running business operations."""
import hashlib
import json
import os
from pathlib import Path
import stat
from datetime import datetime
from zoneinfo import ZoneInfo
import zipfile

ROOTS = {
    '430': Path('/Users/zhangsan/Downloads/430'),
    'shared': Path('/Users/zhangsan/Documents/数据分析'),
}
SKIP_DIRS = {
    'node_modules', '.git', '.venv', 'venv', '__pycache__', '.pytest_cache',
    'opensms_artifacts', 'opensms_runs', 'outputs', 'recovery-records',
    'profiles', 'profile', 'browser-profile', 'browser-data', 'private-session',
    'logs', 'downloads', 'test-results', 'playwright-report', '.cache',
}
SKIP_NAMES = {
    '.DS_Store', 'auth.json', 'credentials.json', 'cookies.json',
    'storage.json', 'storage-state.json', 'storage_state.json',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    volume = Path('/Volumes/zs01')
    if not volume.is_mount():
        raise RuntimeError('zs01未挂载；不回退本地')
    stamp = datetime.now(ZoneInfo('Asia/Hong_Kong')).strftime('%Y%m%dT%H%M%S%z')
    output = volume / 'chatgpt/OpenSMS-Code-Exports'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ('OpenSMS_全部代码_' + stamp + '.zip')
    manifest, excluded = [], []
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for label, root in ROOTS.items():
            for base, dirs, names in os.walk(root, followlinks=False):
                selected = []
                for name in sorted(dirs):
                    path = Path(base) / name
                    if name in SKIP_DIRS or name.startswith('.') or path.is_symlink():
                        excluded.append(str(path))
                    else:
                        selected.append(name)
                dirs[:] = selected
                for name in sorted(names):
                    path = Path(base) / name
                    rel = path.relative_to(root)
                    if (path.is_symlink() or name in SKIP_NAMES
                            or name.startswith(('.env', '._'))
                            or path.suffix.lower() in {'.pyc', '.db', '.sqlite', '.sqlite3', '.log', '.lock', '.sock', '.zip', '.xlsx', '.xls', '.csv', '.tsv', '.har', '.jsonl'}
                            or (label == 'shared' and len(rel.parts) == 1
                                and path.suffix == '.json' and name != 'opensms_rules.json')):
                        excluded.append(str(path))
                        continue
                    before = path.stat()
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    data = path.read_bytes()
                    after = path.stat()
                    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                        raise RuntimeError('源文件变化，不能确认快照：' + str(path))
                    member = label + '/' + rel.as_posix()
                    info = zipfile.ZipInfo(member)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (before.st_mode & 0xFFFF) << 16
                    z.writestr(info, data)
                    manifest.append({'path': member, 'source': str(path), 'bytes': len(data), 'sha256': digest(data)})
        readme = '''# OpenSMS 代码导出\n\n430/：主项目源码、脚本、测试、规则和项目资源。\nshared/：共用入口、绑定工具和 CloakBrowser 项目。\n\n仅导出代码，不含聊天记录、技能包、业务数据库、历史报表、浏览器配置、登录凭据、运行锁及 node_modules。部分测试表格与业务输入数据也不包含。排除项见 EXCLUDED.json。源码保持原样，可能包含内部地址或标识，请按私有代码保管。\n\n新电脑需要安装 Node/Python 及依赖，调整原用户名绝对路径，并重新登录。不得直接复用旧锁、会话或重放提交。此包不等于可直接恢复全部业务状态的迁移包。\n\n没有自动安装、自动启动或自动执行任何业务。\n'''
        z.writestr('README.md', readme)
        z.writestr('MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr('EXCLUDED.json', json.dumps(excluded, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise RuntimeError('ZIP校验失败')
        for record in manifest:
            if digest(z.read(record['path'])) != record['sha256']:
                raise RuntimeError('内容校验失败：' + record['path'])
    archive_hash = digest(archive.read_bytes())
    checksum = archive.with_suffix('.zip.sha256')
    checksum.write_text(archive_hash + '  ' + archive.name + '\n')
    receipt = archive.with_suffix('.receipt.json')
    result = {'archive': str(archive), 'bytes': archive.stat().st_size,
              'fileCount': len(manifest), 'countsByRoot': {label: sum(r['path'].startswith(label + '/') for r in manifest) for label in ROOTS},
              'sha256': archive_hash, 'checksum': str(checksum),
              'verified': True, 'businessStarted': False, 'receipt': str(receipt)}
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
