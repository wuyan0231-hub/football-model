"""Manual short cycles retain the scheduled execution and persistence gates."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from tests.test_advanced_research_only_workflow import (
    WORKFLOW, PREPARATION, LEGACY, condition, context, evaluate, job, step,
)


class ShortOnlyWorkflowTests(unittest.TestCase):
    def short_context(self, **kwargs):
        value = context(short_only=True, **kwargs)
        for name in ("verify", "pre_study_capture"):
            value["needs"][name]["result"] = "skipped"
        for identity in (*PREPARATION[:3], "research"):
            value["steps"][identity]["outcome"] = "skipped"
        return value

    def test_manual_input_defaults_to_false_and_keeps_schedules(self):
        dispatch = WORKFLOW.split("  workflow_dispatch:\n", 1)[1].split("  schedule:\n", 1)[0]
        self.assertRegex(dispatch, r"short_only:\n[\s\S]*required: false\n[\s\S]*type: boolean\n[\s\S]*default: false\n")
        self.assertEqual(re.findall(r"cron: '([^']+)'", WORKFLOW), ['10,40 * * * *', '7 2 * * *'])

    def test_short_cycle_skips_expensive_jobs_but_keeps_all_existing_short_steps(self):
        value = self.short_context()
        self.assertTrue(evaluate(condition(job("validate_dispatch"), "    "), value))
        self.assertTrue(evaluate(condition(job("advanced"), "    "), value))
        for name in ("verify", "pre_study_capture", "study_leagues", "study_finalize"):
            self.assertFalse(evaluate(condition(job(name), "    "), value), name)
        self.assertEqual(value["env"]["SHORT_ONLY"], "true")
        self.assertEqual(value["env"]["STUDY_RUN"], "false")
        self.assertEqual(value["env"]["LEGACY_SHORT_RUN"], "true")
        self.assertEqual(value["env"]["RESEARCH_ONLY"], "false")
        body = job("advanced")
        for identity in PREPARATION[:3]:
            self.assertFalse(evaluate(condition(step(body, identity)), value), identity)
        self.assertFalse(evaluate(condition(step(body, "research")), value))
        for identity in ("restore", "durable", *PREPARATION[3:], *LEGACY, "check", "persist", "export"):
            self.assertTrue(evaluate(condition(step(body, identity)), value), identity)
        timeout = re.search(r"(?m)^    timeout-minutes: (.+)$", body).group(1)
        timeout = timeout.removeprefix("${{").removesuffix("}}").strip()
        self.assertTrue(evaluate("(" + timeout + ") == 110", value))
        # Commands stay shared with the scheduled path: no duplicate state
        # restore/save, fixture collector, or altered protocol is introduced.
        self.assertEqual(body.count('python scripts/cloud_state.py load --runtime runtime'), 1)
        self.assertEqual(body.count('python scripts/cloud_state_save_once.py --runtime runtime'), 1)
        for identity in (*PREPARATION[3:], *LEGACY):
            self.assertIn('continue-on-error: true', step(body, identity))

    def test_conflicting_flags_are_rejected_before_any_state_writer(self):
        for name in ("verify", "pre_study_capture", "advanced", "study_leagues", "study_finalize"):
            value = self.short_context(research_only=True)
            self.assertFalse(evaluate(condition(job(name), "    "), value), name)
        block = step(job("validate_dispatch"), name="验证手动模式互斥与短轮分支")
        script = '\n'.join(line[10:] for line in block.split('        run: |\n', 1)[1].splitlines())
        for research, short, ref, expected in (
            ('false', 'false', 'refs/heads/main', 0),
            ('true', 'false', 'refs/heads/main', 0),
            ('false', 'true', 'refs/heads/main', 0),
            ('true', 'true', 'refs/heads/main', 1),
            ('false', 'true', 'refs/heads/review', 1),
        ):
            with self.subTest(research=research, short=short, ref=ref):
                result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                    env={**os.environ, 'RESEARCH_ONLY': research, 'SHORT_ONLY': short, 'DISPATCH_REF': ref})
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_short_mode_is_explicit_manual_main_and_source_repository_only(self):
        for event, schedule, expected_study, expected_legacy in (
            ('push', '', 'true', 'false'),
            ('schedule', '7 2 * * *', 'true', 'false'),
            ('schedule', '10,40 * * * *', 'false', 'true'),
        ):
            value = context(event, short_only=True, schedule=schedule)
            self.assertEqual(value['env']['SHORT_ONLY'], 'false')
            self.assertEqual(value['env']['STUDY_RUN'], expected_study)
            self.assertEqual(value['env']['LEGACY_SHORT_RUN'], expected_legacy)
        value = self.short_context(main=False)
        self.assertFalse(evaluate(condition(job('advanced'), '    '), value))
        for repository in ('another-owner/football-model', 'wuyan0231-hub/another-repo'):
            value = self.short_context(repository=repository)
            for name in ('validate_dispatch', 'verify', 'pre_study_capture', 'advanced', 'study_leagues', 'study_finalize'):
                self.assertFalse(evaluate(condition(job(name), '    '), value), (repository, name))

    def test_every_short_failure_stays_failed_and_check_still_gates_save(self):
        body = job('advanced')
        gate = condition(step(body, name='工程失败保持失败'))
        value = self.short_context()
        self.assertFalse(evaluate(gate, value))
        for identity in ('restore', 'durable', *PREPARATION[3:], *LEGACY, 'check', 'persist'):
            for outcome in ('failure', 'skipped'):
                with self.subTest(identity=identity, outcome=outcome):
                    value['steps'][identity]['outcome'] = outcome
                    self.assertTrue(evaluate(gate, value))
                    value['steps'][identity]['outcome'] = 'success'
        for outcome in ('failure', 'skipped'):
            value['steps']['check']['outcome'] = outcome
            self.assertFalse(evaluate(condition(step(body, 'persist')), value))
        for outcome in ('failure', 'cancelled', 'skipped'):
            value['needs']['validate_dispatch']['result'] = outcome
            self.assertFalse(evaluate(condition(job('advanced'), '    '), value))

    def test_manual_receipt_records_short_mode_and_preserves_legacy_provenance(self):
        block = step(job('advanced'), name='保存逐步骤事实回执')
        script = textwrap.dedent(block.split("          python - <<'PY'\n", 1)[1].split("          PY", 1)[0])
        value = self.short_context()
        env = {**value['env'], **{key.upper(): entry['outcome'] for key, entry in value['steps'].items()},
               'PRE_STUDY_CAPTURE': 'skipped', 'ADVANCED_PARENT_STATE': 'a' * 40,
               'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = b'{"state_oid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
            (root / 'advanced-run-parent.json').write_bytes(parent)
            env['GITHUB_STEP_SUMMARY'] = str(root / 'summary')
            previous = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.dict(os.environ, env):
                    exec(compile(script, '<actual workflow receipt>', 'exec'), {})
            finally:
                os.chdir(previous)
            receipt = json.loads((root / 'advanced-run-receipt.json').read_text())
            self.assertEqual(receipt['SHORT_ONLY'], 'true')
            self.assertEqual(receipt['RESEARCH'], 'skipped')
            self.assertFalse(receipt['research_full_completed'])
            self.assertFalse(receipt['full_collection_completed'])
            self.assertEqual(receipt['LEGACY_CAPTURE_V1'], 'success')
            legacy = json.loads((root / 'feature-run-receipt.json').read_text())
            self.assertEqual(legacy['FEATURE_PARENT_STATE'], 'a' * 40)
            self.assertEqual(legacy['dispatch_mode'], 'shared_short_cycle')
            self.assertEqual(legacy['FACTS_OUTCOME'], 'success')
            self.assertEqual(legacy['CAPTURE_V2_OUTCOME'], 'success')
            self.assertEqual(legacy['SCORING_V2_OUTCOME'], 'success')
            self.assertEqual((root / 'feature-run-parent.json').read_bytes(), parent)


if __name__ == '__main__':
    unittest.main()
