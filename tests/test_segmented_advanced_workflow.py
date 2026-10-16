"""Real run gates preserve state serialization, cancellation and honest receipts."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

from football_model.state_storage import StorageError


ROOT = Path(__file__).parents[1]
WORKFLOW = (ROOT / ".github/workflows/advanced-models.yml").read_text()


def job(name):
    return re.split(r"(?m)^  [a-z_]+:\n", WORKFLOW.split("  " + name + ":\n", 1)[1], 1)[0]


def python_step(body, name):
    block = body.split("      - name: " + name + "\n", 1)[1].split("      - ", 1)[0]
    return textwrap.dedent(block.split("          python - <<'PY'\n", 1)[1].split("          PY", 1)[0])


class SegmentedWorkflowTests(unittest.TestCase):
    def test_every_state_writer_serializes_while_verification_does_not_lock(self):
        self.assertNotRegex(WORKFLOW.split("jobs:", 1)[0], r"(?m)^concurrency:")
        self.assertNotIn("concurrency:", job("verify"))
        for name in ("pre_study_capture", "advanced", "study_leagues", "study_finalize"):
            body = job(name)
            self.assertIn("    concurrency:", body)
            self.assertIn("football-model-state", body)
            self.assertIn("cancel-in-progress: false", body)
            self.assertIn("queue: max", body)

    def test_matrix_keeps_all_leagues_and_separate_data_failures(self):
        body = job("study_leagues")
        leagues = re.search(r"        league: \[([^]]+)\]", body).group(1).split(", ")
        configured = [c["key"] for c in json.loads((ROOT / "config.json").read_text())["competitions"]
                      if c.get("mode") != "audit_only"]
        self.assertEqual(leagues, configured)
        self.assertIn("max-parallel: 1", body)
        self.assertIn("fail-fast: false", body)
        self.assertIn('--run-goalmodel --checkpoint-state --repo .', body)
        self.assertIn("if: steps.preflight.outcome == 'success'", body)
        self.assertIn('actions/runs/$GITHUB_RUN_ID/artifacts?per_page=100', body)
        self.assertIn('advanced-study-storage-stop-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-', body)
        self.assertIn('actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs?', body)
        self.assertIn('name: advanced-study-storage-stop-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.league }}', body)
        self.assertIn('--resume-only', job("study_finalize"))
        self.assertEqual(WORKFLOW.count('python -m unittest discover -s tests -v'), 1)

    def test_long_cleanup_never_survives_explicit_cancellation(self):
        for name in ("advanced-models.yml", "football.yml", "feature-models.yml",
                     "football-total-shrink.yml", "fundamentals.yml", "fixture-recovery.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            for block in re.split(r"(?m)^      - ", text):
                if any(command in block for command in ('cloud_state.py save', 'cloud_state.py check',
                                                        'export_advanced_evidence.py', 'uses: actions/upload-artifact')):
                    self.assertNotIn("always()", block, name)
        self.assertEqual(WORKFLOW.count("always()"), 1)  # the small factual receipt only
        self.assertIn("continue-on-error: true", job("advanced").split("id: durable", 1)[1].split("      - ", 1)[0])

    def test_save_reuses_its_own_preflight_without_removing_actual_main_check(self):
        for name in ('pre_study_capture', 'study_leagues', 'study_finalize'):
            body = job(name)
            persist = body.split('        id: persist\n', 1)[1].split('      - ', 1)[0]
            self.assertEqual(persist.count('scripts/cloud_state_save_once.py'), 1)
            self.assertNotIn('scripts/cloud_state.py check', persist)
            self.assertNotIn('scripts/cloud_state.py preflight', persist)
        body = job('advanced')
        self.assertIn('id: check', body)
        self.assertEqual(body.count('python scripts/cloud_state.py check --runtime runtime'), 1)
        self.assertIn("steps.check.outcome == 'success'", body.split('        id: persist\n', 1)[1])
        self.assertIn('python scripts/cloud_state_save_once.py --runtime runtime', body)
        before_fit = job('study_leagues').split('        id: preflight\n', 1)[1].split('      - ', 1)[0]
        self.assertIn('scripts/cloud_state.py prepare', before_fit)
        self.assertIn('scripts/cloud_state.py preflight', before_fit)

    def test_embedded_python_compiles(self):
        scripts = re.findall(r"          python - <<'PY'\n(.*?)          PY", WORKFLOW, re.S)
        self.assertGreater(len(scripts), 5)
        for script in scripts:
            ast.parse(textwrap.dedent(script))

    def test_storage_gate_and_receipt_stop_only_new_expensive_fits(self):
        gate_script = python_step(job("study_leagues"), "检查本轮持久化停止标记")
        receipt_script = python_step(job("study_leagues"), "记录实际计算、已存状态及后续停止条件")
        for stop_count, attempted, restore, preflight, persist, study, expected in (
            (0, 0, 'success', 'success', 'success', {'results': [{'status': 'failed'}]}, False),
            (0, 0, 'success', 'success', 'success', {'checkpoint_failed_leagues': 1}, True),
            (0, 0, 'success', 'success', 'success', {'capacity_deferred_leagues': 1}, True),
            (0, 0, 'success', 'failure', 'success', {}, True),
            (0, 0, 'success', 'success', 'failure', {}, True),
            (0, 0, 'failure', 'skipped', 'skipped', {}, True),
            (1, 0, 'skipped', 'skipped', 'skipped', {}, False),
            (0, 1, 'skipped', 'skipped', 'skipped', {}, False),
        ):
            with self.subTest(stop_count=stop_count, preflight=preflight, persist=persist, study=study):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    subprocess.run(['git', 'init', '-q', str(root)], check=True)
                    (root / 'advanced-storage-stop-count.txt').write_text(str(stop_count))
                    (root / 'advanced-storage-stop-step-count.txt').write_text(str(attempted))
                    env = {**os.environ, 'GITHUB_OUTPUT': str(root / 'output'), 'GITHUB_STEP_SUMMARY': str(root / 'summary'),
                           'STORAGE_GATE': 'success', 'ALLOW_FIT': str(stop_count == 0 and attempted == 0).lower(),
                           'RESTORE': restore, 'PREFLIGHT': preflight,
                           'PERSIST': persist, 'RESEARCH': 'success', 'STUDY_LEAGUE': 'epl'}
                    env.update(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
                               PYTHONPATH=str(ROOT) + os.pathsep + env.get('PYTHONPATH', ''))
                    subprocess.run([sys.executable, '-c', gate_script], cwd=root, env=env, check=True)
                    self.assertIn('allow_fit=' + str(stop_count == 0 and attempted == 0).lower(), (root / 'output').read_text())
                    path = root / 'runtime/advanced_models/study_runs/latest.json'
                    study.update(source_commit='a' * 40, configured_leagues=['epl'], generated_at='2026-10-16T01:00:01Z')
                    path.parent.mkdir(parents=True); path.write_text(json.dumps(study))
                    (root / 'advanced-study-run-window.json').write_text(json.dumps({
                        'schema_version': 'advanced-study-command-window-v1', 'github_run_id': '123',
                        'github_run_attempt': '1', 'source_commit': 'a' * 40, 'configured_leagues': ['epl'],
                        'started_at': '2026-10-16T01:00:00Z', 'ended_at': '2026-10-16T01:00:02Z',
                        'report_changed': True, 'previous_report_sha256': None,
                        'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
                    subprocess.run([sys.executable, '-c', receipt_script], cwd=root, env=env, check=True)
                    receipt = json.loads((root / 'advanced-study-run-receipt.json').read_text())
                    self.assertIs(receipt['storage_stop'], expected)
                    self.assertIs((root / 'advanced-study-storage-stop.json').is_file(), expected)


class ThinRunEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('run_export', ROOT / 'scripts/export_advanced_evidence.py')
        cls.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.module)

    def test_bounded_bundle_keeps_reports_and_truthful_fixed_state_route(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory); runtime = repo / 'runtime'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / '.git/football-cloud-state.json').write_text(json.dumps({'state_oid': 'a' * 40, 'source_sha': 'b' * 40}))
            path = runtime / 'advanced_models/epl/protocol/reports/latest.json'
            path.parent.mkdir(parents=True); path.write_bytes(b'{"actual_report":true}')
            historical = runtime / 'advanced_models/objects/old.json'
            historical.parent.mkdir(); historical.write_bytes(b'old evidence stays at fixed Git state')
            result = self.module.export(runtime, repo / 'evidence.tar.gz', repo=repo, league='epl')
            self.assertFalse(result['self_contained_original_closure'])
            with tarfile.open(repo / 'evidence.tar.gz') as archive:
                manifest = json.load(archive.extractfile('advanced-evidence-manifest.json'))
                self.assertEqual(manifest['state_marker']['state_oid'], 'a' * 40)
                self.assertFalse(manifest['self_contained_original_closure'])
                self.assertEqual(archive.extractfile(path.relative_to(repo).as_posix()).read(), path.read_bytes())
                self.assertNotIn(historical.relative_to(repo).as_posix(), archive.getnames())

    def test_limit_failure_never_writes_a_misleading_complete_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory); runtime = repo / 'runtime'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            path = runtime / 'advanced_models/study_runs/latest.json'
            path.parent.mkdir(parents=True); path.write_bytes(b'x' * 32)
            output = repo / 'evidence.tar.gz'
            with self.assertRaisesRegex(ValueError, 'limit exceeded'):
                self.module.export(runtime, output, repo=repo, max_bytes=16)
            self.assertFalse(output.exists())

    def test_report_with_credential_is_rejected_before_any_archive_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory); runtime = repo / 'runtime'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            path = runtime / 'advanced_models/study_runs/latest.json'
            path.parent.mkdir(parents=True); path.write_bytes(b'{"mistake":"synthetic-test-credential"}')
            output = repo / 'evidence.tar.gz'
            with mock.patch.dict(os.environ, FOOTBALL_API_KEY='synthetic-test-credential'):
                with self.assertRaisesRegex(StorageError, '凭据'):
                    self.module.export(runtime, output, repo=repo)
            self.assertFalse(output.exists())


class CurrentStudyBindingTests(unittest.TestCase):
    def test_current_report_rejects_wrong_league_source_run_attempt_and_old_latest(self):
        from scripts.advanced_study_window import current_report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'latest.json'; window = root / 'window.json'
            value = {'configured_leagues': ['epl'], 'source_commit': 'a' * 40, 'generated_at': '2026-10-16T01:00:01Z'}
            bound = {'schema_version': 'advanced-study-command-window-v1', 'github_run_id': '123',
                     'github_run_attempt': '1', 'source_commit': 'a' * 40, 'configured_leagues': ['epl'],
                     'started_at': '2026-10-16T01:00:00Z', 'ended_at': '2026-10-16T01:00:02Z',
                     'report_changed': True, 'previous_report_sha256': None}
            with mock.patch.dict(os.environ, GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1'):
                for report_patch, window_patch, expected in (
                    ({}, {}, 'current_report_verified'),
                    ({'configured_leagues': ['serie_a']}, {}, 'old_report_not_current'),
                    ({'source_commit': 'b' * 40}, {}, 'old_report_not_current'),
                    ({'generated_at': '2026-10-16T00:59:59Z'}, {}, 'old_report_not_current'),
                    ({}, {'github_run_id': '122'}, 'old_report_not_current'),
                    ({}, {'github_run_attempt': '2'}, 'old_report_not_current'),
                    ({}, {'report_changed': False}, 'old_report_not_current'),
                    ({}, {'ended_at': None}, 'old_report_not_current'),
                ):
                    with self.subTest(report_patch=report_patch, window_patch=window_patch):
                        path.write_text(json.dumps({**value, **report_patch}))
                        window.write_text(json.dumps({**bound, 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **window_patch}))
                        actual = current_report('failure', 'epl', window=window, report=path)
                        self.assertEqual(actual['status'], expected)
                        self.assertEqual('report' in actual, expected == 'current_report_verified')
                self.assertEqual(current_report('skipped', 'epl', window=window, report=path)['status'], 'not_executed_in_this_step')

    def test_command_wrapper_binds_success_and_early_failure_without_reusing_old_report(self):
        from scripts.advanced_study_window import current_report, run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'latest.json'; window = root / 'window.json'
            script = ('from datetime import datetime,timezone; import json; from pathlib import Path; '
                      'Path(' + repr(str(path)) + ').write_text(json.dumps({'
                      '"configured_leagues":["epl"],"source_commit":"' + 'a' * 40 + '",'
                      '"generated_at":datetime.now(timezone.utc).isoformat()}))')
            with mock.patch.dict(os.environ, GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1'):
                self.assertEqual(run([sys.executable, '-c', script], 'epl', window=window, report=path), 0)
                self.assertEqual(current_report('success', 'epl', window=window, report=path)['status'], 'current_report_verified')
                self.assertEqual(run([sys.executable, '-c', 'raise SystemExit(1)'], 'epl', window=window, report=path), 1)
                actual = current_report('failure', 'epl', window=window, report=path)
                self.assertEqual(actual['status'], 'old_report_not_current')
                self.assertNotIn('report', actual)


if __name__ == '__main__':
    unittest.main()
