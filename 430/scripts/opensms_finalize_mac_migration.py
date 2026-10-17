#!/usr/bin/env python3
"""Finalize only a newly generated migration artifact, without business I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
from datetime import datetime
from zoneinfo import ZoneInfo
import zipfile

from opensms_export_session_migration import build_zip, file_manifest, sha256_file, write_text

def copy_portable(source, target):
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)
    target.chmod(stat.S_IMODE(source.stat().st_mode))

PASSWORD = re.compile(r'((?:密码|密碼)\s*[:：=]\s*)([^\s\n，,；;]{4,})')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--restore-result', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.name.startswith('OpenSMS_Mac迁移包_') or not root.is_relative_to(Path('/Volumes/zs01/chatgpt/OpenSMS-Migration')):
        raise RuntimeError('Refusing unrelated target')
    source_map = json.loads((root/'SOURCE_MAP.json').read_text())
    original_repaired=0
    for entry in source_map:
        if entry['sha256']==entry['sourceSha256']:continue
        source=Path(entry['source']);target=root/entry['path']
        # Preserve the original immutable audit bytes and valid code syntax.
        if sha256_file(source)!=entry['sourceSha256']:
            raise RuntimeError('Source changed since snapshot: '+str(source))
        copy_portable(source,target)
        entry['sha256']=entry['sourceSha256'];original_repaired+=1
    chat_path = root/'conversation/CHAT_USER_ASSISTANT.jsonl'
    messages = [json.loads(line) for line in chat_path.open()]
    redactions = 0
    for record in messages:
        def replace(match):
            nonlocal redactions
            if match[2].startswith('[REDACTED'):return match[0]
            redactions += 1
            return match[1]+'[REDACTED_CREDENTIAL]'
        record['text'] = PASSWORD.sub(replace, record['text'])
    write_text(chat_path, ''.join(json.dumps(m,ensure_ascii=False)+'\n' for m in messages))
    with (root/'conversation/聊天记录.txt').open('w') as stream:
        stream.write('10日报表巡检：用户可见聊天归档\n\n北京时间UTC+8；凭据脱敏。内部推理、系统/开发者指令及工具原始输出不在导出范围。\n\n')
        for m in messages:
            timestamp=datetime.fromisoformat(m['timestamp'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Hong_Kong')).isoformat() if m['timestamp'] else '未知'
            stream.write(f"[{m['sequence']}] {timestamp} {'用户' if m['role']=='user' else '助手'}\n{m['text']}\n\n")
    summary_path = root/'conversation/CONVERSATION_SUMMARY.json'
    conversation = json.loads(summary_path.read_text())
    if redactions:
        conversation['credentialRedactions']['chinese-password'] = conversation['credentialRedactions'].get('chinese-password',0)+redactions
    write_text(summary_path,json.dumps(conversation,ensure_ascii=False,indent=2))
    for name in ('opensms_build_mac_migration.py','opensms_finalize_mac_migration.py'):
        source = Path(__file__).parent/name
        target = root/'code/current_430/scripts'/name
        copy_portable(source,target)
        path = target.relative_to(root).as_posix()
        entry = {'source':str(source),'path':path,'category':'code','sourceSha256':sha256_file(source),'sha256':sha256_file(target)}
        source_map = [x for x in source_map if x['path']!=path]+[entry]
    write_text(root/'SOURCE_MAP.json',json.dumps(source_map,ensure_ascii=False,indent=2))
    summary=json.loads((root/'BACKUP_SUMMARY.json').read_text())
    summary['conversation']={k:v for k,v in conversation.items() if k!='sourceSegments'}
    summary['securityFinalized']=True
    summary['originalCodeAndEvidenceRestored']=summary.get('originalCodeAndEvidenceRestored',0)+original_repaired
    summary['redactedFiles']=0
    summary['codeFiles']=sum(x['category']=='code' for x in source_map)
    summary['copiedFiles']=len(source_map)
    write_text(root/'BACKUP_SUMMARY.json',json.dumps(summary,ensure_ascii=False,indent=2))
    limits=json.loads((root/'EXCLUDED_AND_LIMITATIONS.json').read_text())
    limits['credentialRedactions']=[]
    limits['sourceCodeAndBusinessEvidence']='byte-preserved; sensitive stores excluded by path, visible chat credentials redacted separately'
    limits['visibleChatPasswordRedactions']=conversation['credentialRedactions']
    write_text(root/'EXCLUDED_AND_LIMITATIONS.json',json.dumps(limits,ensure_ascii=False,indent=2))
    from opensms_build_mac_migration import RESTORE_PY
    write_text(root/'restore_mac.py',RESTORE_PY)
    if args.restore_result:
        test=json.loads(args.restore_result.read_text())
        if not test['restored'] or test['businessStarted'] or test['automationsEnabled']:
            raise RuntimeError('Invalid isolated restore result')
        db=Path(test['workspace'])/'opensms_artifacts/state/task_f_kooda_activation.sqlite3'
        if sha256_file(db)!=summary['database']['sha256']:raise RuntimeError('Restored F database hash mismatch')
        receipt={'passed':True,'mode':'offline isolated destination on the same Mac; not a live test on a second computer',
                 'filesVerified':test['verifiedFiles'],'databaseSha256':sha256_file(db),'businessStarted':False,
                 'automationsEnabled':False,'runtimeAndAuthentication':'not carried over; require installation and manual login on new Mac',
                 'sourceReceipt':str(args.restore_result)}
        write_text(root/'RESTORE_TEST_RECEIPT.json',json.dumps(receipt,ensure_ascii=False,indent=2))
    # No self-referential digest: verify data excludes its own manifest.
    records=[{'path':p.relative_to(root).as_posix(),'sha256':sha256_file(p),'size':p.stat().st_size}
             for p in sorted(root.rglob('*')) if p.is_file() and not p.name.startswith('._') and p.name not in {'.DS_Store','FILES.tsv','SHA256SUMS.txt','VERIFY_MANIFEST.json'}]
    write_text(root/'VERIFY_MANIFEST.json',json.dumps(records,ensure_ascii=False,indent=2))
    records=file_manifest(root)
    archive=root.with_suffix('.zip')
    temporary=root.parent/(root.name+'.verified.tmp.zip')
    print(json.dumps({'event':'security_finalized_archiving','files':len(records),'chatPasswordRedactions':conversation['credentialRedactions']}),flush=True)
    build_zip(root,temporary)
    with zipfile.ZipFile(temporary) as z:
        for record in records:
            if hashlib.sha256(z.read(root.name+'/'+record['path'])).hexdigest()!=record['sha256']:
                raise RuntimeError('Archive verification failed')
    temporary.replace(archive)
    digest=sha256_file(archive)
    write_text(Path(str(archive)+'.sha256'),digest+'  '+archive.name+'\n')
    result={**summary,'archive':str(archive),'archiveBytes':archive.stat().st_size,'sha256':digest,'verifiedFiles':len(records)}
    write_text(root.parent/(root.name+'_交付回执.json'),json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
