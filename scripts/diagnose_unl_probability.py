"""Diagnostic harness only: replay fixed-source UNL and preserve a real score failure.

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
import subprocess
import sys
import tempfile
import traceback

SOURCE_SHA = '9603500cfc322fdcb76af65ef78e25dafd8f77e4'
SOURCE_TREE = '93983bc5005bc944840cf35c9080ce54fed9919d'
IMPLEMENTATION_SHA = '7c58fa1bad6e783155f031a4b03f1e3dc7e3521f247ecb729da0fbf842e58135'
TARGET_ERROR = 'invalid 1X2 probability values'


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--state-commit', required=True)
    parser.add_argument('--expected-input-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path, help='Diagnostic directory outside runtime.')
    parser.add_argument('--study-start', help='Optional original study-start ISO time, for historical reproduction only.')
    args = parser.parse_args()
    source, runtime, output = args.source_root.resolve(), args.runtime.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt = {'schema_version': 'unl-probability-diagnosis-receipt-v1', 'actual_started_at': now(),
               'algorithm_source_commit': args.source_commit, 'fixed_state_commit': args.state_commit,
               'diagnostic_code_commit': os.environ.get('GITHUB_SHA'),
               'diagnostic_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'workflow_run_id': os.environ.get('GITHUB_RUN_ID'),
               'workflow_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
               'purpose': 'isolated historical failure reproduction; no prospective or production qualification',
               'api_collection_called': False, 'model_state_save_called': False,
               'target_error_captures': [], 'diagnostic_capture_errors': [], 'r_calls': 0, 'score_calls': 0}
    original_score = original_fit = original_study = None
    ar = adapter = None
    exit_code = 1
    try:
        if args.source_commit != SOURCE_SHA or not re.fullmatch(r'[0-9a-f]{40}', args.state_commit):
            raise ValueError('Expected fixed source960 and a full 40-character state commit.')
        if not re.fullmatch(r'[0-9a-f]{64}', args.expected_input_sha256):
            raise ValueError('Expected exact input SHA-256.')
        if output == runtime or output.is_relative_to(runtime):
            raise ValueError('Diagnostic output must be outside runtime.')
        receipt['algorithm_checkout_head'] = git(source, 'rev-parse', 'HEAD')
        receipt['algorithm_checkout_tree'] = git(source, 'rev-parse', 'HEAD^{tree}')
        if receipt['algorithm_checkout_tree'] != SOURCE_TREE:
            raise ValueError('Algorithm tree differs from fixed source960.')
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
        if implementation['sha256'] != IMPLEMENTATION_SHA:
            raise ValueError('Algorithm dependency bytes differ from the verified source960 implementation.')
        receipt['implementation_fingerprint'] = implementation
        receipt['environment'] = {'python': sys.version, 'numpy': np.__version__,
                                  'scipy': scipy.__version__, 'statsmodels': statsmodels.__version__}
        original_score, original_fit, original_study = ar.score_forecast, adapter.fit_predict_goalmodel, ar.study
        source_lines = Path(ar.__file__).read_text().splitlines()
        r_records, forecast_to_record = [], {}

        with tempfile.TemporaryDirectory(prefix='unl-r-context-', dir=output.parent) as temp:
            spool = Path(temp)

            def fit_wrapper(*fit_args, **fit_kwargs):
                frame = inspect.currentframe().f_back
                local = frame.f_locals
                call_context = {'candidate': local.get('candidate'), 'horizon': local.get('horizon'),
                                'block': local.get('block_id'), 'caller_line': frame.f_lineno}
                del frame, local
                # No catch or retry around the real R adapter.
                result = original_fit(*fit_args, **fit_kwargs)
                receipt['r_calls'] += 1
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

            def score_wrapper(prediction, match, **kwargs):
                receipt['score_calls'] += 1
                try:
                    return original_score(prediction, match, **kwargs)
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
                                        'algorithm_source_commit': SOURCE_SHA, 'fixed_state_commit': args.state_commit,
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
                                 '--source-commit', SOURCE_SHA, '--state-commit', args.state_commit, '--run-goalmodel'])
            receipt['original_advanced_main_exit_code'] = exit_code
    except BaseException as exc:
        receipt['uncaught_error'] = {'class': type(exc).__name__, 'detail': str(exc), 'traceback': traceback.format_exc()}
        raise
    finally:
        if ar is not None and original_score is not None:
            ar.score_forecast = original_score
            ar.study = original_study
        if adapter is not None and original_fit is not None:
            adapter.fit_predict_goalmodel = original_fit
        receipt['actual_finished_at'] = now()
        receipt['target_error_reproduced'] = bool(receipt['target_error_captures'])
        receipt['diagnostic_exit_code'] = exit_code
        write(output / 'receipt.json', receipt)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
