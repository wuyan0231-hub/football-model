"""Bounded, read-only successor for one already authorized research attempt.

Artifacts are data, never executable inputs. Original failures and unavailable
receipts remain visible. No collection, fitting, activation, or state saving.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
import zlib

REPO = 'wuyan0231-hub/football-model'
REPO_ID = 1380205666
RUN = 38928003279
ATTEMPT = 2
WORKFLOW_ID = 382774946
SOURCE = '4ae6764f2502c8892bb892c3a08bf0a34d604b1a'
OLD = '1e311689b3405f64c6ad9636732d2defb7ad67e26b4bc7e04b01a5b1b2b2d6ac'
CURRENT = '041d304f087669467c66ea0f3d67c710411098d95dbd670b7f7531673239a96d'
ANCHOR = 'a99395311e2eaf3cea2f06487aab564362021f23'
PROTOCOL = '18a3498b0d24276904c54fe8c063ff7dbf1ce24d300c6cb6cf8632206e0fd448'
WORKFLOW_HASH = 'aa7ba5bd396d9d64f2f685e9c267c85bb3f46703513c0ea9397424d47c224d34'
VALIDATE_HASH = '2824a7398a13930264f6e5252d97dc9366a2fed56cf345ab707b69a9c8b0d16e'
HELPERS = {
    'scripts/fixed_research_report_audit.py':'886791d65c62bc4586a750ee3228da059018b87fdb50ef51a35cc51317583bf6',
    'scripts/fixed_development_gate_audit.py':'4c6a3e7077a9a562427a13b87c613d589701d9379d800f8e2d7ecec018d4d52b',
    'scripts/fixed_fixture_validation_audit.py':'8c87a4bb58350fe2497790e6fe76c225fc46cdd4f836659ebe0982aac3db52dc',
}
TARGETS = ('mls', 'unl', 'liga_mx', 'segunda', 'argentina_a')
SKIPPED = ('epl','championship','laliga','bundesliga','serie_a','ligue_1','bundesliga_2','ligue_2','allsvenskan','eredivisie','primeira','brazil_a','j1','k1')
MIB = 1024 * 1024
JSON_LIMIT = 4 * MIB
LOG_LIMIT = 16 * MIB
ZIP_LIMIT = 96 * MIB
UNPACK_LIMIT = 128 * MIB
UPLOAD_LIMIT = 40 * MIB
ACTIVE_CHILD = None


class Refused(ValueError):
    pass


def need(value, code):
    if not value:
        raise Refused(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_helpers(repo):
    actual={name:sha((repo/name).read_bytes()) for name in HELPERS}
    need(actual==HELPERS,'reviewed_helper_bytes_changed')
    return actual


def strict_json(raw):
    need(len(raw) <= JSON_LIMIT, 'json_budget')
    def pairs(items):
        out = {}
        for k, v in items:
            need(k not in out, 'duplicate_json_key')
            out[k] = v
        return out
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(Refused('nonfinite_json')))


def canonical_path(name):
    need(isinstance(name, str) and name and len(name) <= 512, 'member_path')
    need(not PurePosixPath(name).is_absolute() and '\\' not in name
        and all(x not in ('', '.', '..') for x in name.split('/'))
        and not any(ord(c) < 32 for c in name), 'member_path')
    return name


def utc(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(result.tzinfo is not None, 'naive_timestamp')
    return result


def tar_headers(raw):
    offset=0;count=0
    while offset+512<=len(raw):
        header=raw[offset:offset+512]
        if not any(header):
            need(not any(raw[offset:]),'tar_trailing_data')
            return
        count+=1
        need(count<=512,'tar_headers_budget')
        field=header[124:136].rstrip(b'\0 ')
        need(bool(re.fullmatch(b'[0-7]+',field)),'tar_non_octal_size')
        size=int(field,8);kind=header[156:157]
        need(kind in (b'0',b'\0',b'x') and (kind!=b'x' or size<=1024),'tar_extended_header')
        offset+=512+((size+511)//512)*512
        need(offset<=len(raw),'tar_truncated')
    raise Refused('tar_termination')


def identity(value):
    need(str(value.get('github_run_id')) == str(RUN)
        and str(value.get('github_run_attempt')) == str(ATTEMPT)
        and value.get('source_sha', value.get('source_commit')) == SOURCE, 'receipt_identity')


def run_identity(run):
    need(run.get('id') == RUN and run.get('run_attempt') == ATTEMPT
        and run.get('head_sha') == SOURCE and run.get('head_branch') == 'main'
        and run.get('event') == 'workflow_dispatch' and run.get('status') == 'completed'
        and run.get('workflow_id') == WORKFLOW_ID
        and run.get('path') == '.github/workflows/advanced-models.yml'
        and run.get('repository', {}).get('id') == REPO_ID
        and run.get('head_repository', {}).get('id') == REPO_ID, 'upstream_identity')


def job_identity(job, name):
    need(job.get('name') == name and job.get('run_id') == RUN
        and job.get('run_attempt') == ATTEMPT and job.get('head_sha') == SOURCE
        and job.get('head_branch') == 'main' and job.get('status') == 'completed', 'job_identity')


def validate_dispatch(job, raw):
    job_identity(job, 'validate_dispatch')
    need(job.get('conclusion') == 'success' and len(raw) <= LOG_LIMIT
        and sha(raw) == VALIDATE_HASH, 'validate_dispatch_original_log')
    text = raw.decode('utf-8-sig')
    # The exact original official runner log is pinned, including its real env
    # block. This successful job was inherited by failed-only attempt 2.
    for key, value in {'RESEARCH_ONLY':'true','SHORT_ONLY':'false',
        'DISPATCH_REF':'refs/heads/main','RESEARCH_LEAGUES':'remaining_after_epl_championship'}.items():
        need(len(re.findall(r'^\S+   '+key+': '+re.escape(value)+r'\s*$', text, re.M)) == 1,
            'dispatch_input_mismatch')


def unpack(raw, metadata, league):
    need(len(raw) <= ZIP_LIMIT and len(raw) == metadata.get('size_in_bytes')
        and metadata.get('digest') == 'sha256:' + sha(raw), 'official_artifact_digest')
    need(metadata.get('name') == f'advanced-study-{league}-{RUN}-{ATTEMPT}'
        and metadata.get('expired') is False, 'official_artifact_identity')
    wr = metadata.get('workflow_run', {})
    need(wr.get('id') == RUN and wr.get('repository_id') == REPO_ID
        and wr.get('head_repository_id') == REPO_ID and wr.get('head_sha') == SOURCE
        and wr.get('head_branch') == 'main', 'artifact_source_identity')
    need(len(raw)>=22 and raw[-22:-18]==b'PK\x05\x06','zip_eocd')
    _,disk,central_disk,on_disk,count,central_size,central_offset,comment=struct.unpack('<4s4H2LH',raw[-22:])
    need(disk==central_disk==comment==0 and on_disk==count and 0<count<=32
        and central_size<=64*1024 and central_offset+central_size==len(raw)-22,'zip_central_budget')
    outer = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        total = 0
        need(len(archive.infolist()) == count, 'zip_members_budget')
        for item in archive.infolist():
            need(item.orig_filename==item.filename,'zip_name_truncation')
            name = canonical_path(item.filename)
            need('/' not in name and name not in outer and not item.is_dir()
                and ((item.external_attr >> 16) & 0o170000) in (0, 0o100000), 'zip_member_type')
            need(item.compress_type==zipfile.ZIP_STORED and item.compress_size==item.file_size
                and not item.flag_bits&1,'zip_original_stored_contract')
            need(name in ('advanced-models-evidence.tar.gz','advanced-models-evidence.tar.sha256')
                or re.fullmatch(r'advanced-[a-z-]+\.json', name), 'zip_member_allowlist')
            total += item.file_size
            need(total <= UNPACK_LIMIT and item.file_size <= ZIP_LIMIT, 'zip_expansion_budget')
            outer[name] = archive.read(item)  # verifies CRC, never extractall
    tar_raw = outer.get('advanced-models-evidence.tar.gz', b'')
    need(bool(tar_raw) and outer.get('advanced-models-evidence.tar.sha256', b'').decode().split()
        == [sha(tar_raw), 'advanced-models-evidence.tar.gz'], 'tar_digest')
    decoder=zlib.decompressobj(31)
    expanded=decoder.decompress(tar_raw,64*MIB+1)
    need(len(expanded)<=64*MIB and decoder.eof and not decoder.unused_data
        and not decoder.unconsumed_tail,'actual_tar_expansion_budget')
    tar_headers(expanded)
    members = {}
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
        total = 0
        for item in archive:
            name = canonical_path(item.name)
            need(item.isfile() and set(item.pax_headers)<= {'path'} and not item.sparse
                and item.type in (tarfile.REGTYPE,tarfile.AREGTYPE)
                and name not in members and len(members) < 256, 'tar_member_type')
            need(not item.pax_headers or item.pax_headers.get('path')==name,'tar_pax_path')
            need(name == 'advanced-evidence-manifest.json'
                or re.fullmatch(r'advanced-[a-z-]+\.json', name)
                or re.fullmatch(r'runtime/(advanced_models|advanced_features|feature_models|advanced_scoring)/[a-zA-Z0-9_./-]+\.json', name), 'tar_member_allowlist')
            total += item.size
            need(total <= 64 * MIB and item.size <= 64 * MIB, 'tar_expansion_budget')
            members[name] = archive.extractfile(item).read(item.size + 1)
            need(len(members[name]) == item.size, 'tar_size')
    manifest = strict_json(members['advanced-evidence-manifest.json'])
    need(manifest.get('schema_version') == 'advanced-run-thin-evidence-v1'
        and manifest.get('league_selection') == league
        and manifest.get('source_commit') == SOURCE
        and str(manifest.get('github_run_id')) == str(RUN)
        and str(manifest.get('github_run_attempt')) == str(ATTEMPT), 'thin_manifest_identity')
    entries = manifest.get('entries', [])
    need(len(entries) == len(members) - 1, 'manifest_member_count')
    seen = set()
    for ref in entries:
        name = canonical_path(ref['path'])
        need(name not in seen and name in members and len(members[name]) == ref['bytes']
            and sha(members[name]) == ref['sha256'], 'manifest_entry_hash')
        seen.add(name)
    for name in set(outer) & set(members):
        need(outer[name] == members[name], 'zip_tar_duplicate_mismatch')
    return outer, members, manifest


def bind(outer, members, manifest, league, job, log_raw):
    job_identity(job, f'study_leagues ({league})')
    need(len(log_raw) <= LOG_LIMIT, 'job_log_budget')
    run = strict_json(outer['advanced-study-run-receipt.json'])
    checked_raw = outer['advanced-checkpoint-checked-save.json']
    checked = strict_json(checked_raw)
    final = strict_json(members['advanced-league-save-run-receipt.json'])
    identity(checked); identity(final)
    need(run.get('GITHUB_SHA') == SOURCE and run.get('GITHUB_RUN_ID') == str(RUN)
        and run.get('GITHUB_RUN_ATTEMPT') == str(ATTEMPT)
        and run.get('STUDY_LEAGUE') == league, 'native_receipt_identity')
    need(run.get('original_research_command', {}).get('receipt_sha256') == sha(checked_raw), 'checked_receipt_hash')
    # Recovery is deliberately not inferred from guessed fields. A real recovery
    # remains a distinct saved-but-not-yet-supported audit scope.
    need(checked.get('recovered_state_only') is False, 'recovery_saved_but_audit_unsupported')
    need(checked.get('operation_completed') is True and checked.get('original_exit_code') == 0
        and checked.get('wrapper_exit_code') == 0 and checked.get('status') == 'command_completed'
        and checked.get('source_ref') == 'refs/heads/main', 'native_operation_incomplete')
    need(job.get('conclusion')=='success','normal_save_job_failure_scope_unverified')
    study = checked.get('study', {})
    fp = checked.get('implementation_fingerprint', {})
    need(study.get('league') == league and study.get('protocol_hash') == PROTOCOL
        and study.get('protocol_raw_sha256') == '4139069bbe6f13c27020b32655dedfa8620475ff80287955a43ee96b00501364'
        and study.get('goalmodel_requested') is True and fp.get('sha256') == OLD
        and len(fp.get('files', {})) == 50, 'native_research_identity')
    digest = sha(json.dumps(fp['files'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
    need(digest == OLD, 'native_fingerprint_hash')
    parent = checked.get('before_marker', {}).get('state_oid')
    need(run.get('ADVANCED_PARENT_STATE') == parent, 'loaded_parent_mismatch')
    expected = ['python','-m','football_model.advanced_research','study','--runtime','runtime',
        '--league',league,'--protocol','advanced-models-v1.json','--source-commit',SOURCE,
        '--state-commit',parent,'--run-goalmodel','--checkpoint-state','--repo','.']
    need(checked.get('command') == expected, 'native_command_mismatch')
    need(final.get('child_exit_code') == 0 and final.get('receipt_parse_status') == 'valid'
        and final.get('runtime_validation', {}).get('status') == 'passed'
        and final.get('resource_preflight', {}).get('status') == 'passed'
        and final.get('resource_preflight', {}).get('report', {}).get('can_save') is True
        and final.get('pack_probe', {}).get('status') == 'passed'
        and final.get('save', {}).get('status') == 'saved_and_marker_verified'
        and final.get('save', {}).get('marker_matches_reported_commit') is True, 'native_final_save_unproved')
    state = final.get('save', {}).get('reported_commit')
    need(isinstance(state, str) and re.fullmatch('[0-9a-f]{40}', state)
        and state == final.get('post_state_oid') == run.get('post_save_state', {}).get('state_oid')
        == manifest.get('state_marker', {}).get('state_oid'), 'final_checkpoint_confusion')
    checkpoint = final.get('parent_state_oid')
    need(checkpoint != parent and checkpoint != state, 'checkpoint_chain_not_distinct')
    text = log_raw.decode('utf-8-sig')
    saved = re.findall(r'^\S+ MODEL_STATE_SAVED_COMMIT=([0-9a-f]{40})\s*$', text, re.M)
    need(saved == [checkpoint, state], 'official_saved_log_chain')
    for name in ('单联赛真实研究与独立持久检查点','保存当次汇总与失败原件'):
        matches = [s for s in job.get('steps', []) if s.get('name') == name]
        need(len(matches) == 1 and matches[0].get('conclusion') == 'success', 'native_save_step_failure')
    binding = run.get('study_report_binding', {})
    raw = members['runtime/advanced_models/study_runs/latest.json']
    need(binding.get('status') == 'current_report_verified' and binding.get('sha256') == sha(raw), 'native_study_binding')
    window_raw=members['advanced-study-run-window.json']
    need(binding.get('window_sha256')==sha(window_raw),'native_window_hash')
    window=strict_json(window_raw);identity(window)
    need(window.get('configured_leagues')==[league] and window.get('command_returncode')==0
        and window.get('report_sha256')==sha(raw) and window.get('report_changed') is True
        and window.get('report_path')=='runtime/advanced_models/study_runs/latest.json','native_window_binding')
    need(utc(job['started_at'])<=utc(window['started_at'])<=utc(window['ended_at'])<=utc(job['completed_at']),
        'native_window_job_time')
    native = strict_json(raw)
    need(native.get('source_commit')==SOURCE and native.get('configured_leagues') == [league]
        and native.get('stored_verified_completed_leagues') == 1
        and native.get('stored_research_durable_full_completed') is True
        and len(native.get('results', [])) == 1 and native['results'][0].get('league') == league
        and native['results'][0].get('durable_saved') is True
        and native['results'][0].get('calculation_completed') is True
        and native['results'][0].get('durable_state_commit')==checkpoint, 'native_stored_closure_incomplete')
    summary_path = f'runtime/advanced_models/{league}/{PROTOCOL}/reports/latest.json'
    summary = strict_json(members[summary_path])
    need(summary.get('league') == league and summary.get('source_commit') == SOURCE
        and summary.get('protocol_hash') == PROTOCOL
        and summary.get('implementation_fingerprint') == fp, 'artifact_summary_identity')
    checkpoint_path=f'runtime/advanced_models/checkpoints/{league}/{PROTOCOL}/latest.json'
    checkpoint_pointer=strict_json(members[checkpoint_path])
    need(checkpoint_pointer.get('checkpoint_ref')==native['results'][0].get('checkpoint_ref'),'checkpoint_pointer_binding')
    return {'league':league,'state_commit':state,'checkpoint_state':checkpoint,'loaded_parent':parent,
        'summary_path':summary_path,'summary_sha256':sha(members[summary_path]),'summary':summary,
        'checkpoint_path':checkpoint_path,'checkpoint_sha256':sha(members[checkpoint_path]),
        'implementation':fp,'native_job_id':job['id'],'native_job_conclusion':job['conclusion'],
        'official_job_log_sha256':sha(log_raw),'native_current_inputs_match':native['results'][0].get('current_inputs_match'),
        'native_current_refit_required':native['results'][0].get('current_refit_required')}


def verify_commit(commit, metadata, state, parent):
    need(commit.get('sha') == state and [p.get('sha') for p in commit.get('parents', [])] == [parent], 'unique_git_parent')
    identity(metadata)
    need(metadata.get('source_ref') == 'refs/heads/main'
        and metadata.get('previous_state_oid') == parent, 'metadata_parent_identity')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target=urllib.parse.urlsplit(newurl)
        need(target.scheme == 'https' and not target.username and not target.password
            and target.port in (None,443) and target.hostname
            and (target.hostname=='api.github.com' or target.hostname.endswith(
                ('.blob.core.windows.net','.githubusercontent.com','.s3.amazonaws.com'))),'untrusted_redirect')
        count=getattr(req,'audit_redirect_count',0)+1
        need(count<=5,'redirect_budget')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        result.audit_redirect_count=count
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            result.remove_header('Authorization')
        return result


class Client:
    def __init__(self):
        self.calls = 0
        self.opener = urllib.request.build_opener(SafeRedirect())
    def get(self, path, limit=JSON_LIMIT):
        self.calls += 1
        need(self.calls <= 50 and path.startswith(f'/repos/{REPO}/'), 'request_budget_or_route')
        request = urllib.request.Request('https://api.github.com' + path,
            headers={'Authorization':'Bearer '+os.environ.get('GH_TOKEN',''),
                'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
        with self.opener.open(request, timeout=45) as response:
            raw = response.read(limit + 1)
        need(len(raw) <= limit, 'response_budget')
        return raw
    def obj(self, path):
        return strict_json(self.get(path))
    def pages(self, path, key):
        result = []
        for page in range(1, 6):
            value = self.obj(path + f'?per_page=100&page={page}')
            result.extend(value[key])
            if len(result) == value['total_count']:
                return result
        raise Refused('pagination_budget')


def unique(items, predicate, code):
    selected = [x for x in items if predicate(x)]
    need(len(selected) == 1, code)
    return selected[0]


def resolve(client, league):
    root = f'/repos/{REPO}'
    run_identity(client.obj(f'{root}/actions/runs/{RUN}/attempts/{ATTEMPT}'))
    jobs = client.pages(f'{root}/actions/runs/{RUN}/attempts/{ATTEMPT}/jobs', 'jobs')
    validation = unique(jobs, lambda x:x.get('name') == 'validate_dispatch', 'validate_job_missing_or_ambiguous')
    validate_dispatch(validation, client.get(f'{root}/actions/jobs/{validation["id"]}/logs', LOG_LIMIT))
    workflow = client.obj(f'{root}/contents/.github/workflows/advanced-models.yml?ref={SOURCE}')
    import base64
    need(workflow.get('encoding') == 'base64' and sha(base64.b64decode(workflow['content'])) == WORKFLOW_HASH, 'original_workflow_bytes')
    job = unique(jobs, lambda x:x.get('name') == f'study_leagues ({league})', 'native_job_missing_or_ambiguous')
    job_identity(job, f'study_leagues ({league})')
    artifact = unique(client.pages(f'{root}/actions/runs/{RUN}/artifacts','artifacts'),
        lambda x:x.get('name') == f'advanced-study-{league}-{RUN}-{ATTEMPT}', 'native_artifact_missing_or_ambiguous')
    need(utc(job['started_at']) <= utc(artifact['created_at']) <= utc(job['completed_at']), 'artifact_job_time_binding')
    outer, members, manifest = unpack(client.get(f'{root}/actions/artifacts/{artifact["id"]}/zip', ZIP_LIMIT), artifact, league)
    result = bind(outer, members, manifest, league, job, client.get(f'{root}/actions/jobs/{job["id"]}/logs', LOG_LIMIT))
    state=result['state_commit'];chain=[]
    for _ in range(16):
        if state==ANCHOR:
            break
        commit = client.obj(f'{root}/git/commits/{state}')
        response = client.obj(f'{root}/contents/state_metadata.json?ref={state}')
        parents=commit.get('parents',[])
        need(len(parents)==1,'unique_git_parent')
        parent=parents[0]['sha']
        if state==result['state_commit']:
            need(parent==result['checkpoint_state'],'final_checkpoint_parent')
        if state==result['checkpoint_state']:
            need(parent==result['loaded_parent'],'checkpoint_loaded_parent')
        verify_commit(commit, strict_json(base64.b64decode(response['content'])), state, parent)
        chain.append({'state':state,'parent':parent});state=parent
    need(state==ANCHOR,'bounded_cas_anchor_unavailable')
    result['unique_cas_chain_to_anchor']=chain
    result['official_artifact'] = {k:artifact[k] for k in ('id','name','size_in_bytes','digest')}
    return result


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
    need(len(raw) <= 512 * 1024, 'receipt_output_budget')
    path.write_bytes(raw)


def aggregate(directory, output):
    records=[]
    for league in TARGETS:
        matches=list(directory.glob('research-attempt2-followup-'+league+'-*/run-final.json'))
        record={'league':league,'status':'audit_incomplete'}
        try:
            need(len(matches)==1,'terminal_receipt_missing_or_ambiguous')
            path=matches[0]
            need(path.is_file() and not path.is_symlink() and path.stat().st_size<=512*1024
                and not any(p.is_symlink() for p in path.parents),'downloaded_terminal_file')
            body=path.read_bytes();value=strict_json(body)
            need(value.get('league')==league and value.get('original_run_id')==RUN
                and value.get('original_run_attempt')==ATTEMPT and value.get('original_source_commit')==SOURCE,
                'downloaded_receipt_identity')
            root=path.parent.resolve();seen=set();total=0
            for ref in value.get('output_files',[]):
                name=canonical_path(ref['path']);target=root/name
                need(type(ref.get('bytes')) is int and 0<=ref['bytes']<=UPLOAD_LIMIT-total,'downloaded_output_budget')
                need(name not in seen and target.resolve().is_relative_to(root)
                    and target.is_file() and not target.is_symlink()
                    and not any(p.is_symlink() for p in target.parents),'downloaded_output_path')
                need(target.stat().st_size==ref['bytes'],'downloaded_output_size')
                with target.open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
                need(actual==ref['sha256'],'downloaded_output_hash')
                total+=ref['bytes'];seen.add(name)
            actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=path.resolve()}
            need(total<=UPLOAD_LIMIT and actual==seen,'downloaded_manifest_coverage')
            record.update(status=value['status'],receipt_bytes=len(body),receipt_sha256=sha(body),
                native_binding=value.get('native_binding'),output_files=value.get('output_files',[]),
                verified_output_bytes=total,error_code=value.get('error_code'))
        except Exception as exc:
            record.update(error_code=str(exc) if isinstance(exc,Refused) else 'unclassified_closure_error',exception_class=type(exc).__name__)
        records.append(record)
    complete=all(x['status']=='completed_with_stated_scope' for x in records)
    write_json(output,{'schema_version':'fixed-research-attempt2-closure-v1','original_run_id':RUN,
        'original_run_attempt':ATTEMPT,'original_source_commit':SOURCE,'results':records,
        'status':'completed_with_stated_scope' if complete else 'audit_incomplete',
        'full_hypothesis_family':380,'full_metric_tests':1900,'activation_performed':False,
        'production_promoted':False,'entire_R02_closed':False,'entire_R07_closed':False,
        'all19_current_inputs_freshness_verified':False,'accuracy_improvement_verified':False,
        'skipped_already_accepted_leagues':list(SKIPPED)})
    return 0 if complete else 1


def worker(stage, repo, runtime, binding, output):
    verify_helpers(repo)
    sys.path.insert(0, str(repo / 'scripts')); sys.path.insert(0, str(repo))
    from football_model.advanced_research import implementation_fingerprint
    need(implementation_fingerprint()['sha256'] == CURRENT, 'reader_implementation_changed')
    for name in ('football_model/goalmodel_adapter.py','football_model/settlement.py'):
        need(sha((repo/name).read_bytes()) == binding['implementation']['files'][name], 'additional_numeric_source_changed')
    if stage=='restore':
        import cloud_state as cloud
        from fixed_fixture_validation_audit import restore_fixed
        write_json(output/'restore-preflight.json',restore_fixed(repo,runtime,binding['state_commit'],cloud))
        return
    from football_model.history_archive import read_bytes
    summary_raw = read_bytes(runtime / binding['summary_path'].removeprefix('runtime/'), runtime=runtime)
    need(sha(summary_raw) == binding['summary_sha256'], 'restored_summary_mismatch')
    checkpoint_raw=read_bytes(runtime/binding['checkpoint_path'].removeprefix('runtime/'),runtime=runtime)
    need(sha(checkpoint_raw)==binding['checkpoint_sha256'],'restored_checkpoint_pointer_mismatch')
    summary = strict_json(summary_raw)
    if stage == 'report':
        from fixed_research_report_audit import audit_report
        write_json(output/'report-audit.json', audit_report(runtime, summary, OLD))
    else:
        import fixed_development_gate_audit as gates
        from football_model.advanced_storage import load_artifact
        protocol_raw=(repo/'advanced-models-v1.json').read_bytes()
        design_raw=(repo/'advanced-confirmation-v1.json').read_bytes()
        need(sha(protocol_raw)==gates.PROTOCOL_RAW_SHA256 and sha(design_raw)==gates.DESIGN_RAW_SHA256, 'frozen_design_changed')
        protocol,design=strict_json(protocol_raw),strict_json(design_raw)
        report=load_artifact(runtime,summary['full_report_ref'])
        numeric=gates.validate_identity(report,summary,OLD,protocol,repo)
        out=gates.Output(output/'r07',repo)
        witness=out.witness(binding['league']+'-numeric-witness.jsonl.gz')
        try:
            computation=gates.audit_arithmetic(report,protocol,design,witness)
            ref=witness.close(True)
            out.write_json(binding['league']+'-gates.json', {'status':'completed_with_stated_scope',
                'league':binding['league'],'retrieved_state_commit':binding['state_commit'],
                'report_ref':summary['full_report_ref'],'numeric_witness':ref,
                'numerical_sources_verified':numeric,**computation})
        except BaseException:
            if witness.closed_ref is None:
                try:witness.close(False)
                except Exception:pass
            raise
        finally:
            out.write_json('manifest.json',{'files':out.files},terminal=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--league', choices=TARGETS)
    p.add_argument('--aggregate',type=Path)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--worker',choices=('restore','report','gates'))
    p.add_argument('--runtime',type=Path)
    p.add_argument('--binding',type=Path)
    args=p.parse_args(argv)
    repo=Path(__file__).resolve().parents[1]
    if args.aggregate:
        return aggregate(args.aggregate,args.output)
    need(args.league in TARGETS,'fixed_target_required')
    if args.worker:
        try:
            worker(args.worker,repo,args.runtime,strict_json(args.binding.read_bytes()),args.output)
            return 0
        except Exception as exc:
            write_json(args.output/(args.worker+'-failure.json'),{'stage':args.worker,'status':'audit_incomplete',
                'exception_class':type(exc).__name__,'error_code':str(exc) if isinstance(exc,Refused) else 'unclassified_worker_error'})
            return 1
    need(not args.output.exists() and not args.output.is_symlink(),'output_must_be_new')
    args.output.mkdir(parents=True)
    def deadline(_signum,_frame):
        if ACTIVE_CHILD is not None and ACTIVE_CHILD.poll() is None:
            os.killpg(ACTIVE_CHILD.pid,signal.SIGKILL)
            ACTIVE_CHILD.wait()
        raise Refused('binding_deadline' if _signum==signal.SIGALRM else 'overall_deadline')
    signal.signal(signal.SIGTERM,deadline)
    signal.signal(signal.SIGALRM,deadline)
    receipt={'schema_version':'fixed-research-attempt2-followup-v1','league':args.league,
        'original_run_id':RUN,'original_run_attempt':ATTEMPT,'original_source_commit':SOURCE,
        'original_implementation':OLD,'expected_auditor_implementation':CURRENT,
        'network_football_requests':0,'fits':0,'remote_state_writes':0,'activation_performed':False,
        'production_promoted':False,'skipped_already_accepted_leagues':list(SKIPPED),
        'limits':{'job_seconds':3600,'internal_seconds':3300,'restore_seconds':480,
            'report_seconds':1800,'gates_seconds':720,'combined_upload_bytes':UPLOAD_LIMIT,
            'r07_output_bytes':32*MIB,'r07_witness_bytes':28*MIB,'r07_terminal_reserve_bytes':MIB,
            'production_resource_limits_changed':False},
        'all19_native_current_inputs_verification':'not_claimed_read_original_final_artifact_separately',
        'status':'audit_in_progress','started_at':datetime.now(timezone.utc).isoformat(),
        'limitations':['Saved-score arithmetic and development gates only.','No independent raw-feature rebuild or fresh R fit.',
            'Old saved identity remains old; no current eligibility or future effect claim.']}
    stage='resolve_native_receipts'
    try:
        receipt['auditor_source_commit']=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        receipt['reviewed_helper_sha256']=verify_helpers(repo)
        print(json.dumps({'readonly_stage':stage,'event':'started'}),flush=True)
        signal.alarm(300)
        try:binding=resolve(Client(),args.league)
        finally:signal.alarm(0)
        print(json.dumps({'readonly_stage':stage,'event':'completed'}),flush=True)
        receipt['native_binding']={k:v for k,v in binding.items() if k not in ('summary','implementation')}
        with tempfile.TemporaryDirectory(prefix='fixed-attempt2-',dir=repo) as temporary:
            base=Path(temporary);runtime=base/'runtime';binding_path=base/'binding.json'
            binding_path.write_text(json.dumps(binding))
            receipt['reader_git_config_bytes']={}
            for key,value,expected in (('core.packedGitWindowSize','32m',32*MIB),('core.packedGitLimit','128m',128*MIB),('core.deltaBaseCacheLimit','32m',32*MIB)):
                subprocess.run(['git','-C',str(repo),'config','--local',key,value],check=True,stdout=subprocess.DEVNULL)
                actual=int(subprocess.check_output(['git','-C',str(repo),'config','--type=int','--get',key],text=True).strip())
                need(actual==expected,'reader_git_config_mismatch');receipt['reader_git_config_bytes'][key]=actual
            # Fixed immutable state; no save-authorizing cloud-state marker.
            for stage,budget in (('restore',480),('report',1800),('gates',720)):
                global ACTIVE_CHILD
                print(json.dumps({'readonly_stage':stage,'event':'started','budget_seconds':budget}),flush=True)
                child=subprocess.Popen([sys.executable,str(Path(__file__)), '--league',args.league,
                    '--output',str(args.output.resolve()),'--worker',stage,'--runtime',str(runtime),
                    '--binding',str(binding_path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    env={**os.environ,'FOOTBALL_API_KEY':'','PYTHONDONTWRITEBYTECODE':'1'},start_new_session=True)
                ACTIVE_CHILD=child
                try:code=child.wait(timeout=budget)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid,signal.SIGTERM)
                    try:child.wait(timeout=10)
                    except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
                    raise Refused(stage+'_deadline')
                need(code==0,stage+'_worker_incomplete')
                ACTIVE_CHILD=None
                print(json.dumps({'readonly_stage':stage,'event':'completed'}),flush=True)
        receipt['status']='completed_with_stated_scope'
    except Exception as exc:
        code=str(exc) if isinstance(exc,Refused) else 'unclassified_readonly_audit_error'
        receipt.update(status='audit_incomplete',failed_stage=stage,error_code=code,exception_class=type(exc).__name__)
    finally:
        receipt['ended_at']=datetime.now(timezone.utc).isoformat()
        files=[]
        for path in sorted(args.output.rglob('*')):
            if path.is_file():
                need(not path.is_symlink(),'output_symlink')
                raw=path.read_bytes();files.append({'path':path.relative_to(args.output).as_posix(),'bytes':len(raw),'sha256':sha(raw)})
        if sum(x['bytes'] for x in files)>UPLOAD_LIMIT-MIB:
            # Preserve the original oversized bytes only in the ephemeral runner;
            # publish a bounded diagnostic, never the oversized directory.
            quarantine=Path(tempfile.mkdtemp(prefix='followup-oversize-',dir=repo))/'original'
            args.output.rename(quarantine);args.output.mkdir()
            receipt.update(status='audit_incomplete',previous_error_code=receipt.get('error_code'),
                error_code='upload_budget',original_output_file_count=len(files),
                original_output_bytes=sum(x['bytes'] for x in files),
                original_outputs_preserved_in_runner_only=True)
            files=[]
        receipt['output_files']=files
        receipt['output_bytes_before_terminal_receipt']=sum(x['bytes'] for x in files)
        write_json(args.output/'run-final.json',receipt)
    return 0 if receipt['status']=='completed_with_stated_scope' else 1


if __name__=='__main__':
    raise SystemExit(main())
