#!/usr/bin/env python3
"""Read local D/E transaction JSON only; never connects to OpenSMS or a database.

Output is exclusive-create. Absence of evidence is not proof of never notified.
cardRetain is deliberately not used as an expiry-notification audit trail.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


ROOTS = [Path('/Volumes/zs01/chatgpt/OpenSMS'),
         Path('/Users/zhangsan/Downloads/430/opensms_artifacts')]
RECEIPT = re.compile(r'^opensms_task_(?:d_(?:swap_zero|retention_verified)|e)_\d.*_(?:receipt|progress)\.json$')
RUN = re.compile(r'(\d{8}T\d{6}\+\d{4})')


def phone(value):
    text = str(value).strip()
    if re.fullmatch(r'1\d{10}', text):
        text = text[1:]
    if not re.fullmatch(r'\d{10}', text):
        raise ValueError('invalid phone identifier')
    return text


def phone_list(values):
    if not isinstance(values, list):
        raise ValueError('missing explicit phone list')
    result = [phone(v) for v in values]
    if len(result) != len(set(result)):
        raise ValueError('duplicate phone identifier within batch')
    return result


def is_int(value):
    return type(value) is int


def valid_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%d') == value
    except (TypeError, ValueError):
        return False


class History:
    def __init__(self, roots):
        self.roots = roots
        self.cache = {}
        self.files = []
        self.errors = []
        self.events = {}
        self.missing_roots = []
        self.json_count = 0

    def read(self, path):
        path = Path(path).resolve()
        if path not in self.cache:
            raw = path.read_bytes()
            self.cache[path] = (json.loads(raw), hashlib.sha256(raw).hexdigest())
        return self.cache[path][0]

    def linked(self, value, source):
        if not value:
            raise ValueError('missing linked notification manifest')
        candidate = Path(value)
        if candidate.is_file():
            return candidate.resolve()
        candidate = source.parent / candidate.name
        if not candidate.is_file():
            raise ValueError('linked manifest not found: ' + str(value))
        return candidate.resolve()

    def manifest(self, doc, source):
        manifest_path = self.linked(doc.get('manifestPath'), source)
        manifest = self.read(manifest_path)
        if manifest.get('task') != doc.get('task'):
            raise ValueError('manifest task mismatch')
        run = RUN.search(source.name)
        if run and run.group(1) not in manifest_path.name:
            raise ValueError('manifest run mismatch')
        if doc.get('task') == 'D':
            if manifest.get('phase') != doc.get('phase'):
                raise ValueError('manifest phase mismatch')
            cards = phone_list(manifest.get('cardNos'))
            if manifest.get('expireDate') != doc.get('expireDate'):
                raise ValueError('manifest date mismatch')
            if len(cards) != doc.get('candidateCount'):
                raise ValueError('manifest candidate count mismatch')
            mapping = {p: manifest['expireDate'] for p in cards}
        else:
            cards, mapping = [], {}
            for group in manifest.get('groups', []):
                group_cards = phone_list(group.get('cardNos'))
                for p in group_cards:
                    if p in mapping:
                        raise ValueError('duplicate E manifest phone')
                    mapping[p] = group.get('expireDate')
                cards.extend(group_cards)
            if len(cards) != doc.get('candidateCount'):
                raise ValueError('E manifest candidate count mismatch')
        return manifest_path, cards, mapping

    def identify(self, doc, source, batch, number, manifest_cards, mapping):
        explicit = batch.get('cardNos')
        mode = 'explicit_batch_cardNos'
        if explicit is not None:
            cards = phone_list(explicit)
        elif doc.get('task') == 'D':
            size = doc.get('markerBatchSize')
            if not is_int(size) or size < 1 or not is_int(number) or number < 1:
                raise ValueError('legacy batch cannot be bound to manifest slice')
            cards = manifest_cards[(number - 1) * size:number * size]
            mode = 'same_run_manifest_slice_by_batchNumber_and_markerBatchSize'
        else:
            raise ValueError('E batch missing explicit cardNos')
        expiry = batch.get('expireDate', doc.get('expireDate'))
        if not cards or not valid_date(expiry):
            raise ValueError('empty batch or invalid expiry')
        if any(mapping.get(p) != expiry for p in cards):
            raise ValueError('batch identity/date differs from linked manifest')
        return cards, expiry, mode

    def add(self, doc, source, batch, cards, expiry, mode, kind, reason, manifest_path):
        run_match = RUN.search(source.name)
        run_id = run_match.group(1) if run_match else doc.get('runId', doc.get('label', ''))
        key = json.dumps([run_id, doc.get('task'), doc.get('phase'), batch.get('batchNumber'), expiry, cards], separators=(',', ':'))
        raw = batch.get('serverResponse', batch.get('response'))
        event = {
            'runId': run_id, 'task': doc.get('task'), 'phase': doc.get('phase'),
            'batchNumber': batch.get('batchNumber'), 'expireDate': expiry,
            'operationStartedAt': doc.get('createdAt'),
            'timeBasis': 'receipt_createdAt_operation_start_not_per_phone_completion',
            'evidencePath': str(source), 'evidenceSha256': self.cache[source][1],
            'manifestPath': str(manifest_path), 'manifestSha256': self.cache[manifest_path][1],
            'identityMode': mode, 'kind': kind, 'reason': reason, 'cardNos': cards,
            'inputCount': len(cards), 'parentStatus': doc.get('status'),
            'rawResponsePresent': isinstance(raw, dict),
        }
        if isinstance(raw, dict):
            data = raw.get('data') or {}
            event.update({k: data.get(k) for k in ('inputCount', 'matchedCardCount', 'updatedChatCount')})
            event['notFound'] = [phone(p) for p in data.get('notFound', [])]
            event['matchedNoChange'] = data.get('updatedChatCount') == 0
        if source.name.endswith('_receipt.json'):
            event['sourcePriority'] = 2
        else:
            event['sourcePriority'] = 1
        existing = self.events.get(key)
        # Same operation's final receipt and progress are duplicate evidence,
        # not additional submissions. A later valid receipt supersedes uncertainty.
        ranks = {'uncertain': 0, 'legacy_recovered': 1, 'confirmed': 2}
        if existing is None or (ranks[kind], event['sourcePriority']) > (ranks[existing['kind']], existing['sourcePriority']):
            self.events[key] = event

    def batch(self, doc, source, batch, number, manifest_path, manifest_cards, mapping, pending=False):
        cards, expiry, mode = self.identify(doc, source, batch, number, manifest_cards, mapping)
        if batch.get('batchNumber') != number:
            raise ValueError('batch sequence mismatch')
        raw = batch.get('serverResponse', batch.get('response'))
        if not isinstance(raw, dict):
            legacy = doc.get('status') == 'recovered_transactional_receipt' and batch.get('recoveredFromLegacyControlFlow') is True
            if legacy:
                evidence = doc.get('recoveryEvidence') or {}
                match = re.search(r'missingCards=(\d+), newLogs=(\d+), updatedChats=(\d+)', evidence.get('legacyError', ''))
                proof_path = self.linked(doc.get('legacyProgressPath'), source)
                proof = self.read(proof_path)
                legacy = bool(match and proof.get('error') == evidence.get('legacyError')
                              and int(match[1]) == len(cards) and int(match[2]) == 0 and int(match[3]) >= len(cards)
                              and batch.get('inputCount') == batch.get('matchedCardCount') == len(cards)
                              and batch.get('updatedChatCount') == int(match[3]) and batch.get('notFound') == []
                              and batch.get('businessReceiptPassed') is True and batch.get('transactionalVerificationPassed') is True)
            self.add(doc, source, batch, cards, expiry, mode, 'legacy_recovered' if legacy else 'uncertain',
                     'legacy_control_flow_reconstruction_without_raw_response' if legacy else 'missing_raw_business_response', manifest_path)
            return
        data = raw.get('data') or {}
        try:
            absent = phone_list(data.get('notFound'))
        except ValueError:
            absent = None
        exact = (raw.get('code') == 200 and absent is not None and set(absent) <= set(cards)
                 and is_int(data.get('inputCount')) and data['inputCount'] == len(cards)
                 and is_int(data.get('matchedCardCount')) and data['matchedCardCount'] == len(cards) - len(absent)
                 and is_int(data.get('updatedChatCount')) and data['updatedChatCount'] >= 0)
        if not pending:
            exact = exact and batch.get('businessReceiptPassed') is True and batch.get('transactionalVerificationPassed') is True
            exact = exact and all(batch.get(k) == data.get(k) for k in ('inputCount', 'matchedCardCount', 'updatedChatCount', 'notFound'))
        # D's original success criterion requires at least one updated session per
        # matched card. E accepts idempotent no-change as documented by its executor.
        if doc.get('task') == 'D' and exact:
            exact = data['updatedChatCount'] >= data['matchedCardCount']
        self.add(doc, source, batch, cards, expiry, mode, 'confirmed' if exact else 'uncertain',
                 ('raw_exact_input_equals_matched_plus_notFound' if exact else 'raw_business_response_not_exact_or_rejected'), manifest_path)

    def scan_file(self, source):
        doc = self.read(source)
        self.files.append({'path': str(source), 'sha256': self.cache[source][1], 'task': doc.get('task'),
                           'phase': doc.get('phase'), 'status': doc.get('status'), 'createdAt': doc.get('createdAt'),
                           'batchCount': len(doc.get('batches') or []), 'pendingBatch': bool(doc.get('pendingBatch'))})
        if doc.get('validateOnly') is True:
            return
        manifest_path, cards, mapping = self.manifest(doc, source)
        for number, batch in enumerate(doc.get('batches') or [], 1):
            try:
                self.batch(doc, source, batch, number, manifest_path, cards, mapping)
            except (ValueError, KeyError, TypeError) as exc:
                self.errors.append({'file': str(source), 'batchNumber': number, 'error': str(exc)})
        pending = doc.get('pendingBatch')
        if pending:
            self.batch(doc, source, pending, len(doc.get('batches') or []) + 1,
                       manifest_path, cards, mapping, pending=True)
        elif doc.get('status') == 'failed' and not doc.get('batches') and cards:
            # Older executors erased pending data. Count-only errors cannot identify
            # which particular card failed, so every possibly submitted card is uncertain.
            error = str(doc.get('error', ''))
            if re.search(r'业务回执不完整|独立回读未通过|HTTP\s+5\d\d|timeout|timed out|gateway', error, re.I):
                size = doc.get('markerBatchSize', len(cards))
                possible = cards[:size] if is_int(size) else cards
                stub = {'batchNumber': 1}
                self.add(doc, source, stub, possible, doc.get('expireDate'),
                         'legacy_first_batch_manifest_possible_submission', 'uncertain',
                         'legacy_error_has_no_per_phone_response: ' + error, manifest_path)

    def scan(self):
        paths = set()
        for root in self.roots:
            if not root.is_dir():
                self.missing_roots.append(str(root))
                continue
            for path in root.rglob('*.json'):
                if path.name.startswith('._'):
                    continue
                self.json_count += 1
                if RECEIPT.fullmatch(path.name):
                    paths.add(path.resolve())
        for source in sorted(paths):
            try:
                self.scan_file(source)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.errors.append({'file': str(source), 'error': str(exc)})
        phones = {}
        events = sorted(self.events.values(), key=lambda e: (e.get('operationStartedAt') or '', e['runId'], e.get('batchNumber') or 0))
        counts = Counter()
        uncertain, legacy, partial = [], [], []
        for event in events:
            event.pop('sourcePriority', None)
            kind = event['kind']
            counts[kind + 'BatchCount'] += 1
            if kind == 'uncertain':
                uncertain.append(event)
            elif kind == 'legacy_recovered':
                legacy.append(event)
            if event.get('notFound'):
                partial.append(event)
            for p in event['cardNos']:
                state = phones.setdefault(p, {'confirmedCount': 0, 'notFoundCount': 0, 'uncertainCount': 0, 'legacyRecoveredCount': 0})
                status = 'notFound' if p in event.get('notFound', []) else {'confirmed': 'confirmed', 'uncertain': 'uncertain', 'legacy_recovered': 'legacyRecovered'}[kind]
                # A code=200 exact response is required before treating notFound as
                # an explicit identity result; malformed responses stay uncertain.
                if kind == 'uncertain':
                    status = 'uncertain'
                state[status + 'Count'] += 1
                counts[status + 'PhoneOccurrences'] += 1
                detail = {k: v for k, v in event.items() if k not in ('cardNos', 'notFound')}
                state['last' + status[0].upper() + status[1:]] = detail
        for name in ('confirmed', 'notFound', 'uncertain', 'legacyRecovered'):
            counts[name + 'UniquePhoneCount'] = sum(s[name + 'Count'] > 0 for s in phones.values())
        counts['noConfirmedEvidenceButUncertainPhoneCount'] = sum(s['confirmedCount'] == 0 and s['uncertainCount'] > 0 for s in phones.values())
        counts['noConfirmedEvidenceButLegacyRecoveredPhoneCount'] = sum(s['confirmedCount'] == 0 and s['legacyRecoveredCount'] > 0 for s in phones.values())
        return {
            'schemaVersion': 1, 'createdAt': datetime.now(timezone.utc).isoformat(), 'readOnlyBusinessState': True,
            'coverage': {'roots': [str(p) for p in self.roots], 'missingRoots': self.missing_roots,
                         'jsonFilenamesScanned': self.json_count, 'matchedReceiptProgressFiles': len(paths),
                         'earliestOperationStartedAt': min((e.get('operationStartedAt') for e in events if e.get('operationStartedAt')), default=None),
                         'latestOperationStartedAt': max((e.get('operationStartedAt') for e in events if e.get('operationStartedAt')), default=None),
                         'limitations': [
                             'Only locally retained task D/E receipts and progress are audited; manual or missing history is not proved absent.',
                             'cardRetain/list is not an expiry-marker audit log and is never used to infer notification absence.',
                             'Transaction receipt verifies the historical marker operation, not recipient delivery or current marker state.',
                             'createdAt is an operation-start timestamp, not an exact per-card completion timestamp.',
                             'Legacy recovery without original serverResponse is separate from raw-response confirmation.',
                             'notFound proves only that specific batch did not find the phone; it does not cancel earlier confirmation.',
                             'uncertain history can be superseded for current classification by a later confirmed marker, but remains recorded.',
                         ]},
            'summary': dict(counts), 'parseErrors': self.errors, 'files': self.files,
            'phones': phones, 'uncertainBatches': uncertain, 'legacyRecoveredBatches': legacy,
            'explicitNotFoundBatches': partial,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Refusing to overwrite existing audit output: ' + str(args.output))
    result = History(args.root or ROOTS).scan()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, separators=(',', ':'))
        handle.write('\n')
    print(json.dumps({'output': str(args.output.resolve()), 'summary': result['summary'],
                      'coverage': {k: v for k, v in result['coverage'].items() if k != 'limitations'},
                      'parseErrors': result['parseErrors']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
