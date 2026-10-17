#!/usr/bin/env python3
"""Private pipe adapter for the explicitly authorized local Grab&Go login."""
import json
import sys
from opensms_auth_keychain import main

if __name__ == '__main__':
    try:
        main('com.openai.opensms.taskf.grabandgoplus', 'task-f')
    except Exception:
        print(json.dumps({'status': 'failed', 'error': 'GRAB_KEYCHAIN_UNAVAILABLE'}))
        sys.exit(2)
