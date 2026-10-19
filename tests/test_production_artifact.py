from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts.export_production_evidence import export
from football_model.state_storage import StorageError


class ThinProductionArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name); self.runtime = self.repo / 'runtime'
        self.git('init', '-q')
        self.git('-c', 'user.name=Artifact Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-q', '-m', 'Source')
        self.source = self.git('rev-parse', 'HEAD').decode().strip()
        self.context = self.repo / 'context.json'
        self.context.write_text(json.dumps({'source_commit': self.source, 'started_at': '2026-10-16T01:00:00Z'}))
        self.summary = self.repo / 'summary.json'; self.summary.write_text('{"layered_summary":"preserved"}')
        self.report = {'run_id': 'current_run', 'source_commit': self.source,
                       'generated_at': '2026-10-16T01:00:30Z', 'honest_failure_denominator': 7}
        self.write('runtime/reports/latest.json', self.report)
        self.write('runtime/reports/history/current_run.json', self.report)
        self.write('runtime/reports/latest.md', '# Current report\n')
        self.write('runtime/integration/index.json', {'competitions': []})
        self.write('runtime/predictions/old/huge.json', {'excluded_original': True})
        self.metadata = {'schema_version': 1, 'source_sha': self.source, 'github_run_id': '123',
                         'github_run_attempt': '1', 'saved_at': '2026-10-16T01:01:00Z'}
        self.saved = self.commit_state(self.metadata)
        self.output = self.repo / 'report.tar.gz'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def write(self, relative, value):
        path = self.repo / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def commit_state(self, metadata):
        self.write('state_metadata.json', metadata)
        self.git('add', '--', 'runtime', 'state_metadata.json')
        self.git('-c', 'user.name=Artifact Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', 'Saved state')
        saved = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('reset', '--soft', self.source)
        (self.repo / '.git/football-cloud-state.json').write_text(json.dumps({
            'schema_version': 1, 'state_oid': saved, 'source_sha': self.source}))
        return saved

    def export(self, **changes):
        kwargs = dict(repo=self.repo, runtime=self.runtime, context_path=self.context, summary_path=self.summary,
                      output=self.output, persist_outcome='success', source_sha=self.source, run_id='123', run_attempt='1',
                      now=datetime(2026,10,16,1,2,tzinfo=timezone.utc))
        kwargs.update(changes)
        return export(**kwargs)

    def test_same_run_saved_thin_bundle_keeps_report_failures_and_true_routes(self):
        value = self.export()
        self.assertLessEqual(value['raw_bytes_including_manifest'], 64 * 1024 * 1024)
        self.assertFalse(value['self_contained_original_closure'])
        with tarfile.open(self.output) as archive:
            manifest = json.load(archive.extractfile('production-evidence-manifest.json'))
            self.assertEqual(manifest['saved_state_commit'], self.saved)
            self.assertFalse(manifest['self_contained_original_closure'])
            self.assertEqual(manifest['schema_version'], 'production-thin-report-v1')
            self.assertEqual(manifest['read_route']['storage_manifest'], 'state_storage.json')
            self.assertFalse(manifest['all_cache_or_runtime_included'])
            report = json.load(archive.extractfile('runtime/reports/latest.json'))
            self.assertEqual(report['honest_failure_denominator'], 7)
            self.assertIn('football-run-summary.json', archive.getnames())
            self.assertNotIn('runtime/predictions/old/huge.json', archive.getnames())

    def test_persist_failure_never_takes_thin_path(self):
        with self.assertRaisesRegex(ValueError, 'persist_not_success'):
            self.export(persist_outcome='failure')
        self.assertFalse(self.output.exists())

    def test_loaded_or_other_run_marker_cannot_claim_this_run_save(self):
        for key, value in (('github_run_id', '122'), ('github_run_attempt', '2'),
                           ('source_sha', 'a' * 40), ('saved_at', '2026-10-16T00:59:59Z')):
            with self.subTest(key=key):
                self.commit_state({**self.metadata, key: value})
                with self.assertRaisesRegex(ValueError, 'marker_not_a_save_from_this_run_attempt'):
                    self.export()
                self.assertFalse(self.output.exists())

    def test_bounded_limit_includes_manifest_and_never_leaves_partial_archive(self):
        with self.assertRaisesRegex(ValueError, 'raw_limit_exceeded'):
            self.export(max_bytes=1024)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_name(self.output.name + '.tmp').exists())

    def test_mutation_after_save_refuses_thin_and_preserves_original_for_fallback(self):
        path = self.runtime / 'reports/latest.json'
        path.write_text(json.dumps({**self.report, 'unsaved_result': True}))
        with self.assertRaisesRegex(ValueError, 'runtime_changed_after_save'):
            self.export()
        self.assertTrue(json.loads(path.read_text())['unsaved_result'])
        self.assertFalse(self.output.exists())

    def test_secret_in_external_summary_never_enters_archive(self):
        self.summary.write_text('{"mistake":"synthetic-artifact-credential"}')
        with mock.patch.dict(os.environ, FOOTBALL_API_KEY='synthetic-artifact-credential'):
            with self.assertRaisesRegex(StorageError, '凭据'):
                self.export()
        self.assertFalse(self.output.exists())

    def test_old_report_from_current_saved_tree_is_not_called_current_report(self):
        self.write('runtime/reports/latest.json', {**self.report, 'generated_at': '2026-10-15T01:00:00Z'})
        self.commit_state(self.metadata)
        with self.assertRaisesRegex(ValueError, 'latest_report_not_this_run'):
            self.export()
        self.assertFalse(self.output.exists())


WORKFLOW = (Path(__file__).parents[1] / '.github/workflows/football.yml').read_text()


def condition_of_named_step(name):
    block = WORKFLOW.split('      - name: ' + name + '\n', 1)[1].split('      - ', 1)[0]
    return re.search(r'if: \$\{\{ (.+) \}\}', block).group(1)


def evaluate(expression, *, cancelled=False, league='epl', **outcomes):
    expression = expression.replace('!cancelled()', repr(not cancelled)).replace('&&', ' and ').replace('||', ' or ')
    expression = expression.replace('env.LEAGUE', repr(league))
    for key in set(re.findall(r'steps\.([a-z_]+)\.outcome', expression)):
        expression = expression.replace('steps.' + key + '.outcome', repr(outcomes.get(key, 'success')))
    return eval(expression, {'__builtins__': {}}, {})


class ProductionArtifactWorkflowTests(unittest.TestCase):
    def test_original_subset_preserved_for_failed_save_and_failed_thin_export(self):
        step = '上传未保存或导出失败的原便捷副本（不含全部缓存，保留 14 天）'
        expression = condition_of_named_step(step)
        self.assertFalse(evaluate(expression, persist='success', thin_evidence='success'))
        self.assertTrue(evaluate(expression, persist='success', thin_evidence='failure'))
        self.assertTrue(evaluate(expression, persist='failure', thin_evidence='skipped'))
        self.assertFalse(evaluate(expression, check='failure', persist='failure', thin_evidence='skipped'))
        self.assertFalse(evaluate(expression, cancelled=True, persist='failure'))
        body = WORKFLOW.split('          name: football-report-${{ github.run_id }}-${{ github.run_attempt }}', 1)[1].split('      - name:', 1)[0]
        paths = body.split('          path: |\n', 1)[1].split('          if-no-files-found:', 1)[0].split()
        self.assertEqual(paths, ['runtime/reports/', 'runtime/predictions/', 'runtime/models/', 'runtime/registries/',
                                'runtime/audits/', 'runtime/fundamentals/', 'runtime/prospective/', 'runtime/result_sync/',
                                'runtime/quotes/', 'runtime/shadow_models/', 'runtime/total_shrink_shadow/', 'runtime/replay/'])
        self.assertIn('retention-days: 14', body)

    def test_export_failure_remains_final_failure_even_after_fallback(self):
        expression = condition_of_named_step('将实际失败标记为失败')
        self.assertFalse(evaluate(expression, persist='success', thin_evidence='success'))
        self.assertTrue(evaluate(expression, persist='success', thin_evidence='failure'))
        self.assertTrue(evaluate(expression, persist='success', thin_evidence='skipped'))
        self.assertTrue(evaluate(expression, persist='failure', thin_evidence='skipped'))
        self.assertFalse(evaluate(expression, league='none', persist='failure', thin_evidence='skipped'))

    def test_schedule_real_save_and_public_layered_summary_contract_unchanged(self):
        self.assertEqual(re.findall(r"cron: '([^']+)'", WORKFLOW), ['17 1 * * *'])
        self.assertEqual(WORKFLOW.count('run: python scripts/cloud_state.py save --runtime runtime'), 1)
        push_paths = WORKFLOW.split('    paths:\n', 1)[1].split('  workflow_dispatch:', 1)[0]
        self.assertNotIn('export_production_evidence', push_paths)
        self.assertNotIn('.github/workflows/football.yml', push_paths)
        layered = WORKFLOW.split('      - name: 上传分层摘要（独立于原运行附件布局）\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn('name: football-run-summary-${{ github.run_id }}-${{ github.run_attempt }}', layered)
        self.assertIn('path: ${{ runner.temp }}/football-run-summary.json', layered)
        self.assertIn('retention-days: 14', layered)
        thin = WORKFLOW.split('      - name: 上传已保存状态的薄报告（不含完整原件闭包）\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn('retention-days: 2', thin)
        self.assertIn('compression-level: 0', thin)
        self.assertNotIn('runtime/models/', thin)


if __name__ == '__main__':
    unittest.main()

