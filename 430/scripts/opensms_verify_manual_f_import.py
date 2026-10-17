"""Read-only reconstruction of an explicitly authorized F import input."""
import json
import sys
from pathlib import Path
from opensms_f_date_import import prepare_import


def main():
    if len(sys.argv) != 3:
        raise ValueError('F_RETRY_SOURCE_ARGUMENTS_INVALID')
    proof_path, input_path = map(Path, sys.argv[1:])
    if not proof_path.is_absolute() or not input_path.is_absolute():
        raise ValueError('F_RETRY_SOURCE_PATH_INVALID')
    proof = json.loads(proof_path.read_text())
    original = json.loads(input_path.read_text())
    rebuilt = prepare_import(proof, original['runId'])
    if rebuilt != original:
        raise ValueError('F_RETRY_SOURCE_RECONSTRUCTION_MISMATCH')
    print(json.dumps({'status': 'passed', 'runId': original['runId'],
                      'fileRows': original['fileRows'], 'readOnly': True}))


if __name__ == '__main__':
    main()
