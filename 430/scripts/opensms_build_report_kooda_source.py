#!/usr/bin/env python3
"""Build report-only Kooda evidence locally; no browser/API fallback exists."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'services/opensms_document_api/pipeline'))
from opensms_export_scan_cache import load_scan_cache, atomic_json
from report_kooda_source import projection_payload, validate_projection


def main():
    parser = argparse.ArgumentParser()
    for name in ('scan-cache', 'source', 'run-id', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    cache = load_scan_cache(args.scan_cache, expected_source=args.source)
    payload = projection_payload(cache['headers'], cache['rows'], args.run_id,
                                 args.source, cache['sourceSha256'])
    validate_projection(payload, args.run_id)
    atomic_json(Path(args.output), payload)
    print(json.dumps({'status': 'passed', 'runId': args.run_id, 'totalRows': payload['totalRows'],
        'sourceKind': payload['sourceKind'], 'sourceScope': payload['sourceScope'],
        'networkAccessed': False, 'output': args.output}, ensure_ascii=False))


if __name__ == '__main__':
    main()
