"""Verify an immutable capture closure in an actually restored Git state commit."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from .state_storage import _expand, MAX_FILE_BYTES


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120)
    if p.returncode:
        raise ValueError("durability_git_object_unavailable")
    return p.stdout


def _time(value):
    t=datetime.fromisoformat(value.replace("Z","+00:00"))
    if t.tzinfo is None: raise ValueError("durability_timezone_required")
    return t


MAX_LOG_BYTES = 32 * 1024 * 1024
METHOD = "git_tree_closure_and_fixed_attempt_job_log_exact_saved_commit_v2"
_VERIFIED_REMOTE_CACHE = {}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api_json(url, token):
    request = Request(url, headers={"Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with build_opener(_NoRedirect()).open(request, timeout=25) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("github_service_response_too_large")
    return json.loads(raw), _sha(raw)


def _job_log(url, token):
    """Authenticate the REST endpoint only; never forward auth to signed URLs."""
    request = Request(url, headers={"Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=25)
    except HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location", "")
        parsed = urlparse(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("unsafe_github_log_redirect")
        # This new request deliberately has no authentication header. GitHub's
        # signed URL is an access capability; neither it nor credentials persist.
        response = urlopen(Request(location, headers={"Accept": "text/plain"}), timeout=25)
    with response:
        raw = response.read(MAX_LOG_BYTES + 1)
    if len(raw) > MAX_LOG_BYTES:
        raise ValueError("github_job_log_too_large")
    return raw


def _remote_completion(metadata, state_commit):
    repository=os.environ.get("GITHUB_REPOSITORY","")
    token=os.environ.get("GITHUB_TOKEN","")
    run_id=str(metadata.get("github_run_id",""))
    attempt = str(metadata.get("github_run_attempt", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repository) or not token or not run_id.isdigit() or not attempt.isdigit():
        return None
    # The immutable attempt endpoint matters: the latest run endpoint changes
    # when the owner reruns the same run ID.
    url="https://api.github.com/repos/"+repository+"/actions/runs/"+run_id+"/attempts/"+attempt
    try:
        value, run_hash = _api_json(url, token)
        if (value.get("status") not in ("in_progress", "completed") or value.get("head_sha") != metadata.get("source_sha")
                or str(value.get("run_attempt")) != attempt or str(value.get("id")) != run_id):
            return None
        jobs = []
        job_page_hashes = []
        for page in range(1, 21):
            body, page_hash = _api_json(url + "/jobs?per_page=100&page=" + str(page), token)
            batch = body.get("jobs")
            if not isinstance(batch, list):
                return None
            jobs.extend(batch); job_page_hashes.append(page_hash)
            if len(jobs) >= body.get("total_count", len(jobs)):
                break
        else:
            return None
        for job in jobs:
            if (str(job.get("run_id")) != run_id or job.get("status") != "completed"
                    or not job.get("completed_at") or (job.get("run_attempt") is not None
                    and str(job["run_attempt"]) != attempt)):
                continue
            job_id = str(job.get("id", ""))
            if not job_id.isdigit():
                continue
            log_url = "https://api.github.com/repos/" + repository + "/actions/jobs/" + job_id + "/logs"
            try:
                raw = _job_log(log_url, token)
            except Exception:
                continue
            pattern = re.compile(r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z )?MODEL_STATE_SAVED_COMMIT=([0-9a-f]{40})$")
            bound = [line for line in raw.decode("utf-8", errors="replace").splitlines()
                if (match := pattern.fullmatch(line)) and match.group(1) == state_commit]
            if not bound:
                continue
            result = {"repository": repository, "run_id": run_id, "head_sha": value["head_sha"],
                "run_attempt": int(attempt), "job_id": job_id, "job_name": job.get("name"),
                "state_commit": state_commit, "saved_commit_marker": "MODEL_STATE_SAVED_COMMIT=" + state_commit,
                "observed_completion_upper_bound": job["completed_at"], "source_url": url,
                "job_log_api_url": log_url, "run_response_sha256": run_hash,
                "job_list_response_sha256": job_page_hashes, "job_log_sha256": _sha(raw),
                "matched_log_lines": bound}
            result["evidence_sha256"] = _sha(_canonical(result))
            return result
    except Exception:
        return None
    return None


def _tree_file(repo, commit, name, archive=None):
    if archive is None:
        manifest=json.loads(_git(repo,"show",commit+":state_storage.json"))
        archive={e["path"]:e for e in manifest.get("entries",[])}
    if name in archive:
        entry=archive[name]
        raw=_git(repo,"show",commit+":state_archives/"+entry["blob_sha256"]+".json.gz")
        if len(raw)!=entry["compressed_bytes"] or _sha(raw)!=entry["blob_sha256"]:
            raise ValueError("archive_hash_mismatch")
        data=_expand(raw,MAX_FILE_BYTES)
        if len(data)!=entry["raw_bytes"] or _sha(data)!=entry["raw_sha256"]:
            raise ValueError("archive_raw_mismatch")
    else:
        data=_git(repo,"show",commit+":runtime/"+name)
    if len(data)>MAX_FILE_BYTES:
        raise ValueError("durable_blob_too_large")
    return data


def _proof_path(runtime, receipt_ref):
    name = receipt_ref.get("path", "")
    if Path(name).is_absolute() or ".." in Path(name).parts or len(Path(name).parts) < 6:
        raise ValueError("unsafe_durable_ref")
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt_ref.get("sha256", ""))):
        raise ValueError("invalid_durable_ref_hash")
    anchor = runtime / receipt_ref["path"]
    return anchor.parents[4] / "durability" / (receipt_ref["sha256"] + ".json")


def _stored_service_evidence(repo, runtime, proof_path, proof, metadata, state_commit):
    """Reuse a previously service-verified receipt already in restored Git state.

    Job logs expire. A freshly supplied JSON assertion cannot substitute for a
    remote check: the exact earlier verified proof must be present in the fixed
    state restored by cloud_state.load, whose marker is outside runtime.
    """
    try:
        if proof.get("verified") is not True or proof.get("method") != METHOD or proof.get("state_commit") != state_commit:
            return None
        evidence = proof["remote_run_completion"]
        if (evidence["state_commit"] != state_commit or evidence["head_sha"] != metadata["source_sha"]
                or str(evidence["run_id"]) != str(metadata["github_run_id"])
                or str(evidence["run_attempt"]) != str(metadata["github_run_attempt"])
                or evidence["saved_commit_marker"] != "MODEL_STATE_SAVED_COMMIT=" + state_commit
                or evidence["evidence_sha256"] != _sha(_canonical({k:v for k,v in evidence.items() if k != "evidence_sha256"}))):
            return None
        _time(proof["service_evidence_verified_at"])
        git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
        marker = json.loads((git_dir / "football-cloud-state.json").read_text())
        current = marker["state_oid"]
        if not re.fullmatch(r"[0-9a-f]{40}", current):
            return None
        raw = _tree_file(repo, current, proof_path.relative_to(runtime).as_posix())
        if raw != proof_path.read_bytes() or json.loads(raw) != proof:
            return None
        return evidence
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _same_bound_evidence(left, right):
    """Later jobs may change run/list JSON, but not this completed job binding."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    changing_envelope = {"run_response_sha256", "job_list_response_sha256", "evidence_sha256"}
    return {k:v for k,v in left.items() if k not in changing_envelope} == {
        k:v for k,v in right.items() if k not in changing_envelope}


def prove(repo, runtime, state_commit, receipt_ref, *, _completion_cache=None):
    """Read receipt and every hash-referenced parent from the fixed Git tree."""
    if not re.fullmatch(r"[0-9a-f]{40}",state_commit): raise ValueError("invalid_state_commit")
    repo, runtime=Path(repo),Path(runtime)
    try:
        _git(repo,"cat-file","-e",state_commit+"^{commit}")
    except ValueError:
        # The cloud checkout's existing origin authentication is used normally;
        # no credential material is inspected or echoed.
        _git(repo,"fetch","--no-tags","--depth=1","origin",state_commit)
    commit_at=_git(repo,"show","-s","--format=%cI",state_commit).decode().strip()
    metadata=json.loads(_git(repo,"show",state_commit+":state_metadata.json"))
    proof_path = _proof_path(runtime, receipt_ref)
    previous = json.loads(proof_path.read_text()) if proof_path.exists() else None
    cache = _completion_cache if _completion_cache is not None else _VERIFIED_REMOTE_CACHE
    cache_key = (os.environ.get("GITHUB_REPOSITORY"), metadata.get("github_run_id"),
        metadata.get("github_run_attempt"), metadata.get("source_sha"), state_commit)
    if cache_key in cache:
        completion = cache[cache_key]
    else:
        completion = _remote_completion(metadata, state_commit)
        # Completed-job evidence is immutable. Failures are cached only within
        # one stamp call, never permanently while an attempt is still running.
        if completion is not None or _completion_cache is not None:
            cache[cache_key] = completion
    if (completion is not None and previous and previous.get("verified") is True
            and previous.get("method") == METHOD
            and _same_bound_evidence(completion, previous.get("remote_run_completion"))
            and previous["remote_run_completion"].get("evidence_sha256") == _sha(_canonical({
                k:v for k,v in previous["remote_run_completion"].items() if k != "evidence_sha256"}))):
        # Keep the original observed response fingerprints/verification clock;
        # the immutable job+log+commit binding has just been checked again.
        completion = previous["remote_run_completion"]
    if completion is None and previous is not None:
        completion = _stored_service_evidence(repo, runtime, proof_path, previous, metadata, state_commit)
    manifest=json.loads(_git(repo,"show",state_commit+":state_storage.json"))
    entries=manifest.get("entries",[])
    archive={e["path"]:e for e in entries}
    if manifest.get("schema_version")!=1 or len(archive)!=len(entries):raise ValueError("durable_manifest_invalid")
    visited={}; values={}
    def read(ref):
        name=ref.get("runtime_relative_path", ref.get("path"))
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))):
            raise ValueError("invalid_durable_ref")
        if Path(name).is_absolute() or ".." in Path(name).parts: raise ValueError("unsafe_durable_ref")
        if name in visited:
            if visited[name]!=ref["sha256"]: raise ValueError("conflicting_durable_ref")
            return values[name]
        data = _tree_file(repo, state_commit, name, archive)
        if hashlib.sha256(data).hexdigest()!=ref["sha256"]: raise ValueError("durable_source_hash_mismatch")
        visited[name]=ref["sha256"]
        value=json.loads(data);values[name]=value
        def children(x):
            if isinstance(x,dict):
                if isinstance(x.get("runtime_relative_path", x.get("path")),str) and re.fullmatch(r"[0-9a-f]{64}",str(x.get("sha256",""))): read(x)
                for v in x.values():children(v)
            elif isinstance(x,list):
                for v in x:children(v)
        children(value)
        return value
    receipt=read(receipt_ref)
    if isinstance(receipt,dict) and "storage_completed_at" not in receipt and "path" in receipt:
        receipt=read(receipt)
    if _time(receipt["storage_completed_at"])>_time(commit_at):raise ValueError("state_commit_precedes_capture")
    saved_at=completion["observed_completion_upper_bound"] if completion else None
    if saved_at and _time(commit_at)>_time(saved_at):raise ValueError("state_commit_after_remote_run_completion")
    closure=hashlib.sha256(json.dumps(visited,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    verified_at = datetime.now(timezone.utc).isoformat() if completion else None
    if previous and completion and previous.get("remote_run_completion") == completion:
        verified_at = previous.get("service_evidence_verified_at") or verified_at
    return {"schema_version":"advanced-durability-v2","receipt_ref":receipt_ref,"state_commit":state_commit,
        "durable_saved_at":saved_at,"commit_clock_at":commit_at,"remote_run_completion":completion,
        "service_evidence_verified_at": verified_at,
        "closure_sha256":closure,"verified_file_count":len(visited),"closure_verified":True,
        "verified":bool(completion),"method":METHOD}


def stamp(repo, runtime, state_commit):
    repo,runtime=Path(repo),Path(runtime)
    reports=[]
    completion_cache={}
    for p in sorted((runtime/"advanced_snapshots").glob("*/captures/*/*/*/first.json")):
        ref={"path":p.relative_to(runtime).as_posix(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
        out=p.parents[4]/"durability"/(ref["sha256"]+".json")
        if out.exists() and json.loads(out.read_text()).get("verified") is True:continue
        proof=prove(repo,runtime,state_commit,ref,_completion_cache=completion_cache)
        out.parent.mkdir(parents=True,exist_ok=True)
        if out.exists():
            previous=out.with_name(out.stem+"-unverified.json")
            if not previous.exists():previous.write_bytes(out.read_bytes())
        out.write_text(json.dumps(proof,sort_keys=True,indent=2))
        reports.append(proof)
    return reports


def main():
    p=argparse.ArgumentParser();p.add_argument("--runtime",default="runtime");p.add_argument("--repo",default=".")
    p.add_argument("--state-commit",required=True);a=p.parse_args()
    proofs=stamp(a.repo,a.runtime,a.state_commit)
    print(json.dumps({"status":"completed_verification_attempt", "new_durable_closures":len(proofs),
        "verified":sum(p["verified"] for p in proofs), "unverified":sum(not p["verified"] for p in proofs)}))
    return 0

if __name__=="__main__":raise SystemExit(main())
