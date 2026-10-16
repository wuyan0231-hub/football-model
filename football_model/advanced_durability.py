"""Verify an immutable capture closure in an actually restored Git state commit."""
from __future__ import annotations
import argparse
from collections import OrderedDict
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
from .state_storage import _expand, _manifest_json, _relative, MAX_FILE_BYTES, StorageError


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
METHOD = "git_tree_scoped_frozen_and_declared_ancestor_closure_exact_saved_commit_v4"
_VERIFIED_REMOTE_CACHE = {}
_ANCESTRY_CACHE = {}


class DurabilityReferenceError(ValueError):
    """A rejected closure reference, with safe fixed-tree failure coordinates."""

    def __init__(self, reason, *, state_commit, path, expected_sha256,
                 actual_sha256, reference_chain):
        super().__init__(reason)
        self.details = {"state_commit": state_commit, "path": path,
            "expected_sha256": expected_sha256, "actual_sha256": actual_sha256,
            "reference_chain": reference_chain}


def _recorded_replay_ref(name, ref):
    replay = re.fullmatch(r"replay/bundles/([0-9a-f]{64})\.json\.gz", name)
    return bool(replay and ref.get("status") == "recorded" and ref.get("sha256") == replay.group(1))


def _replay_reference_metadata(ref, value):
    payload = value["payload"]
    if (not isinstance(payload.get("events"), list) or not isinstance(payload.get("initial_state"), dict)
            or type(ref.get("events")) is not int or ref["events"] != len(payload["events"])
            or type(ref.get("initial_state_files")) is not int
            or ref["initial_state_files"] != len(payload["initial_state"])):
        raise ValueError("durable_replay_reference_metadata_mismatch")


def _reference_value(name, ref, data):
    """Respect the producer's replay payload digest; bind raw bytes separately.

    Production replay's Recorder.save returns the canonical *payload* digest,
    not the gzip file digest.  This is the only registered non-byte reference.
    A filename alone never exempts an arbitrary file from byte-hash checking.
    """
    from .production_replay import SCHEMA, _canonical as replay_canonical
    raw_digest = _sha(data)
    replay = re.fullmatch(r"replay/bundles/([0-9a-f]{64})\.json\.gz", name)
    typed = _recorded_replay_ref(name, ref)
    if not typed and raw_digest != ref["sha256"]:
        return None, "raw_bytes_sha256", raw_digest
    if replay is None:
        return json.loads(data), "raw_bytes_sha256", raw_digest
    # Decode even a raw-byte reference to a replay path only under the exact
    # producer contract.  One member, bounded expansion, no trailing data.
    try:
        value = _manifest_json(_expand(data, MAX_FILE_BYTES))
    except StorageError as exc:
        raise ValueError("durable_replay_encoding_invalid") from exc
    payload = value.get("payload") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"sha256", "payload"}
            or not isinstance(payload, dict) or payload.get("schema") != SCHEMA):
        raise ValueError("durable_replay_schema_mismatch")
    digest = _sha(replay_canonical(payload))
    if value["sha256"] != digest or replay.group(1) != digest:
        raise ValueError("durable_replay_payload_hash_mismatch")
    if typed:
        _replay_reference_metadata(ref, value)
        return value, "production_replay_canonical_payload_sha256", digest
    return value, "raw_bytes_sha256", raw_digest


def verify_reference_bytes(name, ref, data):
    """Validate fixed-original bytes under the exact producing reference contract.

    Callers still establish which fixed Git tree supplied ``data``. Returning
    both fingerprints never upgrades local bytes to a remote durability proof.
    """
    if (not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
            or not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", "")))):
        raise ValueError("invalid_durable_ref")
    if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
        raise ValueError("durable_blob_too_large_or_invalid")
    value, semantics, actual = _reference_value(name, ref, data)
    if actual != ref["sha256"]:
        raise ValueError("durable_source_hash_mismatch")
    return {"value": value, "reference_sha256": actual,
        "reference_hash_semantics": semantics, "raw_sha256": _sha(data)}


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
        try:
            data=_git(repo,"show",commit+":runtime/"+name)
        except ValueError as missing_direct:
            # A cold catalog changes location, never the referenced original.
            # Do not reinterpret archive corruption as an absent direct file.
            try:
                _git(repo, "show", commit + ":runtime/history_archive/catalog.json")
            except ValueError:
                raise missing_direct
            from .history_archive import read_state_bytes
            data = read_state_bytes(repo, commit, name)
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


def _safe_name(name):
    try:
        if any(ord(c) < 32 for c in name):
            raise ValueError("unsafe_durable_ref")
        return _relative(name)
    except (TypeError, StorageError) as exc:
        raise ValueError("unsafe_durable_ref") from exc


def _baseline_scope(name, value, raw_sha256, read_file):
    match = re.fullmatch(r"(advanced_snapshots/[^/]+)/baseline_payloads/[0-9a-f]{64}\.json", name)
    if not match or not isinstance(value, dict) or value.get("schema_version") != "feature-snapshots-1.0":
        return None
    marker = value.get("baseline_packet_reference_closure")
    scope = {"path": name, "sha256": raw_sha256, "routes": {}, "declared_state": value.get("state_commit"),
             "frozen": marker == "frozen_raw_inputs_v1"}
    if marker is not None and not scope["frozen"]:
        raise ValueError("unsupported_baseline_packet_reference_closure")
    if not scope["frozen"]:
        return scope
    if not isinstance(value.get("source_refs"), list):
        raise ValueError("invalid_baseline_packet_source_refs")
    for ref in value["source_refs"]:
        if not isinstance(ref, dict) or ref.get("kind") != "baseline_packet_input":
            continue
        origin = _safe_name(ref.get("origin_runtime_path"))
        target = _safe_name(ref.get("runtime_relative_path"))
        digest = ref.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid_baseline_packet_route_hash")
        ordinary = target == match.group(1) + "/sources/" + digest + ".json"
        replay = target == origin and re.fullmatch(r"replay/bundles/[0-9a-f]{64}\.json\.gz", target)
        if not (ordinary or replay):
            raise ValueError("unsafe_baseline_packet_route_target")
        # Register only independently verified raw bytes from this current
        # fixed tree. Producer-typed replay edges keep their logical digest.
        verify_reference_bytes(target, {"sha256": digest}, read_file(target))
        pair = (origin, digest)
        if pair in scope["routes"] and scope["routes"][pair] != target:
            raise ValueError("ambiguous_baseline_packet_route")
        scope["routes"][pair] = target
    return scope


def _declared_ancestor(repo, source, current):
    """Prove only the explicitly declared source, never search for a hash."""
    if not re.fullmatch(r"[0-9a-f]{40}", str(source)):
        raise ValueError("invalid_declared_baseline_state")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    key = (str(Path(repo).resolve()), repository, source, current)
    if key in _ANCESTRY_CACHE:
        return _ANCESTRY_CACHE[key]
    result = None
    try:
        _git(repo, "merge-base", "--is-ancestor", source, current)
        result = {"method": "local_git_is_ancestor", "source_commit": source, "current_commit": current}
    except ValueError:
        parents = _git(repo, "show", "-s", "--format=%P", current)
        if source in parents.decode().strip().split():
            result = {"method": "local_git_direct_parent", "source_commit": source,
                      "current_commit": current, "parent_header_sha256": _sha(parents)}
    if result is None:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("declared_baseline_ancestry_unverified")
        url = "https://api.github.com/repos/" + repository + "/compare/" + source + "..." + current
        # Files are only returned on compare page one. Page two retains the
        # whole-comparison ancestry metadata without a potentially huge diff.
        body, first_sha = _api_json(url + "?per_page=1&page=2", token)
        def valid(value):
            return (isinstance(value, dict) and value.get("status") == "ahead"
                and value.get("base_commit", {}).get("sha") == source
                and value.get("merge_base_commit", {}).get("sha") == source
                and value.get("behind_by") == 0 and type(value.get("total_commits")) is int
                and 0 < value["total_commits"] <= 100000 and value.get("ahead_by") == value["total_commits"])
        if not valid(body):
            raise ValueError("declared_baseline_ancestry_unverified")
        head_url = "https://api.github.com/repos/" + repository + "/git/commits/" + current
        head_body, head_sha = _api_json(head_url, token)
        if not isinstance(head_body, dict) or head_body.get("sha") != current:
            raise ValueError("declared_baseline_compare_head_unverified")
        result = {"method": "github_exact_compare_base_head_mergebase", "source_commit": source,
            "current_commit": current, "source_url": url, "ahead_by": body["ahead_by"],
            "compare_metadata_page": 2, "compare_response_sha256": first_sha,
            "head_commit_url": head_url, "head_commit_response_sha256": head_sha}
    _ANCESTRY_CACHE[key] = result
    return result


class _LegacyResolver:
    """Small shared cache of exact declared historical summary originals."""
    def __init__(self, repo, state_commit):
        self.repo, self.state_commit = Path(repo), state_commit
        self.readers, self.resolved = OrderedDict(), {}

    def resolve(self, scope, name, ref):
        # Only mutable comparison summaries/indexes have this compatibility
        # route. Corruption of immutable models, sources, or replay objects is
        # never repaired by looking at an older state.
        if scope["frozen"] or not (re.fullmatch(r"(?:[^/]+/)*reports/(?:latest|score-latest)\.json", name)
                or name in {"integration/index.json", "integration/f5c-index.json"}):
            raise ValueError("no_registered_declared_state_route")
        source = scope["declared_state"]
        ancestry = _declared_ancestor(self.repo, source, self.state_commit)
        key = (source, name, ref["sha256"])
        if key not in self.resolved:
            from . import history_archive as history
            if source not in self.readers:
                if len(self.readers) >= 2:
                    _, expired = self.readers.popitem(last=False)
                    expired.close()
                self.readers[source] = history.FixedStateReader(self.repo, source)
            reader = self.readers[source]
            self.readers.move_to_end(source)
            manifest = reader.validate_manifest()
            raw = reader.read(name)
            checked = verify_reference_bytes(name, ref, raw)
            _, entries = history._manifest_index(self.repo, source)
            entry = entries.get(name)
            transport = "state_archives/" + entry["blob_sha256"] + ".json.gz" if entry else "runtime/" + name
            blob = reader.root_bytes(transport)
            if entry and (len(blob) != entry["compressed_bytes"] or _sha(blob) != entry["blob_sha256"]):
                raise ValueError("declared_baseline_transport_hash_mismatch")
            git_blob = _git(self.repo, "rev-parse", source + ":" + transport).decode().strip()
            if git_blob != hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest():
                raise ValueError("declared_baseline_git_blob_mismatch")
            self.resolved[key] = (raw, {"source_commit": source, "source_path": name,
                "transport_path": transport, "transport_git_blob": git_blob,
                "source_manifest_sha256": manifest["sha256"], "transport_sha256": _sha(blob),
                "transport_bytes": len(blob), "raw_sha256": checked["raw_sha256"], "raw_bytes": len(raw),
                "ancestry": ancestry})
        return self.resolved[key]

    def close(self):
        for reader in self.readers.values():
            reader.close()
        self.readers.clear()


def _stable_reference_bindings(bindings):
    # Verification method, HTTP envelopes, and clocks are observations. The
    # exact ancestry endpoints and every source/byte/path binding are identity.
    stable = json.loads(json.dumps(bindings))
    for binding in stable:
        historical = binding.get("historical_source")
        if historical is not None:
            ancestry = historical["ancestry"]
            historical["ancestry"] = {k: ancestry[k] for k in ("source_commit", "current_commit")}
    return stable


def prove(repo, runtime, state_commit, receipt_ref, *, _completion_cache=None, _read_file=None,
          _legacy_resolver=None):
    """Read receipt and every hash-referenced parent from the fixed Git tree."""
    if not re.fullmatch(r"[0-9a-f]{40}",state_commit): raise ValueError("invalid_state_commit")
    if _read_file is None:
        from .history_archive import FixedStateReader
        with FixedStateReader(repo, state_commit) as reader:
            reader.validate_manifest()
            return prove(repo, runtime, state_commit, receipt_ref,
                _completion_cache=_completion_cache, _read_file=reader.read,
                _legacy_resolver=_legacy_resolver)
    repo, runtime=Path(repo),Path(runtime)
    if _legacy_resolver is None:
        resolver = _LegacyResolver(repo, state_commit)
        try:
            return prove(repo, runtime, state_commit, receipt_ref,
                _completion_cache=_completion_cache, _read_file=_read_file, _legacy_resolver=resolver)
        finally:
            resolver.close()
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
    visited, raw_hashes, data_cache, checked_cache = {}, {}, {}, {}
    walked, bindings = set(), []
    def read(ref, chain=(), scope=None):
        if not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))):
            raise ValueError("invalid_durable_ref")
        name = _safe_name(ref.get("runtime_relative_path", ref.get("path")))
        target = scope["routes"].get((name, ref["sha256"]), name) if scope else name
        identity, historical = target, None
        if target not in data_cache:
            data_cache[target] = _read_file(target)
        data = data_cache[target]
        check_key = (identity, ref["sha256"], _recorded_replay_ref(name, ref))
        if check_key not in checked_cache:
            checked_cache[check_key] = _reference_value(name, ref, data)
        value, semantics, actual = checked_cache[check_key]
        if actual != ref["sha256"] and scope is not None and not scope["frozen"]:
            try:
                data, historical = _legacy_resolver.resolve(scope, name, ref)
                value, semantics, actual = _reference_value(name, ref, data)
                identity = "git:" + historical["source_commit"] + ":" + name
            except (ValueError, StorageError, OSError) as exc:
                rejected = DurabilityReferenceError("durable_source_hash_mismatch",
                    state_commit=state_commit, path=name, expected_sha256=ref["sha256"],
                    actual_sha256=actual, reference_chain=list(chain))
                rejected.details["declared_state_resolution_error"] = str(exc)
                rejected.details["declared_state_commit"] = scope["declared_state"]
                raise rejected from exc
        if actual != ref["sha256"]:
            raise DurabilityReferenceError("durable_source_hash_mismatch",
                state_commit=state_commit, path=name, expected_sha256=ref["sha256"],
                actual_sha256=actual, reference_chain=list(chain))
        if _recorded_replay_ref(name, ref):
            _replay_reference_metadata(ref, value)
        raw_digest = _sha(data)
        visited.setdefault(identity, {})[semantics] = ref["sha256"]
        raw_hashes[identity] = raw_digest
        if scope and (target != name or historical is not None):
            binding = {"baseline_payload_path": scope["path"], "baseline_payload_sha256": scope["sha256"],
                "reference_chain": list(chain), "origin_runtime_path": name,
                "expected_sha256": ref["sha256"], "resolved_path": target,
                "resolved_state_commit": state_commit, "raw_sha256": raw_digest}
            if historical is not None:
                binding.update({"resolution": "declared_ancestor_original", "resolved_path": name,
                    "resolved_state_commit": historical["source_commit"], "historical_source": historical})
            else:
                binding["resolution"] = "scoped_frozen_raw_input"
            bindings.append(binding)
        child_scope = _baseline_scope(target, value, raw_digest, _read_file) or scope
        scope_id = (child_scope["path"], child_scope["sha256"]) if child_scope else None
        context = (identity, scope_id)
        if context in walked:
            return value
        walked.add(context)
        if len(walked) > 100000:
            raise ValueError("durable_reference_closure_too_large")
        def children(x, pointer=""):
            if isinstance(x, dict):
                if isinstance(x.get("runtime_relative_path", x.get("path")), str) and re.fullmatch(r"[0-9a-f]{64}", str(x.get("sha256", ""))):
                    edge = {"source_path": target, "json_pointer": pointer,
                        "path": x.get("runtime_relative_path", x.get("path")),
                        "expected_sha256": x["sha256"]}
                    read(x, (*chain, edge), child_scope)
                for key, v in x.items():
                    escaped = str(key).replace("~", "~0").replace("/", "~1")
                    children(v, pointer + "/" + escaped)
            elif isinstance(x, list):
                for index, v in enumerate(x):
                    children(v, pointer + "/" + str(index))
        children(value)
        return value
    receipt = read(receipt_ref)
    if isinstance(receipt, dict) and "storage_completed_at" not in receipt and "path" in receipt:
        receipt = read(receipt)
    if _time(receipt["storage_completed_at"])>_time(commit_at):raise ValueError("state_commit_precedes_capture")
    saved_at=completion["observed_completion_upper_bound"] if completion else None
    if saved_at and _time(commit_at)>_time(saved_at):raise ValueError("state_commit_after_remote_run_completion")
    hash_semantics = {name: sorted(identities) for name, identities in visited.items()}
    # HTTP observation envelopes and verification clocks are evidence, not
    # the identity of an original. Rechecking the same fixed bytes keeps the
    # substantive closure stable even when GitHub returns a changed envelope.
    stable_bindings = _stable_reference_bindings(bindings)
    closure=hashlib.sha256(_canonical({"references": visited, "raw_sha256": raw_hashes,
        "reference_hash_semantics": hash_semantics, "resolved_reference_bindings": stable_bindings})).hexdigest()
    # The independent checks above establish these same immutable facts again.
    # Retain a matching prior observation envelope so an API->local ancestry
    # route, or a changed HTTP envelope, cannot break exact proof recomputation.
    try:
        if (previous and previous.get("verified") is True and previous.get("closure_verified") is True
                and previous.get("method") == METHOD and previous.get("state_commit") == state_commit
                and previous.get("receipt_ref") == receipt_ref and previous.get("closure_sha256") == closure
                and _stable_reference_bindings(previous["resolved_reference_bindings"]) == stable_bindings):
            bindings = previous["resolved_reference_bindings"]
    except (KeyError, TypeError, ValueError):
        pass
    verified_at = datetime.now(timezone.utc).isoformat() if completion else None
    if previous and completion and previous.get("remote_run_completion") == completion:
        verified_at = previous.get("service_evidence_verified_at") or verified_at
    typed_names = {name for name, identities in hash_semantics.items()
        if any(kind != "raw_bytes_sha256" for kind in identities)}
    return {"schema_version":"advanced-durability-v4","receipt_ref":receipt_ref,"state_commit":state_commit,
        "durable_saved_at":saved_at,"commit_clock_at":commit_at,"remote_run_completion":completion,
        "service_evidence_verified_at": verified_at,
        "closure_sha256":closure,"verified_file_count":len(visited),"closure_verified":True,
        "resolved_reference_bindings": bindings,
        "historical_resolution_count": sum(b["resolution"] == "declared_ancestor_original" for b in bindings),
        # Ordinary references already bind raw SHA in the closure. Only the
        # producer-specific exceptions need both readable fingerprints here.
        "reference_hash_semantics":{name:hash_semantics[name] for name in sorted(typed_names)},
        "raw_sha256":{name:raw_hashes[name] for name in sorted(typed_names)},
        "verified":bool(completion),"method":METHOD}


def _retained_saved_proof(repo, runtime, state_commit, receipt_ref, path, read_file):
    """Keep an authentic earlier proof's service clock, never a local assertion.

    The already-restored fixed tree authenticates the complete earlier v4
    proof. Its exact saved-job evidence must still pass the stored-evidence
    checks, and its source must be an actual ancestor of this current state.
    This reuses a previously verified closure; it does not claim a new proof
    at the old capture time or accept an unsaved local proof.
    """
    try:
        if not path.is_file():
            return None
        raw = path.read_bytes()
        proof = json.loads(raw)
        if (proof.get("schema_version") != "advanced-durability-v4" or proof.get("method") != METHOD
                or proof.get("verified") is not True or proof.get("closure_verified") is not True
                or proof.get("receipt_ref") != receipt_ref
                or not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("closure_sha256", "")))
                or not re.fullmatch(r"[0-9a-f]{40}", str(proof.get("state_commit", "")))
                or read_file(path.relative_to(runtime).as_posix()) != raw):
            return None
        first_name = _safe_name(receipt_ref["path"])
        first_raw = read_file(first_name)
        if _sha(first_raw) != receipt_ref["sha256"] or (runtime / first_name).read_bytes() != first_raw:
            return None
        source = proof["state_commit"]
        _declared_ancestor(repo, source, state_commit)
        from .history_archive import FixedStateReader
        with FixedStateReader(repo, source) as reader:
            metadata = json.loads(reader.root_bytes("state_metadata.json"))
        evidence = _stored_service_evidence(repo, runtime, path, proof, metadata, source)
        if evidence is None or proof.get("durable_saved_at") != evidence["observed_completion_upper_bound"]:
            return None
        _time(proof["durable_saved_at"])
        _time(proof["commit_clock_at"])
        if _time(proof["commit_clock_at"]) > _time(proof["durable_saved_at"]):
            return None
        return {**proof, "verification_status": "retained_fixed_tree_service_verified_proof"}
    except (ValueError, StorageError, OSError, KeyError, TypeError):
        return None


def stamp(repo, runtime, state_commit):
    """Recheck every immutable first independently; preserve rejected attempts."""
    from .history_archive import FixedStateReader
    repo, runtime = Path(repo), Path(runtime)
    reports, completion_cache, pending = [], {}, []
    for p in sorted((runtime / "advanced_snapshots").glob("*/captures/*/*/*/first.json")):
        ref = {"path": p.relative_to(runtime).as_posix(), "sha256": _sha(p.read_bytes())}
        pending.append((ref, _proof_path(runtime, ref)))
    if not pending:
        return reports
    resolver = _LegacyResolver(repo, state_commit)
    try:
        with FixedStateReader(repo, state_commit) as reader:
            reader.validate_manifest()
            for ref, out in pending:
                try:
                    retained = _retained_saved_proof(repo, runtime, state_commit, ref, out, reader.read)
                    if retained is not None:
                        reports.append(retained)
                        continue
                    proof = prove(repo, runtime, state_commit, ref, _completion_cache=completion_cache,
                        _read_file=reader.read, _legacy_resolver=resolver)
                except (ValueError, StorageError, OSError, KeyError, TypeError) as exc:
                    failure = {"schema_version": "advanced-durability-v4", "method": METHOD,
                        "status": "failed_verification_attempt", "verified": False, "closure_verified": False,
                        "receipt_ref": ref, "state_commit": state_commit, "error": str(exc),
                        "attempted_at": datetime.now(timezone.utc).isoformat()}
                    if isinstance(exc, DurabilityReferenceError):
                        failure["failure"] = exc.details
                    raw = json.dumps(failure, sort_keys=True, indent=2).encode()
                    failures = out.parent / "failures"
                    failures.mkdir(parents=True, exist_ok=True)
                    path = failures / (ref["sha256"] + "-" + _sha(raw) + ".json")
                    # Each rejected attempt is append-only. A valid prior proof
                    # and the immutable capture are never overwritten by it.
                    with path.open("xb") as handle:
                        handle.write(raw)
                    failure["failure_report_path"] = path.relative_to(runtime).as_posix()
                    reports.append(failure)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                raw = json.dumps(proof, sort_keys=True, indent=2).encode()
                if out.exists() and out.read_bytes() != raw:
                    old_raw = out.read_bytes()
                    previous = out.with_name(out.stem + "-previous-" + _sha(old_raw) + ".json")
                    if not previous.exists():
                        previous.write_bytes(old_raw)
                out.write_bytes(raw)
                reports.append(proof)
    finally:
        resolver.close()
    return reports

def main():
    p=argparse.ArgumentParser();p.add_argument("--runtime",default="runtime");p.add_argument("--repo",default=".")
    p.add_argument("--state-commit",required=True);a=p.parse_args()
    try:
        proofs=stamp(a.repo,a.runtime,a.state_commit)
    except DurabilityReferenceError as exc:
        print(json.dumps({"status": "failed_verification_attempt", "error": str(exc),
            "failure": exc.details}, sort_keys=True))
        return 1
    failed = sum(p.get("closure_verified") is False for p in proofs)
    verified = sum(p.get("verified") is True for p in proofs)
    closures = sum(p.get("closure_verified") is True for p in proofs)
    retained = sum(p.get("verification_status") == "retained_fixed_tree_service_verified_proof" for p in proofs)
    print(json.dumps({"status": "partial_verification_attempt" if failed else "completed_verification_attempt",
        "new_durable_closures": verified - retained, "retained_saved_closures": retained,
        "verified_reference_closures": closures, "verified": verified,
        "unverified": closures - verified, "failed": failed, "attempted": len(proofs)}))
    return 1 if failed else 0

if __name__=="__main__":raise SystemExit(main())
