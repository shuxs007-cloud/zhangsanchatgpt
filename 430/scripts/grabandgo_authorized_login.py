#!/usr/bin/env python3
"""No-echo credential intake; password is only sent over a private child pipe."""
import getpass
import json
import os
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parent.parent
node='/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
if __name__=='__main__':
    username=input('Grab&Go account: ').strip()
    password=getpass.getpass('Grab&Go password (hidden): ')
    result=subprocess.run([node,str(root/'scripts/grabandgo_authorized_login.mjs')],
        input=json.dumps({'username':username,'password':password}),text=True,
        cwd=root,env={**os.environ,'GRAB_CURRENT_USER_LOGIN_AUTHORIZED':'1'},
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    password=''
    print(result.stdout.strip())
    raise SystemExit(result.returncode)
