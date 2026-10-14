"""Isolated verification: replay fixed-state UNL with the repaired source and real R.

Deploy separately from the algorithm checkout. No football API or cloud-state
save is called. Ordinary scoring and R return values pass through unchanged;
the original exception is re-raised after evidence capture. Local study outputs
are disposable diagnostic outputs and must never be promoted or state-pushed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

TARGET_ERROR = 'invalid 1X2 probability values'
REPORT_CLOSURE_MAX_BYTES = 512 * 1024 * 1024
REPORT_CLOSURE_DISK_RESERVE_BYTES = 512 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def raw(value):
    # Finite values keep Python's round-trip float representation. Any nonfinite
    # value is explicitly tagged, rather than silently replaced or emitted as
    # non-standard JSON; repr/hex are also recorded at the failing probability.
    def encode(item):
        if isinstance(item, float) and not math.isfinite(item):
            return {'$diagnostic_nonfinite_float': repr(item)}
        if isinstance(item, dict):
            return {k: encode(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [encode(v) for v in item]
        return item
    return (json.dumps(encode(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def sha(value):
    return hashlib.sha256(raw(value)).hexdigest()


def write(path, value):
    encoded = raw(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {'path': str(path), 'sha256': hashlib.sha256(encoded).hexdigest(), 'bytes': len(encoded)}


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()


def is_repaired_target(prediction, match, local, source_line):
    # Loop locals retain the previous candidate while M0 is scored. Bind the
    # actual prediction implementation and callsite, not that stale variable.
    return (prediction.get('model_scope') == 'independent_goalmodel_research_only'
            and prediction.get('model_version') == 'pinned_goalmodel_saved_fit_prediction_v3'
            and (match.get('match_id'), local.get('candidate'), local.get('horizon'), local.get('block_id'))
                == (1545612, 'G1', 'LINEUP', 52)
            and source_line.strip() == 'row["scores"][candidate] = score_forecast(prediction, f, extended_diagnostics=True)')


def preserve_scored_output(scored, capture, receipt):
    try:
        capture()
    except Exception as exc:
        receipt['diagnostic_capture_errors'].append({'stage': 'score_success_capture',
            'class': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()})
    return scored


def export_report_closure(runtime, output, reference):
    runtime = runtime.resolve()
    output = output.resolve()
    if output == runtime or output.is_relative_to(runtime):
        raise ValueError('Diagnostic report output must remain outside fixed runtime.')
    visited, entries, total = set(), [], 0

    def copy_reference(ref):
        nonlocal total
        relative = Path(ref['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Report closure requires canonical relative paths.')
        path = (runtime / ref['path']).resolve()
        if not path.is_relative_to(runtime):
            raise ValueError('Report closure reference escapes fixed runtime.')
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != ref['sha256']:
            raise ValueError('Report closure reference SHA-256 mismatch.')
        if path in visited:
            return
        if len(encoded) > 60 * 1024 * 1024 or total + len(encoded) > REPORT_CLOSURE_MAX_BYTES:
            raise ValueError('Diagnostic report closure exceeds bounded artifact budget.')
        if shutil.disk_usage(output.parent).free < len(encoded) + REPORT_CLOSURE_DISK_RESERVE_BYTES:
            raise ValueError('Diagnostic report closure disk reserve exceeded before write.')
        visited.add(path)
        total += len(encoded)
        destination = (output / ref['path']).resolve()
        if not destination.is_relative_to(output):
            raise ValueError('Report closure destination escapes diagnostic output.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
        entries.append({'path': ref['path'], 'sha256': ref['sha256'], 'bytes': len(encoded)})
        walk(json.loads(encoded))

    def walk(value):
        if isinstance(value, dict):
            if '$advanced_object_ref' in value:
                copy_reference(value['$advanced_object_ref'])
            elif '$advanced_list_chunks' in value:
                for ref in value['$advanced_list_chunks']:
                    copy_reference(ref)
            else:
                for item in value.values():
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    copy_reference(reference)
    write(output / 'manifest.json', {'schema_version': 'unl-study-report-closure-v1',
        'root_ref': reference, 'total_bytes': total, 'entries': entries,
        'logical_budget_bytes': REPORT_CLOSURE_MAX_BYTES,
        'disk_reserve_bytes': REPORT_CLOSURE_DISK_RESERVE_BYTES,
        'scope': 'exact compact report serialization closure; provenance input refs remain bound to fixed full state'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--source-tree', required=True)
    parser.add_argument('--expected-implementation-sha256', required=True)
    parser.add_argument('--state-commit', required=True)
    parser.add_argument('--expected-input-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path, help='Diagnostic directory outside runtime.')
    parser.add_argument('--study-start', help='Optional original study-start ISO time, for historical reproduction only.')
    args = parser.parse_args()
    source, runtime, output = args.source_root.resolve(), args.runtime.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt = {'schema_version': 'unl-probability-repair-verification-receipt-v1', 'actual_started_at': now(),
               'algorithm_source_commit': args.source_commit, 'fixed_state_commit': args.state_commit,
               'diagnostic_code_commit': os.environ.get('GITHUB_SHA'),
               'diagnostic_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'workflow_run_id': os.environ.get('GITHUB_RUN_ID'),
               'workflow_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
               'purpose': 'isolated fixed-input repaired-source historical verification; no prospective or production qualification',
               'api_collection_called': False, 'model_state_save_called': False,
               'target_error_captures': [], 'target_success_captures': [], 'target_repair_verified': False,
               'successful_real_r_calls': 0, 'r_native_matrix_summaries': [], 'native_target_captures': [],
               'r_adapter_failures': [], 'diagnostic_capture_errors': [], 'r_calls': 0, 'score_calls': 0}
    original_score = original_fit = original_study = original_validate = None
    ar = adapter = None
    exit_code = 1
    try:
        if any(re.fullmatch(r'[0-9a-f]{40}', value) is None for value in
               (args.source_commit, args.source_tree, args.state_commit)):
            raise ValueError('Expected full immutable source commit, source tree and state commit.')
        if re.fullmatch(r'[0-9a-f]{64}', args.expected_implementation_sha256) is None:
            raise ValueError('Expected full implementation SHA-256.')
        if not re.fullmatch(r'[0-9a-f]{64}', args.expected_input_sha256):
            raise ValueError('Expected exact input SHA-256.')
        if output == runtime or output.is_relative_to(runtime):
            raise ValueError('Diagnostic output must be outside runtime.')
        receipt['algorithm_checkout_head'] = git(source, 'rev-parse', 'HEAD')
        receipt['algorithm_checkout_tree'] = git(source, 'rev-parse', 'HEAD^{tree}')
        if receipt['algorithm_checkout_head'] != args.source_commit or receipt['algorithm_checkout_tree'] != args.source_tree:
            raise ValueError('Algorithm commit/tree differs from the requested fixed source.')
        subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'], check=True)
        input_path = runtime / 'data' / 'unl.json'
        input_bytes = input_path.read_bytes()
        input_digest = hashlib.sha256(input_bytes).hexdigest()
        if input_digest != args.expected_input_sha256:
            raise ValueError('Restored UNL input differs from requested exact original.')
        receipt['input'] = {'path': 'data/unl.json', 'sha256': input_digest, 'bytes': len(input_bytes)}
        protocol_path = source / 'advanced-models-v1.json'
        receipt['protocol_sha256'] = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
        sys.path.insert(0, str(source))
        import numpy as np
        import scipy
        import statsmodels
        from football_model import advanced_research as ar
        from football_model import goalmodel_adapter as adapter
        implementation = ar.implementation_fingerprint()
        if implementation['sha256'] != args.expected_implementation_sha256:
            raise ValueError('Algorithm dependency bytes differ from the requested fixed implementation.')
        receipt['implementation_fingerprint'] = implementation
        receipt['environment'] = {'python': sys.version, 'numpy': np.__version__,
                                  'scipy': scipy.__version__, 'statsmodels': statsmodels.__version__}
        original_score, original_fit, original_study = ar.score_forecast, adapter.fit_predict_goalmodel, ar.study
        original_validate = adapter._validate_output
        source_lines = Path(ar.__file__).read_text().splitlines()
        r_records, forecast_to_record = [], {}
        current_r_calls = []

        with tempfile.TemporaryDirectory(prefix='unl-r-context-', dir=output.parent) as temp:
            spool = Path(temp)

            def validate_wrapper(native, fixtures, dc):
                validated = original_validate(native, fixtures, dc)
                try:
                    call = current_r_calls[-1]
                    summaries = []
                    for forecast in validated['forecasts']:
                        matrix = np.asarray(forecast['score_matrix'], dtype=float)
                        mass = math.fsum(matrix.ravel())
                        summaries.append({'match_id': forecast['match_id'], 'shape': list(matrix.shape),
                            'numpy_mass': float(matrix.sum()), 'fsum_mass': mass,
                            'absolute_mass_error': abs(mass - 1)})
                        if (call.get('candidate'), call.get('block'), forecast['match_id']) == ('G1', 52, 1545612):
                            ref = write(output / 'native-R-G1-EARLY-1545612.json', {
                                'schema_version': 'unl-actual-native-r-target-v1', 'captured_at': now(),
                                'caller': call, 'forecast': forecast,
                                'result_without_forecasts': {key: value for key, value in validated.items() if key != 'forecasts'},
                                'purpose': 'original validated native R output before compatibility aggregation; no mutation'})
                            receipt['native_target_captures'].append(ref)
                    receipt['r_native_matrix_summaries'].append({'caller': call, 'forecasts': summaries})
                except Exception as exc:
                    receipt['diagnostic_capture_errors'].append({'stage': 'native_r_matrix_capture',
                        'class': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()})
                return validated

            def fit_wrapper(*fit_args, **fit_kwargs):
                frame = inspect.currentframe().f_back
                local = frame.f_locals
                call_context = {'candidate': local.get('candidate'), 'horizon': local.get('horizon'),
                                'block': local.get('block_id'), 'caller_line': frame.f_lineno}
                del frame, local
                current_r_calls.append(call_context)
                try:
                    result = original_fit(*fit_args, **fit_kwargs)
                except Exception as exc:
                    receipt['r_adapter_failures'].append({'caller': call_context, 'class': type(exc).__name__,
                        'detail': str(exc), 'traceback': traceback.format_exc()})
                    raise
                finally:
                    current_r_calls.pop()
                receipt['r_calls'] += 1
                if result.get('status') == 'ok' and result.get('r_version'):
                    receipt['successful_real_r_calls'] += 1
                try:
                    context = {'schema_version': 'unl-actual-r-call-context-v1', 'caller': call_context,
                               'adapter_arguments': {'training_matches': fit_args[0] if fit_args else fit_kwargs.get('training_matches'),
                                                     'fixtures': fit_args[1] if len(fit_args) > 1 else fit_kwargs.get('fixtures'),
                                                     'options': fit_kwargs},
                               'result_without_forecasts': {k: v for k, v in result.items() if k != 'forecasts'},
                               'forecast_summaries': [{'match_id': f.get('match_id'),
                                                       'forecast_sha256': sha(f),
                                                       'score_matrix_sha256': sha(f.get('score_matrix'))}
                                                      for f in result.get('forecasts', [])]}
                    context['adapter_arguments_sha256'] = sha(context['adapter_arguments'])
                    context['request_hash_note'] = 'result.request_sha256 is the real adapter canonical bridge-request hash; adapter_arguments_sha256 separately identifies harness-captured call arguments.'
                    path = spool / f'r-call-{receipt["r_calls"]:04d}.json'
                    ref = write(path, context)
                    record = {'candidate': call_context['candidate'], 'cutoff': result.get('cutoff'),
                              'fit_sha256': sha(result.get('fit')), 'context_ref': ref}
                    index = len(r_records)
                    r_records.append(record)
                    for forecast in result.get('forecasts', []):
                        forecast_to_record[id(forecast)] = index
                except Exception as exc:
                    receipt['diagnostic_capture_errors'].append({'stage': 'r_context_spool', 'class': type(exc).__name__, 'detail': str(exc)})
                return result

            def capture_target_success(prediction, match, scored, options, frame):
                try:
                    local = frame.f_locals
                    source_line = source_lines[frame.f_lineno - 1] if 0 < frame.f_lineno <= len(source_lines) else ''
                    if not is_repaired_target(prediction, match, local, source_line):
                        return
                    saved_card = local.get('historical_goal_cards', {}).get(1545612, {}).get('G1')
                    if not saved_card:
                        raise ValueError('Actual target has no saved EARLY R fit.')
                    hits = [record for record in r_records if record['candidate'] == 'G1'
                            and record['cutoff'] == saved_card.get('training_cutoff')
                            and record['fit_sha256'] == sha(saved_card.get('goalmodel_fit'))]
                    if len(hits) != 1:
                        raise ValueError('Target cannot bind uniquely to its actual real R call.')
                    ref = hits[0]['context_ref']
                    encoded = Path(ref['path']).read_bytes()
                    if hashlib.sha256(encoded).hexdigest() != ref['sha256']:
                        raise ValueError('Actual R context spool hash mismatch.')
                    context = json.loads(encoded)
                    if context['result_without_forecasts'].get('status') != 'ok' or not context['result_without_forecasts'].get('r_version'):
                        raise ValueError('Target R parent is not a successful actual R fit.')
                    matrix = np.asarray(prediction['score_matrix'], dtype=float)
                    values = prediction['probabilities']
                    if not all(math.isfinite(v) and 0 <= v <= 1 for v in values.values()):
                        raise ValueError('Target repair did not produce bounded finite 1X2.')
                    evidence = {'schema_version': 'unl-actual-repaired-target-v1', 'captured_at': now(),
                        'algorithm_source_commit': args.source_commit, 'fixed_state_commit': args.state_commit,
                        'implementation_fingerprint': implementation, 'input': receipt['input'],
                        'protocol_sha256': receipt['protocol_sha256'], 'environment': receipt['environment'],
                        'callsite': {'candidate': 'G1', 'horizon': 'LINEUP', 'block': 52, 'match_id': 1545612,
                                     'path': 'football_model/advanced_research.py', 'line': frame.f_lineno},
                        'prediction': prediction, 'fixture_record': match, 'scores': scored, 'score_options': options,
                        'saved_early_goalmodel_card': saved_card, 'actual_r_context': context,
                        'historical_feature_row': local.get('features', {}).get(1545612),
                        'numeric': {'matrix_shape': list(matrix.shape), 'matrix_sum': float(matrix.sum()),
                            'matrix_fsum': math.fsum(matrix.ravel()), 'matrix_sha256': sha(prediction['score_matrix']),
                            'probability_repr': {key: repr(value) for key, value in values.items()},
                            'expected_goals': prediction['expected_goals']},
                        'preservation_note': 'Harness passes through actual repaired algorithm outputs and strict scoring. No alternate fitting, clipping or matrix mutation.'}
                    evidence_ref = write(output / 'repaired-G1-LINEUP-1545612.json', evidence)
                    receipt['target_success_captures'].append(evidence_ref)
                    print(json.dumps({'repaired_target_captured': evidence_ref}), flush=True)
                finally:
                    del frame

            def score_wrapper(prediction, match, **kwargs):
                receipt['score_calls'] += 1
                try:
                    scored = original_score(prediction, match, **kwargs)
                    frame = inspect.currentframe().f_back
                    try:
                        return preserve_scored_output(scored,
                            lambda: capture_target_success(prediction, match, scored, kwargs, frame), receipt)
                    finally:
                        del frame
                except Exception as exc:
                    if str(exc) == TARGET_ERROR:
                        frame = inspect.currentframe().f_back
                        try:
                            local, line = frame.f_locals, frame.f_lineno
                            line_text = source_lines[line - 1] if 0 < line <= len(source_lines) else ''
                            candidate = local.get('candidate')
                            if 'row["scores"]["M0"]' in line_text:
                                candidate = 'M0'
                            elif 'row["scores"]["COLD_START"]' in line_text:
                                candidate = 'COLD_START'
                            elif 'parent_on_added_training_pool' in line_text:
                                candidate = local.get('parent')
                            fixture_id = match.get('match_id')
                            saved_card = local.get('historical_goal_cards', {}).get(fixture_id, {}).get(candidate)
                            record_index = forecast_to_record.get(id(prediction))
                            if record_index is None and saved_card:
                                wanted_fit = sha(saved_card.get('goalmodel_fit'))
                                hits = [i for i, item in enumerate(r_records)
                                        if item['candidate'] == candidate and item['cutoff'] == saved_card.get('training_cutoff')
                                        and item['fit_sha256'] == wanted_fit]
                                if len(hits) == 1:
                                    record_index = hits[0]
                            r_context = None
                            if record_index is not None:
                                ref = r_records[record_index]['context_ref']
                                contents = Path(ref['path']).read_bytes()
                                if hashlib.sha256(contents).hexdigest() != ref['sha256']:
                                    raise ValueError('Diagnostic R context spool hash mismatch.')
                                r_context = json.loads(contents)
                            matrix = np.asarray(prediction.get('score_matrix'), dtype=float)
                            probabilities = prediction.get('probabilities', {})
                            replay = local.get('replay', {})
                            block = local.get('block_id', match.get('block'))
                            baseline_fit = next((f for f in replay.get('baseline_fits', []) if f.get('block') == block), None)
                            evidence = {'schema_version': 'unl-actual-probability-failure-v1', 'captured_at': now(),
                                        'algorithm_source_commit': args.source_commit, 'fixed_state_commit': args.state_commit,
                                        'diagnostic_code_commit': receipt['diagnostic_code_commit'],
                                        'implementation_fingerprint': implementation, 'input': receipt['input'],
                                        'protocol_sha256': receipt['protocol_sha256'], 'environment': receipt['environment'],
                                        'exception': {'class': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()},
                                        'callsite': {'path': 'football_model/advanced_research.py', 'line': line, 'source_line': line_text,
                                                     'function': frame.f_code.co_name, 'league': local.get('league'),
                                                     'candidate': candidate, 'horizon': local.get('horizon'), 'block': block,
                                                     'match_id': fixture_id, 'study_started_at': local.get('now')},
                                        'prediction': prediction, 'fixture_record': match,
                                        'score_options': kwargs,
                                        'numeric': {'probability_repr': {k: repr(v) for k, v in probabilities.items()},
                                                    'probability_hex': {k: float(v).hex() for k, v in probabilities.items()},
                                                    'matrix_shape': list(matrix.shape), 'matrix_sum': float(matrix.sum()),
                                                    'matrix_sum_hex': float(matrix.sum()).hex(),
                                                    'matrix_min': float(matrix.min()), 'matrix_max': float(matrix.max()),
                                                    'matrix_all_finite': bool(np.isfinite(matrix).all()),
                                                    'matrix_any_negative': bool((matrix < 0).any()),
                                                    'matrix_sha256': sha(prediction.get('score_matrix')),
                                                    'expected_goals': prediction.get('expected_goals')},
                                        'saved_early_goalmodel_card': saved_card, 'actual_r_context': r_context,
                                        'r_context_found': r_context is not None,
                                        'historical_feature_row': local.get('features', {}).get(fixture_id),
                                        'baseline_fit': baseline_fit,
                                        'preservation_note': 'Original prediction, matrix and fixture are retained; original exception is re-raised. No clipping, normalization, validation relaxation or replacement fitting was performed by the harness.'}
                            number = len(receipt['target_error_captures']) + 1
                            safe_candidate = re.sub(r'[^A-Za-z0-9_-]', '_', str(candidate))
                            path = output / f'failure-{number:04d}-{safe_candidate}-{fixture_id}.json'
                            ref = write(path, evidence)
                            receipt['target_error_captures'].append({'callsite': evidence['callsite'], 'evidence': ref})
                            print(json.dumps({'diagnostic_target_error_captured': ref, 'candidate': candidate,
                                              'horizon': local.get('horizon'), 'match_id': fixture_id}), flush=True)
                        except Exception as capture_exc:
                            receipt['diagnostic_capture_errors'].append({'stage': 'score_failure_capture',
                                'class': type(capture_exc).__name__, 'detail': str(capture_exc), 'traceback': traceback.format_exc()})
                        finally:
                            del frame
                    raise

            adapter.fit_predict_goalmodel = fit_wrapper
            adapter._validate_output = validate_wrapper
            ar.score_forecast = score_wrapper
            if args.study_start:
                fixed_start = datetime.fromisoformat(args.study_start.replace('Z', '+00:00'))
                if fixed_start.tzinfo is None or fixed_start > datetime.now(timezone.utc):
                    raise ValueError('Historical study start must be timezone-aware and not future.')
                receipt['effective_historical_study_start'] = fixed_start.isoformat()
                receipt['clock_note'] = 'Only the initial study cutoff replays the supplied historical time. Diagnostic actual execution times and subsequent generated_at calls remain current; local outputs have no prospective qualification.'
                def study_wrapper(*study_args, **study_kwargs):
                    first = True
                    def clock():
                        nonlocal first
                        if first:
                            first = False
                            return fixed_start
                        return datetime.now(timezone.utc)
                    study_kwargs['clock'] = clock
                    return original_study(*study_args, **study_kwargs)
                ar.study = study_wrapper
            os.chdir(source)
            exit_code = ar.main(['study', '--runtime', str(runtime), '--league', 'unl',
                                 '--config', str(source / 'config.json'), '--protocol', str(protocol_path),
                                 '--source-commit', args.source_commit, '--state-commit', args.state_commit, '--run-goalmodel'])
            receipt['actual_advanced_main_exit_code'] = exit_code
            summary_path = runtime / 'advanced_models/study_runs/latest.json'
            summary = json.loads(summary_path.read_text())
            if summary.get('source_commit') != args.source_commit or summary.get('configured_leagues') != ['unl']:
                raise ValueError('Current study-run summary is not bound to this fixed source/league.')
            write(output / 'study-run-summary.json', summary)
            report_directory = runtime / 'advanced_models/unl' / ar.digest(ar.validate_protocol(json.loads(protocol_path.read_text()))) / 'reports'
            if exit_code != 0 or summary.get('research_full_completed') is not True:
                receipt['first_study_failure'] = next((result for result in summary['results']
                    if result.get('status') != 'completed_development'), None)
                attempt_path = report_directory / 'latest_attempt.json'
                if attempt_path.exists():
                    attempt = json.loads(attempt_path.read_text())
                    if attempt.get('source_commit') == args.source_commit:
                        write(output / 'first-study-attempt.json', attempt)
                        ref = attempt.get('failure_ref')
                        if ref:
                            failure_path = (runtime / ref['path']).resolve()
                            if (not failure_path.is_relative_to(runtime)
                                    or hashlib.sha256(failure_path.read_bytes()).hexdigest() != ref['sha256']):
                                raise ValueError('Actual first study failure reference mismatch.')
                            write(output / 'first-study-failure.json', json.loads(failure_path.read_bytes()))
                exit_code = 1
                return exit_code
            report_summary = json.loads((report_directory / 'latest.json').read_text())
            write(output / 'study-report-summary.json', report_summary)
            report_ref = report_summary['full_report_ref']
            export_report_closure(runtime, output / 'study-report-closure', report_ref)
            report = ar.load_report(runtime, report_ref)
            if (report.get('source_commit') != args.source_commit or report.get('state_commit') != args.state_commit
                    or report.get('implementation_fingerprint', {}).get('sha256') != args.expected_implementation_sha256):
                raise ValueError('Current immutable study report source/state/implementation binding mismatch.')
            target_rows = [row for row in report['scored_matches'] if row.get('match_id') == 1545612 and row.get('horizon') == 'LINEUP']
            if len(target_rows) != 1 or 'G1' not in target_rows[0]['scores']:
                raise ValueError('Actual repaired target missing from immutable study report.')
            if len(receipt['target_success_captures']) != 1:
                raise ValueError('Expected exactly one actual repaired target capture.')
            target_capture = json.loads(Path(receipt['target_success_captures'][0]['path']).read_text())
            if (target_rows[0]['predictions'].get('G1') != target_capture['prediction']
                    or target_rows[0]['scores']['G1'] != target_capture['scores']):
                raise ValueError('Immutable report differs from actual repaired target prediction/scoring.')
            write(output / 'study-report-target-row.json', target_rows[0])
            receipt['study_report_ref'] = report_ref
            if (exit_code != 0 or summary.get('research_full_completed') is not True
                    or len(receipt['target_success_captures']) != 1 or receipt['successful_real_r_calls'] == 0
                    or receipt['target_error_captures'] or receipt['diagnostic_capture_errors']):
                raise ValueError('Repaired target/real R/full-study verification conditions not met.')
            receipt['target_repair_verified'] = True
    except BaseException as exc:
        exit_code = 1
        receipt['uncaught_error'] = {'class': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()}
        raise
    finally:
        if ar is not None and original_score is not None:
            ar.score_forecast = original_score
            ar.study = original_study
        if adapter is not None and original_fit is not None:
            adapter.fit_predict_goalmodel = original_fit
        if adapter is not None and original_validate is not None:
            adapter._validate_output = original_validate
        receipt['actual_finished_at'] = now()
        receipt['target_error_reproduced'] = bool(receipt['target_error_captures'])
        receipt['diagnostic_exit_code'] = exit_code
        write(output / 'receipt.json', receipt)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())

