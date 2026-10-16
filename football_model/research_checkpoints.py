"""Resume development only from a verified, remotely saved research closure.

A calculation receipt deliberately does not claim its own future save. The
restored Git state, not a mutable JSON assertion, proves durability on resume.
This engineering checkpoint never confers prospective/confirmation eligibility.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .history_archive import read_bytes, iter_paths, read_state_bytes, FixedStateReader
from .advanced_storage import load_artifact
from .advanced_durability import verify_reference_bytes
from .model import RESULT_AVAILABILITY_DELAY, _timestamp


VERSION = "advanced-research-checkpoint-v1"


class CheckpointError(ValueError):
    """A checkpoint cannot be saved or safely reused."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120)
    if result.returncode:
        raise CheckpointError("checkpoint_git_read_failed")
    return result.stdout.decode().strip()


def _ref(runtime, path):
    relative = Path(path).resolve().relative_to(runtime).as_posix()
    return {"path": relative, "sha256": hashlib.sha256(read_bytes(path, runtime=runtime)).hexdigest()}


def input_inventory(runtime):
    """Hash actual logical source bytes once, independent of hot/cold layout.

    Include additions as well as existing selected sources. Otherwise a newly
    available lineup/statistic could silently reuse a prior missing-data result.
    The shared state lock prevents source collection during this study command.
    """
    runtime = Path(runtime).resolve()
    prefixes = ("fundamentals/", "advanced_features/evidence/", "advanced_features/contexts/",
        "advanced_features/snapshots/", "advanced_features/player_load/", "feature_models/schedules/")
    entries = {}
    for path in iter_paths(runtime):
        relative = path.relative_to(runtime).as_posix()
        # Collection reports and mutable continuation ledgers do not feed
        # numeric features; their changes must not manufacture new inputs.
        if relative.startswith(prefixes) and "/collection/" not in relative and path.suffix == ".json":
            entries[relative] = hashlib.sha256(read_bytes(path, runtime=runtime)).hexdigest()
    return entries


def input_identity(runtime, league, protocol_hash, implementation, run_goalmodel, inventory, now):
    runtime = Path(runtime).resolve()
    history = read_bytes(runtime / "data" / (league + ".json"), runtime=runtime)
    value = json.loads(history)
    mature = sorted(m["match_id"] for m in value["matches"]
        if _timestamp(m["kickoff"], "kickoff") + RESULT_AVAILABILITY_DELAY <= now)
    sources = {p: sha for p, sha in inventory.items()
        if not p.startswith("feature_models/schedules/")
        or p.startswith("feature_models/schedules/" + league + "/")}
    return {"league": league, "protocol_hash": protocol_hash,
        "implementation_sha256": implementation["sha256"], "goalmodel_requested": bool(run_goalmodel),
        "history_sha256": hashlib.sha256(history).hexdigest(),
        "source_inventory_sha256": _digest(sources), "mature_target_ids_sha256": _digest(mature)}


def _references(value):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
            # Preserve the registered producer contract (notably replay
            # status/events/initial_state_files); its digest need not be the
            # raw gzip digest. Never infer that exception from a filename.
            yield {**value, "path": value.get("runtime_relative_path", value["path"])}
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


class ResearchCheckpoints:
    def __init__(self, runtime, repo, source_commit, *, guard, clock=None):
        self.runtime, self.repo = Path(runtime).resolve(), Path(repo).resolve()
        self.source_commit, self.guard = source_commit, guard
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Explicit invocation is insufficient on an engineering branch. Only
        # the existing main release job may exercise its existing CAS save.
        if (os.environ.get("GITHUB_REF") != "refs/heads/main"
                or os.environ.get("GITHUB_SHA") != source_commit
                or _git(self.repo, "rev-parse", "HEAD") != source_commit):
            raise CheckpointError("checkpoint_requires_matching_main_release")
        self._marker()  # load must already have succeeded; never create a marker.
        self.inventory = input_inventory(self.runtime)

    def _marker(self):
        git_dir = Path(_git(self.repo, "rev-parse", "--absolute-git-dir"))
        marker = json.loads((git_dir / "football-cloud-state.json").read_text())
        if (marker.get("schema_version") != 1 or marker.get("source_sha") != self.source_commit
                or not re.fullmatch(r"[0-9a-f]{40,64}", str(marker.get("state_oid", "")))):
            raise CheckpointError("checkpoint_restored_state_marker_invalid")
        return marker

    def identity(self, league, protocol_hash, implementation, run_goalmodel):
        return input_identity(self.runtime, league, protocol_hash, implementation,
            run_goalmodel, self.inventory, self.clock())

    def _read_saved(self, commit, reference, reader=None):
        relative = reference["path"]
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))):
            raise CheckpointError("checkpoint_reference_invalid")
        path = (self.runtime / relative).resolve()
        if not path.is_relative_to(self.runtime):
            raise CheckpointError("checkpoint_reference_escapes_runtime")
        local = read_bytes(path, runtime=self.runtime)
        raw = reader.read(relative) if reader is not None else read_state_bytes(self.repo, commit, relative)
        if raw != local:
            raise CheckpointError("checkpoint_not_in_restored_state")
        verify_reference_bytes(relative, reference, raw)
        return raw

    def resume(self, league, protocol_hash, identity, max_age_days, *, allow_saved_snapshot=False):
        """Return a saved result only after exact Git closure verification."""
        pointer = self.runtime / "advanced_models" / "checkpoints" / league / protocol_hash / "latest.json"
        if not pointer.is_file():
            return None, "checkpoint_unavailable"
        reader = None
        try:
            commit = self._marker()["state_oid"]
            reader = FixedStateReader(self.repo, commit)
            head = json.loads(self._read_saved(commit, _ref(self.runtime, pointer), reader))
            reference = head["checkpoint_ref"]
            checkpoint = json.loads(self._read_saved(commit, reference, reader))
            saved_identity = checkpoint.get("input_identity", {})
            same_inputs = saved_identity == identity
            compatible = all(saved_identity.get(key) == identity.get(key) for key in
                ("league", "protocol_hash", "implementation_sha256", "goalmodel_requested"))
            if (checkpoint.get("schema_version") != VERSION or not compatible
                    or (not same_inputs and not allow_saved_snapshot)):
                return None, "checkpoint_inputs_protocol_or_implementation_changed"
            if checkpoint.get("calculation_completed") is not True:
                raise CheckpointError("checkpoint_calculation_incomplete")
            generated = _timestamp(checkpoint["calculation_generated_at"], "checkpoint_generated_at")
            now = self.clock()
            fresh = generated <= now and now - generated <= timedelta(days=max_age_days)
            if generated > now or (not fresh and not allow_saved_snapshot):
                return None, "checkpoint_stale_or_from_future"
            # Verify every actual compact object and every nested raw source,
            # card and training reference, not merely the top-level report.
            pending, seen = list(_references(checkpoint)), set()
            while pending:
                item = pending.pop()
                # Deduplicate identical assertions only. Two references to
                # the same replay bytes can make different event-count claims;
                # verifying one must never silently validate the other.
                key = _digest(item)
                if key in seen:
                    continue
                seen.add(key)
                raw = self._read_saved(commit, item, reader)
                decoded = verify_reference_bytes(item["path"], item, raw)
                pending.extend(_references(decoded["value"]))
            report = load_artifact(self.runtime, checkpoint["report_ref"])
            if (report.get("protocol_hash") != protocol_hash or report.get("league") != league
                    or report.get("implementation_fingerprint", {}).get("sha256") != identity["implementation_sha256"]
                    or report.get("goalmodel_requested") != identity["goalmodel_requested"]):
                raise CheckpointError("checkpoint_report_identity_mismatch")
            current_reusable = same_inputs and fresh
            return {**checkpoint["result"],
                "status": "completed_development" if current_reusable else "verified_saved_development_snapshot",
                "calculation_reused": current_reusable, "saved_snapshot_verified": True,
                "snapshot_input_identity": saved_identity,
                "current_inputs_match": same_inputs, "current_model_fresh": fresh,
                "current_refit_required": not current_reusable,
                "durable_saved": True, "durability_status": "verified_saved_git_closure",
                "durable_state_commit": commit,
                "checkpoint_ref": reference, "checkpoint_closure_files_verified": len(seen)}, None
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired):
            return None, "checkpoint_saved_closure_verification_failed"
        finally:
            if reader is not None:
                reader.close()

    def persist(self, league, protocol_hash, identity, report, result, write):
        """Stage immutable receipt, preflight, and use the existing CAS save."""
        from scripts import cloud_state
        directory = self.runtime / "advanced_models" / league / protocol_hash / "reports"
        summary = json.loads(read_bytes(directory / "latest.json", runtime=self.runtime))
        reference = summary["full_report_ref"]
        if load_artifact(self.runtime, reference) != report:
            raise CheckpointError("checkpoint_report_does_not_match_saved_output")
        checkpoint = {"schema_version": VERSION, "league": league,
            "source_commit": self.source_commit, "input_identity": identity,
            "calculation_completed": True, "calculation_generated_at": report["generated_at"],
            "report_ref": reference, "result": result,
            "durability": "requires_saved_git_closure_verification",
            "prospective_qualified": False, "production_changed": False}
        base = self.runtime / "advanced_models" / "checkpoints" / league / protocol_hash
        path = base / "history" / (_digest(checkpoint) + ".json")
        if not path.exists():
            write(path, checkpoint, self.guard, final=True)
        elif json.loads(path.read_text()) != checkpoint:
            raise CheckpointError("checkpoint_immutable_collision")
        ref = _ref(self.runtime, path)
        write(base / "latest.json", {"checkpoint_ref": ref}, self.guard, final=True)
        cloud_state.prepare_state(self.repo, self.runtime)
        self.guard.refresh()
        preflight = cloud_state.preflight_state(self.repo, self.runtime)
        if preflight.get("can_save") is not True:
            return {"durable_saved": False, "checkpoint_ref": ref,
                "checkpoint_status": "resource_deferred", "checkpoint_preflight": preflight}
        commit = cloud_state.save_state(self.repo, self.runtime)
        # Same explicit marker consumed by existing remote durability checks;
        # no new remote writer, force-push, credential or concurrency mechanism.
        print("MODEL_STATE_SAVED_COMMIT=" + commit, flush=True)
        # The just-saved league's new objects were not eligible for eviction
        # before that save. Compact them now, before the next expensive fit,
        # while retaining the exact committed bytes in immutable Git history.
        saved = {"durable_saved": True, "durable_state_commit": commit,
            "checkpoint_ref": ref, "checkpoint_status": "saved",
            "checkpoint_preflight": preflight}
        try:
            cloud_state.prepare_state(self.repo, self.runtime)
            self.guard.refresh()
        except (OSError, ValueError, RuntimeError) as exc:
            # A later cleanup failure cannot erase an already successful CAS
            # save. Stop further fitting, but retain that durable fact.
            saved.update(checkpoint_status="saved_postprocess_failed", continue_after_save=False,
                         post_save_error_code=type(exc).__name__)
        return saved
