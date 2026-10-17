#!/usr/bin/env python3
"""Offline, visible-conversation and operational-state migration archive.

Never exports internal reasoning, browser profiles, passwords or running locks.
Never registers a task, makes a business request or mutates a production DB.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from opensms_export_session_migration import (
    build_zip, export_visible_segments, file_manifest, redact_backup_text,
    sha256_file, write_text, REDACTIONS,
)

# Older visible-chat exporter covers English credential keys but not this form.
REDACTIONS.append(('chinese-password', re.compile(r'(?:密码|密碼)\s*[:：=]\s*[^\s\n，,；;]{4,}')))

OLD_HOME = Path('/Users/zhangsan')
WORK = OLD_HOME / 'Downloads/430'
THREAD = '01a04f47-db4e-7220-be57-eb543fdb8141'
SKIP_DIRS = {'node_modules', '.git', '.venv', '__pycache__', '.pytest_cache',
             'profiles', 'profile', 'private-session', 'browser-profile',
             'browser-data', 'Cookies', 'Cache', 'dist', 'build'}
SKIP_NAMES = {'auth.json', 'credentials.json', 'cookies.json', 'storage.json',
              'storage-state.json', 'storage_state.json', '.DS_Store'}
TEXT_EXTS = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.sh', '.zsh', '.bash',
             '.md', '.txt', '.json', '.jsonl', '.toml', '.yaml', '.yml', '.html',
             '.css', '.csv', '.tsv', '.xml', '.swift'}

RESTORE_PY = r'''#!/usr/bin/env python3
"""Offline restore; refuses overwrites and never runs business tasks."""
import argparse, hashlib, json, os, platform, re, shutil, subprocess, sys
from pathlib import Path

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''):h.update(b)
    return h.hexdigest()

def verify(root):
    records=json.loads((root/'VERIFY_MANIFEST.json').read_text())
    for record in records:
        p=(root/record['path']).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file():raise RuntimeError('Missing/unsafe archive member: '+record['path'])
        if digest(p)!=record['sha256']:raise RuntimeError('Checksum mismatch: '+record['path'])
    return len(records)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--verify-only',action='store_true')
    parser.add_argument('--home',type=Path,default=Path.home())
    args=parser.parse_args(); root=Path(__file__).resolve().parent
    count=verify(root)
    if args.verify_only:
        print(json.dumps({'verified':True,'files':count}));return
    if platform.system()!='Darwin':raise RuntimeError('This restore is for macOS only.')
    target_home=args.home.expanduser().resolve()
    work=target_home/'Downloads/430'; shared=target_home/'Documents/数据分析'
    for target in (work,shared):
        if target.exists() and any(target.iterdir()):raise RuntimeError('Refusing to overwrite existing directory: '+str(target))
    # All guards pass before the first destination write.
    for source,target in ((root/'code/current_430',work),(root/'code/shared_数据分析',shared)):
        if source.exists():shutil.copytree(source,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('._*','.DS_Store'))
    source_state=root/'core_state/opensms_artifacts'
    if source_state.exists():shutil.copytree(source_state,work/'opensms_artifacts',dirs_exist_ok=True,ignore=shutil.ignore_patterns('._*','.DS_Store'))
    rewrite=[]
    for directory in (work,shared):
        for p in directory.rglob('*'):
            if not p.is_file() or 'opensms_artifacts' in p.parts or p.suffix not in {'.py','.mjs','.js','.sh','.json','.toml','.yaml','.yml'}:continue
            try:text=p.read_text()
            except UnicodeDecodeError:continue
            updated=text.replace('/Users/zhangsan',str(target_home))
            if updated!=text:p.write_text(updated);rewrite.append(str(p))
    # Portable package copies are not injected into private plugin caches.
    migration=work/'migration_bundle';migration.mkdir(exist_ok=True)
    for name in ('conversation','skills','automations','evidence','environment'):
        source=root/name
        if source.exists():shutil.copytree(source,migration/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('._*','.DS_Store'))
    for name in ('开始恢复.md','CONTEXT_HANDOFF.md','BACKUP_SUMMARY.json','EXCLUDED_AND_LIMITATIONS.json'):
        if (root/name).exists():shutil.copy2(root/name,migration/name)
    skill_destination=target_home/'.codex/skills'
    restored_skills=[]
    for group in ('user','agents','memory'):
        source=root/'skills'/group
        if not source.exists():continue
        for skill in source.iterdir():
            if not skill.is_dir() or not (skill/'SKILL.md').exists():continue
            target=skill_destination/skill.name
            if target.exists():continue
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(skill,target,ignore=shutil.ignore_patterns('._*','.DS_Store'))
            restored_skills.append(skill.name)
    dependency=target_home/'.cache/codex-runtimes/codex-primary-runtime/dependencies'
    node=dependency/'node/bin/node'; py=dependency/'python/bin/python3'
    modules=dependency/'node/node_modules'
    if modules.exists() and not (work/'node_modules').exists():(work/'node_modules').symlink_to(modules,target_is_directory=True)
    # Link to an existing drive only. Never create a local bulk-data fallback.
    links={work/'outputs':Path('/Volumes/zs01/chatgpt/OpenSMS/local-work/outputs'),
           work/'opensms_artifacts/exports':Path('/Volumes/zs01/chatgpt/OpenSMS/local-work/exports'),
           work/'opensms_artifacts/reports':Path('/Volumes/zs01/chatgpt/OpenSMS/local-work/reports')}
    missing=[]
    for link,target in links.items():
        if target.is_dir():
            link.parent.mkdir(parents=True,exist_ok=True)
            if not link.exists():link.symlink_to(target,target_is_directory=True)
        else:missing.append(str(target))
    # The original external UUID is intentionally preserved and must match.
    storage=json.loads((work/'scripts/opensms_storage_policy.json').read_text())
    drive_uuid=None
    if Path('/Volumes/zs01').is_mount():
        import plistlib
        result=subprocess.run(['/usr/sbin/diskutil','info','-plist','/Volumes/zs01'],capture_output=True)
        if result.returncode==0:drive_uuid=plistlib.loads(result.stdout).get('VolumeUUID')
    dependencies_ok=py.is_file() and node.is_file()
    imports_ok=False
    if py.is_file():
        result=subprocess.run([str(py),'-c','import openpyxl,pandas,cryptography,requests,httpx'],capture_output=True)
        imports_ok=result.returncode==0
    report={'restored':True,'verifiedFiles':count,'businessStarted':False,'automationsEnabled':False,
            'workspace':str(work),'sharedWorkspace':str(shared),'pathRewrites':rewrite,
            'restoredPersonalSkills':restored_skills,'runtimeFound':dependencies_ok,'pythonImportsPassed':imports_ok,
            'originalDriveUuidMatches':str(drive_uuid).upper()==storage['externalVolumeUuid'].upper(),
            'missingExternalDirectories':missing,
            'manualSteps':['Install/reinstall Codex workspace dependencies and listed plugins',
                           'Install BitBrowser and rebind the existing profile ID if it changed',
                           'Reinstall CloakBrowser dependencies in the shared workspace',
                           'Log in OpenSMS and Grab&Go manually; no credentials were exported',
                           'Review old submission intents; never replay them',
                           'Disable the old computer automation before enabling one on the new computer']}
    (work/'RESTORE_RESULT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print('文件恢复完成；尚未启动任何业务。请完成“开始恢复.md”中的环境与登录检查。')

if __name__=='__main__':main()
'''

RESTORE_COMMAND = '''#!/bin/bash
set -euo pipefail
MIGRATION_ROOT="$(cd "$(dirname "$0")" && pwd)"
MIGRATION_PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$MIGRATION_PYTHON" ]]; then
  MIGRATION_PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$MIGRATION_PYTHON" ]]; then
  printf '请先安装 Codex 工作区依赖或 Python 3，再双击恢复。\\n'
  read -r -p '按回车结束' _migration_answer
  exit 3
fi
"$MIGRATION_PYTHON" "$MIGRATION_ROOT/restore_mac.py" "$@"
read -r -p '按回车结束' _migration_answer
'''

def regular_files(root: Path):
    if not root.is_dir():return
    for base,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in SKIP_DIRS and d != 'locks' and not d.endswith('.lock') and not d.startswith('.optimization-'))
        for name in sorted(names):
            p=Path(base)/name
            if p.is_symlink() or name in SKIP_NAMES or name.startswith(('._','.env')):continue
            if p.suffix in {'.pyc','.lock','.sock'} or name.endswith(('-shm','-wal')):continue
            if p.is_file():yield p

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-root',type=Path,default=Path('/Volumes/zs01/chatgpt/OpenSMS-Migration'))
    parser.add_argument('--thread-id',default=THREAD)
    args=parser.parse_args()
    if not Path('/Volumes/zs01').is_mount():raise RuntimeError('zs01未挂载；不回退本地')
    if not args.output_root.resolve().is_relative_to(Path('/Volumes/zs01')):raise RuntimeError('输出必须在zs01')
    stamp=datetime.now(ZoneInfo('Asia/Hong_Kong')).strftime('%Y%m%dT%H%M%S%z')
    root=args.output_root/('OpenSMS_Mac迁移包_'+stamp);root.mkdir(parents=True,exist_ok=False)
    copied=[];redactions=[];links=[];skipped=[];errors=[]
    def copy(source: Path,target: Path,category: str,redact=False):
        before=source.stat();raw=source.read_bytes()
        if (source.stat().st_size,source.stat().st_mtime_ns)!=(before.st_size,before.st_mtime_ns):raise RuntimeError('备份期间源文件变化：'+str(source))
        data=raw;findings={}
        # Do not run free-text credential heuristics over executable source or
        # hashed receipts: JS header expressions and UUID session metadata are
        # not passwords. Sensitive stores are excluded by path; chat is redacted
        # separately by the visible-conversation exporter.
        if redact and source.suffix.lower() in TEXT_EXTS:
            try:
                clean,findings=redact_backup_text(raw.decode('utf-8'));data=clean.encode('utf-8')
            except UnicodeDecodeError:pass
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        target.chmod(stat.S_IMODE(before.st_mode))
        copied.append({'source':str(source),'path':target.relative_to(root).as_posix(),'category':category,
                       'sourceSha256':hashlib.sha256(raw).hexdigest(),'sha256':hashlib.sha256(data).hexdigest()})
        if findings:redactions.append({'path':target.relative_to(root).as_posix(),'patterns':findings})
    def tree(source: Path,destination: Path,category: str):
        for p in regular_files(source):copy(p,destination/p.relative_to(source),category)
    sessions=[]
    for folder in (OLD_HOME/'.codex/sessions',OLD_HOME/'.codex/archived_sessions'):
        if folder.exists():sessions.extend(folder.rglob('*'+args.thread_id+'*.jsonl'))
    chat=export_visible_segments(sorted(set(sessions)),root/'conversation',args.thread_id)
    if not chat['messageCount']:raise RuntimeError('没有可见聊天，停止')
    print(json.dumps({'event':'migration_conversation_complete','messages':chat['messageCount'],'segments':chat['segments']}),flush=True)
    for source,label in ((WORK,'current_430'),(OLD_HOME/'Documents/数据分析','shared_数据分析')):
        for child in source.iterdir():
            if child.name in {'opensms_artifacts','outputs','opensms_runs','node_modules','recovery-records','__pycache__'} or child.name.startswith('.'):continue
            if child.is_symlink():links.append({'source':str(child),'target':str(child.resolve())});continue
            target=root/'code'/label/child.name
            if child.is_dir():tree(child,target,'code')
            elif child.is_file():copy(child,target,'code')
    # Preserve idempotency and history evidence, but never stale sessions/locks.
    artifacts=WORK/'opensms_artifacts'
    for child in artifacts.iterdir():
        if child.name in {'logs','maintenance','manual_grab_login'} or child.is_symlink():continue
        if child.name=='state':
            for state in child.iterdir():
                if state.name in {'new_browser','grab-query-tool','request_pacing','weighted_balance_monitor'} or state.suffix in {'.sqlite3','.db'} or state.name.endswith(('-wal','-shm','.lock')) or state.name.startswith('task_f_session_probe'):continue
                target=root/'core_state/opensms_artifacts/state'/state.name
                if state.is_dir():tree(state,target,'business-state')
                elif state.is_file():copy(state,target,'business-state')
        elif child.is_dir():tree(child,root/'core_state/opensms_artifacts'/child.name,'business-evidence')
    db=artifacts/'state/task_f_kooda_activation.sqlite3'
    db_target=root/'core_state/opensms_artifacts/state'/db.name
    db_target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(db_target) as target:
        source.backup(target)
    with sqlite3.connect(db_target.as_uri()+'?mode=ro',uri=True) as verify:
        check=verify.execute('PRAGMA quick_check').fetchall()
        if check!=[('ok',)]:raise RuntimeError('F库快照完整性检查未通过')
        tables=[r[0] for r in verify.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts={t:verify.execute('SELECT COUNT(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables}
    db_result={'source':str(db),'snapshot':str(db_target.relative_to(root)),'sha256':sha256_file(db_target),'quickCheck':'ok','tables':counts,
               'method':'SQLite online backup from read-only source; no live DB writes'}
    print(json.dumps({'event':'migration_f_database_complete','bytes':db_target.stat().st_size,'quickCheck':'ok'}),flush=True)
    skill_roots=[(OLD_HOME/'.codex/skills','user'),(OLD_HOME/'.agents/skills','agents'),
                 (OLD_HOME/'.codex/memories/skills','memory'),(OLD_HOME/'.codex/plugins/cache','plugin_snapshots')]
    skills=[];seen=set()
    for source,label in skill_roots:
        if not source.is_dir():continue
        for manifest in sorted(source.rglob('SKILL.md')):
            if 'node_modules' in manifest.parts:continue
            if str(manifest.resolve()) in seen:continue
            seen.add(str(manifest.resolve()));directory=manifest.parent
            target=root/'skills'/label/directory.relative_to(source)
            tree(directory,target,'skill')
            skills.append({'name':directory.name,'source':str(manifest),'path':str((target/'SKILL.md').relative_to(root))})
    for manifest in list((OLD_HOME/'.codex/plugins/cache').rglob('SKILL.md')):
        for parent in list(manifest.parents)[:3]:
            for name in ('container_tools','scripts','assets'):
                support=parent/name
                if support.is_dir() and support != manifest.parent:
                    relative=support.relative_to(OLD_HOME/'.codex/plugins/cache')
                    tree(support,root/'skills/plugin_support'/relative,'skill-support')
    # Only this chat's automation, as an inert template. Never edit live schedules.
    automation=OLD_HOME/'.codex/automations/opensms-a/automation.toml'
    if automation.exists():
        text=automation.read_text();paused=re.sub(r'(?m)^status\s*=.*$','status = "PAUSED"',text)
        write_text(root/'automations/opensms-a.PAUSED.template.toml',paused)
    attachments=set()
    for line in (root/'conversation/CHAT_USER_ASSISTANT.jsonl').read_text().splitlines():
        message=json.loads(line)
        if message['role']!='user':continue
        for name in re.findall(r'/Users/zhangsan/(?:\.codex/attachments|Downloads)/[^\r\n<>\"|]+?\.(?:xlsx|xls|csv|tsv|txt|pdf|docx|png|jpg|jpeg|webp)(?=[\s`）)]|$)',message['text'],flags=re.I):attachments.add(name)
    for name in sorted(attachments):
        source=Path(name)
        if source.is_file():copy(source,root/'conversation/user_files'/source.relative_to(OLD_HOME),'user-attachment')
        else:skipped.append({'source':name,'reason':'referenced_user_attachment_no_longer_present'})
    # Include the last completed A and its separate F delta. Historical bulk files remain on zs01.
    delivery=Path('/Volumes/zs01/chatgpt/OpenSMS/20261016/20261016T080237+0800')
    if delivery.exists():tree(delivery,root/'evidence/latest_task_a','latest-delivery')
    f_delta=Path('/Volumes/zs01/chatgpt/OpenSMS/local-work/task_f_outputs/20261016T080237+0800/OpenSMS_任务F新增激活日期_20261016T080237+0800.xlsx')
    if f_delta.exists():copy(f_delta,root/'evidence/latest_task_f'/f_delta.name,'latest-f-delta',False)
    # Export versions, not virtualenvs, credentials or platform-dependent binaries.
    versions=sorted(({'name':d.metadata.get('Name'),'version':d.version} for d in importlib.metadata.distributions()),key=lambda d:d['name'] or '')
    write_text(root/'environment/python_packages.json',json.dumps(versions,ensure_ascii=False,indent=2))
    write_text(root/'environment/runtime.json',json.dumps({'python':sys.version,'executable':sys.executable,'macos':subprocess.check_output(['sw_vers','-productVersion'],text=True).strip(),
        'architecture':os.uname().machine,'externalVolume':'/Volumes/zs01','externalVolumeUuid':'013FA99B-C153-3D70-93A2-37D801ACFD6A'},indent=2))
    write_text(root/'skills/SKILLS_INVENTORY.json',json.dumps(skills,ensure_ascii=False,indent=2))
    limitations={'notIncluded':['internal reasoning/system or developer instructions/raw tool output','passwords/Cookie/tokens/browser profiles/OS keychain',
        'active locks and browser process state','other conversations and their automations','Python/Node binaries or node_modules',
        'historical bulk exports and Task I index (keep the original zs01 drive)'],
        'missingUserAttachments':skipped,'originalSymlinks':links,'credentialRedactions':redactions,
        'chatScope':'All locally present segments with exact thread ID; visible user/assistant content only; no promise for deleted messages',
        'coldStartRequired':'Codex runtimes/plugins, CloakBrowser, BitBrowser and manual authentication must be set up on the new Mac',
        'automaticTaskReplay':False,'automationsRestoredActive':False,'sourceReadErrors':errors}
    write_text(root/'EXCLUDED_AND_LIMITATIONS.json',json.dumps(limitations,ensure_ascii=False,indent=2))
    write_text(root/'SOURCE_MAP.json',json.dumps(copied,ensure_ascii=False,indent=2))
    write_text(root/'restore_mac.py',RESTORE_PY);(root/'restore_mac.py').chmod(0o755)
    write_text(root/'恢复到新Mac.command',RESTORE_COMMAND);(root/'恢复到新Mac.command').chmod(0o755)
    write_text(root/'CONTEXT_HANDOFF.md',f'''# 10日报表巡检：换机交接

原会话：{args.thread_id}。创建于北京时间{stamp}。

请先读 `code/current_430/scripts/OPENSMS_10DAY_PATROL_POLICY.md`，它是完整业务规则源；不要把历史聊天授权当作本轮授权。

工作目录：~/Downloads/430；共享入口：~/Documents/数据分析/opensms。
任务A只启动一次、精确绑定patrol_started；B/C/D/E默认关闭；后台F独立终态核验、只本地入库供下轮A使用。
保留F库、人工续费覆盖层、余额/套餐字段、E永久去重、G原运营商ID、F截止日提交意图与历史证据，不清空台账。
只共用一个OpenSMS CloakBrowser；F使用BitBrowser 430。新电脑登录态、钥匙串和浏览器进程必须重新核验，不能复用旧PID/端口。
机房配置以 opensms_room_settings.json 为准，2/3/5/6/7号共137台配置，150和193未配置。

最近主报表：20261016T080237+0800，成功。17条运营商写入及回读；8条F日期已提交后台并双日期回读。
该轮后台F另新增2条有效日期，已经本地入库，尚未由下一轮A上传。不得重复启动旧F或拿旧文档补交。
本包只恢复文件与可见聊天资料，不承诺恢复原Codex侧边栏同一聊天身份。不要写入Codex内部会话索引或state数据库。

在新会话粘贴：
“请读取 ~/Downloads/430/migration_bundle/CONTEXT_HANDOFF.md 及原规则文件，只做离线环境检查和依赖准备，核对zs01与数据库；暂不导出、不提交、不创建自动化。登录与迁移核验完成后由我另行授权业务。”
''')
    write_text(root/'开始恢复.md','''# Mac 换机恢复

## 包中有什么

- conversation：本机会话所有现存段的用户/助手可见消息（凭据脱敏）、图片及仍存在的用户附件。
- code：当前430和共享数据分析代码、测试、全部规则、JSON配置、真实截止日Excel模板。
- core_state：一致性备份的F SQLite库（含续费覆盖层/余额/套餐）、永久去重和运营商恢复台账、提交意图、快照及F作业证据。
- skills：个人业务技能及当前系统/插件技能的源码快照；插件授权不在包中。
- evidence：最近已完成主报表、回执和本次F新增日期文件。
- automations：本会话A定时器的暂停模板，不会自动启用。

## 最短恢复步骤

1. 把ZIP复制到新Mac并解压，同时连接原移动硬盘 **zs01**。推荐新Mac使用相同的短用户名 zhangsan；不同用户名由恢复器改代码/配置路径，历史只读证据中的旧绝对路径不改，仍须人工核对。
2. 安装并登录Codex；让新会话加载工作区依赖（Python/Node）和重新安装本清单所需的官方插件。包中有版本清单，但不复制旧机器的二进制/浏览器。
3. 在解压目录执行 `python3 restore_mac.py --verify-only`，再双击“恢复到新Mac.command”。目标Downloads/430及Documents/数据分析必须为空或不存在；已有目录会拒绝覆盖。
4. 打开 ~/Downloads/430/RESTORE_RESULT.json，确认依赖、原移动盘UUID和目录；历史37GB+本地工作区/全部报表、8GB任务I索引仍在原移动盘，没有再塞进ZIP。若更换移动盘，先完整迁移资料并另外核验配置UUID，不能放宽未挂载停止规则。
5. 共享浏览器目录 ~/Documents/数据分析/cloakbrowser_free_test 中使用原package.json准备CloakBrowser依赖；在新Mac安装BitBrowser并核对固定430 profile ID。重新人工登录OpenSMS和Grab&Go、重新设置不同步钥匙串凭据。绝不复制旧Cookie、PID/端口、登录profile或锁文件。
6. 按CONTEXT_HANDOFF.md在新聊天离线核查。技能快照不直接注入插件内部缓存；个人业务技能已按不覆盖方式恢复到 ~/.codex/skills，重启/重新加载后核对。官方插件应正常重新安装并连接。
7. 先停止旧电脑的同用途定时器，再由新电脑通过正式自动化管理工具重建“暂停模板”，确认路径和23:00—11:00时段后另行启用。**恢复器不会自动开定时器或执行任何业务**，防止双机重复写入。

## 必须知道

聊天资料可阅读和给新会话接续，但没有在本次工具/文档中验证安全的跨机“原侧边栏同一聊天导入”路径；本包不修改Codex内部注册数据库。官方CLI可在空闲时使用/export导出完整Markdown（https://learn.chatgpt.com/docs/developer-commands）。

已排除内部推理、系统/开发者指令和工具原始输出；这不是原始内部JSONL导出。可见业务消息保留；凭据文本有脱敏。已删除/未落盘消息、缺失附件无法凭空恢复，详情见EXCLUDED_AND_LIMITATIONS.json。
本包含手机号、ICCID、业务库和私人聊天，不加密；请只使用可信加密硬盘传输，不发公共网盘。账号密码、Cookie、密钥与macOS钥匙串不在包中。
恢复只做文件拷贝、校验、环境检查；上线前仍需登录、依赖和外接盘核验，因此不把“文件可恢复”冒称“无需设置立即执行业务”。
''')
    summary={'status':'success','threadId':args.thread_id,'title':'10日报表巡检','createdAt':stamp,
        'conversation':{k:v for k,v in chat.items() if k!='sourceSegments'},
        'codeFiles':sum(x['category']=='code' for x in copied),'skills':len(skills),
        'skillFiles':sum(x['category']=='skill' for x in copied),'copiedFiles':len(copied),
        'database':db_result,'missingAttachments':len(skipped),'redactedFiles':len(redactions),
        'businessWrites':0,'autoEnabled':False,'root':str(root),'historicalBulkDataRequiresOriginalZs01':True}
    write_text(root/'BACKUP_SUMMARY.json',json.dumps(summary,ensure_ascii=False,indent=2))
    # A JSON verifier avoids shell quoting/whitespace ambiguity in Chinese paths.
    records=file_manifest(root)
    write_text(root/'VERIFY_MANIFEST.json',json.dumps(records,ensure_ascii=False,indent=2))
    # VERIFY_MANIFEST itself is covered by the final SHA file and outer archive hash.
    records=file_manifest(root)
    print(json.dumps({'event':'migration_archiving','files':len(records)}),flush=True)
    archive=root.with_suffix('.zip');build_zip(root,archive)
    import zipfile
    with zipfile.ZipFile(archive) as z:
        for record in records:
            if hashlib.sha256(z.read(root.name+'/'+record['path'])).hexdigest()!=record['sha256']:raise RuntimeError('归档逐文件校验失败')
        if any('/RAW_CODEX_SESSION.jsonl' in name for name in z.namelist()):raise RuntimeError('禁止内部原始会话导出')
    archive_hash=sha256_file(archive)
    write_text(Path(str(archive)+'.sha256'),archive_hash+'  '+archive.name+'\n')
    result={**summary,'archive':str(archive),'archiveBytes':archive.stat().st_size,'sha256':archive_hash,'verifiedFiles':len(records)}
    write_text(root.parent/(root.name+'_交付回执.json'),json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
