"""Offline Task F deadline export and shell-dispatch safety regression tests."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'task_f_deadline_export_under_test', ROOT / 'scripts/opensms_export_task_f_deadlines.py')
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)
ICCID = '891223020025159114'


class TemporaryDatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task_f_deadline_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'source.sqlite3'

    def create_database(self, rows=()):
        with sqlite3.connect(self.database) as conn:
            # No SQLite affinity coercion: preserve invalid numeric inputs for tests.
            conn.execute('CREATE TABLE kooda_activation '
                         '(query_iccid, cutoff_date, activation_date, lookup_status)')
            conn.executemany('INSERT INTO kooda_activation VALUES (?, ?, ?, ?)', rows)
        return self.database


class ReadRowsTests(TemporaryDatabaseCase):
    def test_normalize_strips_exactly_one_terminal_f(self):
        for suffix in ('', 'F', 'f'):
            with self.subTest(suffix=suffix):
                self.assertEqual(EXPORTER.normalize_iccid(ICCID + suffix), ICCID)

    def test_normalize_rejects_invalid_values_and_precision_loss(self):
        invalid = [int(ICCID), float(ICCID), None, '', '1' * 17, '1' * 23,
                   ICCID + 'FF', ICCID + 'fF', 'F' + ICCID, ICCID + ' ',
                   ' ' + ICCID, '８' * 18, '8.91223020025159114e17', '8' * 17 + '-']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                EXPORTER.normalize_iccid(value)

    def test_normalize_accepts_18_through_22_digits_as_text(self):
        for length in range(18, 23):
            with self.subTest(length=length):
                value = '0' + '9' * (length - 1)
                self.assertEqual(EXPORTER.normalize_iccid(value + 'F'), value)

    def test_empty_database_returns_empty_rows_without_mutation(self):
        self.create_database()
        before = self.database.read_bytes()
        self.assertEqual(EXPORTER.read_rows(self.database), ([], {}))
        self.assertEqual(self.database.read_bytes(), before)

    def test_absent_database_is_not_created(self):
        with self.assertRaises(sqlite3.OperationalError):
            EXPORTER.read_rows(self.database)
        self.assertFalse(self.database.exists())

    def test_invalid_database_is_rejected_without_replacement(self):
        self.database.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            EXPORTER.read_rows(self.database)
        self.assertEqual(self.database.read_bytes(), b'not a sqlite database')

    def test_sort_and_normalize_iccid_preserving_text(self):
        rows = [('9' * 22 + 'F', '2026-09-01', '2026-08-03', 'found'),
                ('0' * 18 + 'f', None, None, 'activation_missing'),
                (ICCID, '', '', 'not_found')]
        self.create_database(rows)
        actual, statuses = EXPORTER.read_rows(self.database)
        self.assertEqual(actual, [['0' * 18, None, None], [ICCID, None, None],
                                  ['9' * 22, '2026-09-01', '2026-08-03']])
        self.assertEqual(statuses, {'activation_missing': 1, 'not_found': 1, 'found': 1})
        self.assertTrue(all(isinstance(row[0], str) for row in actual))

    def test_normalized_duplicates_stop_export(self):
        self.create_database([(ICCID, None, None, 'not_found'),
                              (ICCID + 'F', None, None, 'activation_missing')])
        with self.assertRaisesRegex(ValueError, '重复'):
            EXPORTER.read_rows(self.database)

    def test_integer_iccid_in_database_is_rejected(self):
        self.create_database([(int(ICCID), None, None, 'not_found')])
        with self.assertRaisesRegex(ValueError, '文本'):
            EXPORTER.read_rows(self.database)

    def test_cutoff_is_activation_plus_29_calendar_days(self):
        cases = [('2026-08-03', '2026-09-01'), ('2024-02-01', '2024-03-01'),
                 ('2023-02-01', '2023-03-02'), ('2024-02-29', '2024-03-29'),
                 ('2026-12-15', '2027-01-13')]
        self.create_database([(str(880000000000000000 + i), cutoff, activation, 'found')
                              for i, (activation, cutoff) in enumerate(cases)])
        actual, statuses = EXPORTER.read_rows(self.database)
        self.assertEqual([row[1:] for row in actual], [[c, a] for a, c in cases])
        self.assertEqual(statuses, {'found': len(cases)})

    def test_invalid_or_inconsistent_dates_and_statuses_rejected(self):
        cases = [
            ('2026-09-02', '2026-08-03', 'found'),  # +30 is not inclusive 30 days.
            ('2026-09-01', None, 'found'),
            (None, '2026-08-03', 'found'),
            ('2026-02-30', '2026-02-01', 'found'),
            ('2026-9-01', '2026-08-03', 'found'),
            ('2026-09-01', '2026-08-03 00:00:00', 'found'),
            (20260901, '2026-08-03', 'found'),
            (None, None, 'found'),
            ('2026-09-01', '2026-08-03', 'activation_missing'),
            ('2026-09-01', '2026-08-03', 'not_found'),
            (None, None, 'pending'),
            (None, None, None),
        ]
        self.create_database()
        for cutoff, activation, status in cases:
            with self.subTest(cutoff=cutoff, activation=activation, status=status):
                with sqlite3.connect(self.database) as conn:
                    conn.execute('DELETE FROM kooda_activation')
                    conn.execute('INSERT INTO kooda_activation VALUES (?, ?, ?, ?)',
                                 (ICCID, cutoff, activation, status))
                with self.assertRaises(ValueError):
                    EXPORTER.read_rows(self.database)


class PublicationTests(TemporaryDatabaseCase):
    def setUp(self):
        super().setUp()
        self.template = self.root / 'template.xlsx'
        self.template.write_bytes(b'immutable template fixture')
        self.output = self.root / 'result.xlsx'
        self.receipt = self.root / 'result.receipt.json'
        self.create_database([(ICCID + 'F', '2026-09-01', '2026-08-03', 'found')])

    def call_export(self):
        return EXPORTER.export_deadlines(self.database, self.template, self.output, self.receipt)

    def fake_writer(self, command, **kwargs):
        # Only the existing local Node writer is allowed: no browser/runner/HTTP process.
        self.assertEqual(Path(command[0]), EXPORTER.NODE)
        self.assertEqual(Path(command[1]).name, 'writer.mjs')
        self.assertEqual(Path(command[2]).resolve(), self.template.resolve())
        rows = json.loads(Path(command[3]).read_text())
        Path(command[4]).write_bytes(b'offline staged workbook')
        return subprocess.CompletedProcess(command, 0,
            json.dumps({'status': 'success', 'recordCount': len(rows)}), '')

    def test_export_uses_readonly_database_and_preserves_template(self):
        before = self.database.read_bytes()
        with mock.patch.object(EXPORTER, 'check_template'), \
             mock.patch.object(EXPORTER.subprocess, 'run', side_effect=self.fake_writer) as writer, \
             mock.patch.object(EXPORTER, 'verify_output') as verify:
            result = self.call_export()
        writer.assert_called_once()
        self.assertEqual(verify.call_args.args[1], [[ICCID, '2026-09-01', '2026-08-03']])
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.template.read_bytes(), b'immutable template fixture')
        self.assertEqual(result['headers'], ['ICCID', '截止日', '激活日期'])
        self.assertFalse(result['networkAccess'])
        self.assertTrue(result['databaseReadOnly'])
        self.assertEqual(result['recordCount'], 1)
        self.assertEqual(json.loads(self.receipt.read_text()), result)

    def test_existing_output_or_receipt_refused_before_any_work(self):
        for occupied in (self.output, self.receipt):
            with self.subTest(occupied=occupied):
                occupied.write_bytes(b'keep original')
                with mock.patch.object(EXPORTER, 'check_template') as check, \
                     mock.patch.object(EXPORTER.subprocess, 'run') as writer, \
                     self.assertRaises(FileExistsError):
                    self.call_export()
                check.assert_not_called()
                writer.assert_not_called()
                self.assertEqual(occupied.read_bytes(), b'keep original')
                occupied.unlink()

    def test_input_output_overlap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '重叠'):
            EXPORTER.export_deadlines(self.database, self.template, self.template, self.receipt)

    def test_invalid_suffixes_rejected(self):
        for output, receipt in [(self.root / 'result.csv', self.receipt),
                                (self.output, self.root / 'result.txt')]:
            with self.subTest(output=output, receipt=receipt), self.assertRaises(ValueError):
                EXPORTER.export_deadlines(self.database, self.template, output, receipt)

    def test_failed_independent_verification_does_not_publish(self):
        with mock.patch.object(EXPORTER, 'check_template'), \
             mock.patch.object(EXPORTER.subprocess, 'run', side_effect=self.fake_writer), \
             mock.patch.object(EXPORTER, 'verify_output', side_effect=ValueError('bad readback')), \
             self.assertRaisesRegex(ValueError, 'bad readback'):
            self.call_export()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.receipt.exists())

    def test_writer_receipt_count_mismatch_does_not_publish(self):
        def wrong_count(command, **kwargs):
            result = self.fake_writer(command, **kwargs)
            result.stdout = json.dumps({'status': 'success', 'recordCount': 999})
            return result
        with mock.patch.object(EXPORTER, 'check_template'), \
             mock.patch.object(EXPORTER.subprocess, 'run', side_effect=wrong_count), \
             self.assertRaisesRegex(ValueError, '数量不符'):
            self.call_export()
        self.assertFalse(self.output.exists())

    def test_racing_output_is_not_overwritten(self):
        def verify_racing_output(path, rows):
            self.output.write_bytes(b'created by another process')
        with mock.patch.object(EXPORTER, 'check_template'), \
             mock.patch.object(EXPORTER.subprocess, 'run', side_effect=self.fake_writer), \
             mock.patch.object(EXPORTER, 'verify_output', side_effect=verify_racing_output), \
             self.assertRaises(FileExistsError):
            self.call_export()
        self.assertEqual(self.output.read_bytes(), b'created by another process')
        self.assertFalse(self.receipt.exists())

    def test_pinned_template_is_valid_without_modification(self):
        template = EXPORTER.DEFAULT_TEMPLATE
        before = template.read_bytes()
        EXPORTER.check_template(template)
        self.assertEqual(EXPORTER.sha256(template), EXPORTER.DEFAULT_TEMPLATE_SHA256)
        self.assertEqual(template.read_bytes(), before)


class ShellDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task_f_dispatch_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / 'scripts'
        scripts.mkdir()
        self.wrapper = scripts / 'run_opensms_task_f.sh'
        source = (ROOT / 'scripts/run_opensms_task_f.sh').read_text()
        # Copy into a fully isolated fake checkout: no test can reach the live runner.
        source = source.replace(
            'TASK_PYTHON="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"',
            'TASK_PYTHON=' + json.dumps(sys.executable))
        self.wrapper.write_text(source)
        (scripts / 'opensms_export_task_f_deadlines.py').write_text(
            'import json, sys\nprint(json.dumps({"entry": "local-export", "args": sys.argv[1:]}))\n')
        (scripts / 'opensms_task_f_reconcile_document.py').write_text(
            'import argparse,json,sys\np=argparse.ArgumentParser();p.add_argument("--state",required=True);'
            'p.add_argument("--source-kind");p.add_argument("--json",action="store_true");p.parse_args();'
            'print(json.dumps({"entry":"document-missing-export","args":sys.argv[1:]}))\n')
        patrol = scripts / 'run_opensms_patrol.sh'
        patrol.write_text('#!/usr/bin/env bash\nexec ' + json.dumps(sys.executable) +
                          ' "$(dirname "$0")/stub_patrol.py" "$@"\n')
        patrol.chmod(0o700)
        (scripts / 'stub_patrol.py').write_text(
            'import json, os, sys\nprint(json.dumps({"entry": "F-only", "args": sys.argv[1:], '
            '"gates": {k:v for k,v in os.environ.items() if k.startswith("OPENSMS_")}}))\n')

    def run_wrapper(self, *args, env=None):
        return subprocess.run(['bash', str(self.wrapper), *args], text=True, capture_output=True,
                              timeout=10, env={**os.environ, **(env or {})})

    def test_export_dispatch_is_local_only(self):
        for args in [('export-all', '--json'), ('export-all', '--db', '/offline/db', '--json')]:
            with self.subTest(args=args):
                result = self.run_wrapper(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['entry'], 'local-export')
                self.assertNotIn('--task-f-only', output['args'])

    def test_default_export_requires_document_and_never_falls_back_to_full_library(self):
        for args in [(), ('export', '--json')]:
            result = self.run_wrapper(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('local-export', result.stdout)
        result = self.run_wrapper('export', '--state', '/offline/run.json', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['entry'], 'document-missing-export')
        self.assertIn('system-export', output['args'])

    def test_sync_export_requires_current_user_authorization(self):
        for args in [('sync-export',), ('sync-export', '--authorization-source', 'heartbeat'),
                     ('sync-export', '--authorization-source', 'history'),
                     ('sync-export', '--force-task-f')]:
            with self.subTest(args=args):
                result = self.run_wrapper(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')

    def test_sync_export_rejects_bcde_and_other_extra_arguments(self):
        for flag in ('--force-task-b', '--force-task-c', '--force-task-d', '--force-task-e',
                     '--task-b-only', '--json', '--task-f-workers'):
            with self.subTest(flag=flag):
                result = self.run_wrapper('sync-export', '--authorization-source', 'current-user', flag)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')

    def test_authorized_sync_dispatches_f_only_with_eight_workers_and_cleared_bcde(self):
        env = {f'OPENSMS_FORCE_TASK_{task}': '1' for task in 'BCDE'}
        env.update({f'OPENSMS_{verb}_TASK_{task}': '1'
                    for task in 'DE' for verb in ('ENABLE', 'EXECUTE')})
        result = self.run_wrapper('sync-export', '--authorization-source', 'current-user', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['entry'], 'F-only')
        self.assertEqual(output['args'], ['run', '--json', '--task-f-only', '--force-task-f',
                                         '--authorization-source', 'current-user', '--task-f-workers', '8'])
        for key in env:
            self.assertNotIn(key, output['gates'])
        for key in ('OPENSMS_FORCE_TASK_F', 'OPENSMS_ENABLE_TASK_F', 'OPENSMS_EXECUTE_TASK_F'):
            self.assertEqual(output['gates'][key], '1')


if __name__ == '__main__':
    unittest.main()
