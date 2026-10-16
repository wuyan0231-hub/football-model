"""Keep legacy future records while avoiding duplicate full-state short jobs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


class ScheduleConsolidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.advanced = (cls.root / '.github/workflows/advanced-models.yml').read_text()
        cls.feature = (cls.root / '.github/workflows/feature-models.yml').read_text()
        cls.main_job = cls.advanced.split('  advanced:\n', 1)[1].split('  study_leagues:\n', 1)[0]

    def step(self, step_id):
        for block in re.split(r'(?m)^      - ', self.main_job):
            if re.search(r'^        id: ' + re.escape(step_id) + r'$', block, re.M):
                return block
        self.fail('missing step: ' + step_id)

    def test_one_short_scheduler_keeps_both_daily_research_routes(self):
        self.assertEqual(re.findall(r"cron: '([^']+)'", self.feature), ['47 1 * * *'])
        self.assertEqual(re.findall(r"cron: '([^']+)'", self.advanced), ['10,40 * * * *', '7 2 * * *'])
        self.assertIn("LEGACY_SHORT_RUN: ${{ github.event_name == 'schedule' && github.event.schedule != '7 2 * * *' }}", self.main_job)
        self.assertIn("STUDY_RUN: ${{ github.event_name != 'schedule' || github.event.schedule == '47 1 * * *' }}", self.feature)
        self.assertIn("timeout-minutes: ${{ (github.event_name != 'schedule' || github.event.schedule == '7 2 * * *') && 180 || 110 }}", self.main_job)
        self.assertIn('  workflow_dispatch:', self.feature)
        self.assertIn('branches: [main, codex/feature-models-20261013, codex/s3-paired-research-20261013]', self.feature)
        for workflow in (self.advanced, self.feature):
            self.assertIn('football-model-state', workflow)
            self.assertIn('cancel-in-progress: false', workflow)
            self.assertIn('queue: max', workflow)
            self.assertNotIn('workflow_run:', workflow)

    def test_shared_short_job_loads_and_saves_once(self):
        self.assertEqual(self.main_job.count('python scripts/cloud_state.py load --runtime runtime'), 1)
        self.assertEqual(self.main_job.count('python scripts/cloud_state.py save --runtime runtime'), 1)
        facts = self.step('facts')
        self.assertIn('--league all --horizon-hours 11 --recent-matches 5', facts)
        self.assertEqual(self.main_job.count('python -m football_model.collect_fundamentals'), 1)
        config = json.loads((self.root / 'config.json').read_text())
        self.assertIn('serie_a', [x['key'] for x in config['competitions'] if x.get('mode') != 'audit_only'])
        for stage in ('legacy_capture_v1', 'legacy_capture_v2', 'legacy_scoring_v1', 'legacy_scoring_v2'):
            self.assertLess(self.main_job.index('id: restore'), self.main_job.index('id: ' + stage))
            self.assertLess(self.main_job.index('id: ' + stage), self.main_job.index('id: persist'))
        self.assertLess(self.main_job.index('id: legacy_capture_v2'), self.main_job.index('id: scoring'))

    def test_legacy_commands_keep_exact_protocol_and_parent_identity(self):
        for stage, module, verb, protocol in (
            ('legacy_capture_v1', 'feature_snapshots', 'capture', 'feature-models.json'),
            ('legacy_capture_v2', 'feature_snapshots', 'capture', 'feature-models-v2.json'),
            ('legacy_scoring_v1', 'feature_scoring', 'score', 'feature-models.json'),
            ('legacy_scoring_v2', 'feature_scoring', 'score', 'feature-models-v2.json'),
        ):
            block = self.step(stage)
            self.assertIn("if: env.IS_RELEASE == 'true' && env.LEGACY_SHORT_RUN == 'true'", block)
            command = re.search(r'^        run: (.+)$', block, re.M).group(1)
            self.assertIn(f'python -m football_model.{module} {verb}', command)
            self.assertIn('--runtime runtime --config config.json --league serie_a', command)
            self.assertIn('--source-commit "$GITHUB_SHA"', command)
            self.assertTrue(command.endswith('--protocol ' + protocol))
            self.assertIn(command.replace('$ADVANCED_PARENT_STATE', '$FEATURE_PARENT_STATE'), self.feature)
            if verb == 'capture':
                self.assertIn('--state-commit "$ADVANCED_PARENT_STATE"', command)
            self.assertNotIn('--as-of', command)

    def test_legacy_failure_remains_visible_and_does_not_stop_other_attempts(self):
        for stage in ('legacy_capture_v1', 'legacy_capture_v2', 'legacy_scoring_v1', 'legacy_scoring_v2'):
            self.assertIn('continue-on-error: true', self.step(stage))
            self.assertIn(f"steps.{stage}.outcome != 'success'", self.main_job)
            self.assertIn('${{ steps.' + stage + '.outcome }}', self.main_job)
        self.assertNotIn('.conclusion', self.main_job)
        self.assertIn("if: ${{ !cancelled() && env.IS_RELEASE == 'true' && steps.check.outcome == 'success' }}", self.step('persist'))

    def test_daily_club_schedule_batch_precedes_optional_history_and_research(self):
        step = self.step('club_schedule_collection')
        self.assertIn("if: env.IS_RELEASE == 'true' && env.STUDY_RUN == 'true'", step)
        self.assertIn('--league all --source-commit "$GITHUB_SHA" --max-run-seconds 600 --reserve-requests 1500 --current-cache-seconds 21600', step)
        self.assertIn('continue-on-error: true', step)
        self.assertLess(self.main_job.index('id: club_schedule_collection'), self.main_job.index('id: pilot_collection'))
        self.assertLess(self.main_job.index('id: club_schedule_collection'), self.main_job.index('id: research'))
        self.assertIn("steps.club_schedule_collection.outcome != 'success'", self.main_job)
        self.assertIn("receipt['full_club_schedule_collection_completed']=receipt['CLUB_SCHEDULE_COLLECTION']=='success'", self.main_job)

    def test_partial_facts_do_not_skip_independent_precapture_steps(self):
        before = self.advanced.split('  pre_study_capture:\n', 1)[1].split('  advanced:\n', 1)[0]
        for stage, module in (('durable', 'advanced_durability'), ('facts', 'collect_fundamentals'),
                              ('confirmation', 'advanced_confirmation'), ('capture', 'advanced_snapshots')):
            block = next(b for b in re.split(r'(?m)^      - ', before)
                         if re.search(r'^        id: ' + stage + '$', b, re.M))
            self.assertIn('continue-on-error: true', block)
            self.assertIn('python -m football_model.' + module, block)
            self.assertEqual(block.count('python -m football_model.'), 1)
            self.assertIn(f"steps.{stage}.outcome != 'success'", before)
            self.assertIn(stage + '_outcome: ${{ steps.' + stage + '.outcome }}', before)
        self.assertIn('PRE_FACTS: ${{ needs.pre_study_capture.outputs.facts_outcome }}', self.main_job)
        self.assertIn("needs.pre_study_capture.result == 'failure'", self.main_job)

    def test_research_admission_requires_a_successfully_persisted_pre_state(self):
        expression = self.main_job.split('    if: >-\n', 1)[1].split('    runs-on:', 1)[0].strip()
        expression = expression.replace('!cancelled()', 'True').replace('&&', ' and ').replace('||', ' or ')
        expression = re.sub(r'\btrue\b', 'True', expression)
        fixed = {'needs.verify.result': 'success', 'github.event.repository.private': True, 'github.event_name': 'push',
                 'vars.ENABLE_SCHEDULE': 'true'}
        for result, restored, saved, expected in (
            ('failure', 'success', 'success', True),
            ('failure', 'failure', 'success', False),
            ('failure', 'success', 'failure', False),
            ('success', 'success', 'success', True),
            ('skipped', '', '', True),
        ):
            values = {**fixed, 'needs.pre_study_capture.result': result,
                      'needs.pre_study_capture.outputs.restore_outcome': restored,
                      'needs.pre_study_capture.outputs.persist_outcome': saved}
            case = expression
            for key in sorted(values, key=len, reverse=True):
                case = case.replace(key, repr(values[key]))
            self.assertEqual(eval(' '.join(case.split()), {'__builtins__': {}}, {}), expected)

    def test_precapture_budget_reserves_time_for_measured_full_state_persistence(self):
        before = self.advanced.split('  pre_study_capture:\n', 1)[1].split('  advanced:\n', 1)[0]
        timeout = int(re.search(r'(?m)^    timeout-minutes: (\d+)$', before).group(1)) * 60
        # 38496501033 attempt2 reached save at 14:33 after starting at 14:14.
        # 38496500922 attempt2 needed 14:06-14:12 just for the final commit/push;
        # its full check/save sequence was 13:34-14:12. Preserve 15m headroom.
        restore_facts_capture_seconds = 14 * 60
        observed_check_and_save_seconds = 38 * 60
        self.assertGreaterEqual(timeout, restore_facts_capture_seconds +
                                observed_check_and_save_seconds + 15 * 60)
        self.assertLessEqual(timeout, 120 * 60)
        self.assertIn("if: ${{ !cancelled() && steps.restore.outcome == 'success' }}", before)
        self.assertEqual(before.count('python scripts/cloud_state.py save --runtime runtime'), 1)

    def test_legacy_artifacts_preserve_models_snapshots_and_original_parent(self):
        self.assertIn('python scripts/export_advanced_evidence.py --runtime runtime --repo .', self.main_job)
        self.assertNotIn("rglob('*')", self.main_job)
        self.assertIn("Path('feature-run-parent.json').write_bytes(Path('advanced-run-parent.json').read_bytes())", self.main_job)

    def receipt_script(self):
        block = self.main_job.split('      - name: 保存逐步骤事实回执\n', 1)[1].split('      - name:', 1)[0]
        return textwrap.dedent(block.split("          python - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0])

    def run_receipt(self, short, failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = b'{"source_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","state_oid":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","loaded_at":"2026-10-13T10:00:00Z"}\n'
            (root / 'advanced-run-parent.json').write_bytes(parent)
            old_study = root / 'runtime/advanced_models/study_runs/latest.json'
            old_study.parent.mkdir(parents=True)
            old_study.write_text(json.dumps({'research_full_completed': True, 'source_commit': 'a' * 40,
                                            'configured_leagues': ['epl'], 'generated_at': '2026-10-13T10:00:00Z'}))
            env = dict(os.environ, GITHUB_STEP_SUMMARY=str(root / 'summary.md'), GITHUB_SHA='a' * 40,
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', ADVANCED_PARENT_STATE='b' * 40,
                       IS_RELEASE='true', STUDY_RUN='false' if short else 'true',
                       LEGACY_SHORT_RUN='true' if short else 'false', RESEARCH='skipped')
            env['PYTHONPATH'] = str(self.root) + os.pathsep + env.get('PYTHONPATH', '')
            for key in ('RESTORE', 'DURABLE', 'FACTS', 'CONFIRMATION', 'CAPTURE', 'SCORING', 'CHECK', 'PERSIST',
                        'LEGACY_CAPTURE_V1', 'LEGACY_CAPTURE_V2', 'LEGACY_SCORING_V1', 'LEGACY_SCORING_V2'):
                env[key] = 'success'
            env['LEGACY_CAPTURE_V1'] = 'failure' if failure else 'success'
            result = subprocess.run([sys.executable, '-c', self.receipt_script()], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            advanced = json.loads((root / 'advanced-run-receipt.json').read_text())
            self.assertEqual(advanced['research_report_binding']['status'], 'not_executed_in_this_step')
            self.assertNotIn('research_report', advanced)
            self.assertFalse(advanced['research_full_completed'])
            if not short:
                self.assertFalse((root / 'feature-run-receipt.json').exists())
                self.assertFalse((root / 'feature-run-parent.json').exists())
                self.assertNotIn('legacy_short_cycle_receipt', advanced)
                return
            legacy = json.loads((root / 'feature-run-receipt.json').read_text())
            self.assertEqual((root / 'feature-run-parent.json').read_bytes(), parent)
            self.assertEqual(legacy['FEATURE_PARENT_STATE'], 'b' * 40)
            self.assertEqual(legacy['FACTS_OUTCOME'], 'success')
            self.assertEqual(legacy['CAPTURE_OUTCOME'], 'failure' if failure else 'success')
            self.assertEqual(legacy['CAPTURE_V2_OUTCOME'], 'success')
            self.assertEqual(legacy['RESEARCH_OUTCOME'], 'skipped')
            self.assertEqual(legacy['RESEARCH_V2_OUTCOME'], 'skipped')
            self.assertEqual(legacy['SCHEDULE_OUTCOME'], 'skipped')
            self.assertEqual(legacy['facts_scope'], 'all')
            self.assertEqual(legacy['dispatch_mode'], 'shared_short_cycle')
            self.assertFalse(legacy['confirmation_active'])
            self.assertFalse(legacy['production_promoted'])
            self.assertEqual(advanced['LEGACY_CAPTURE_V1'], legacy['CAPTURE_OUTCOME'])

    def test_short_cycle_receipt_preserves_failure_and_shared_provenance(self):
        self.run_receipt(short=True, failure=True)

    def test_full_research_does_not_fabricate_legacy_short_cycle_receipt(self):
        self.run_receipt(short=False)


if __name__ == '__main__':
    unittest.main()
