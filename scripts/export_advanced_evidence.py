"""Export bounded run receipts; immutable originals remain at fixed Git states.

This artifact is deliberately a thin inspection bundle, not a standalone model
or complete historical evidence closure. Single-match exports must resolve and
verify their own full reference closure with the normal model-access tooling.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_model.history_archive import read_bytes
from football_model.state_storage import _scan_secret


def export(runtime: Path, output: Path, *, repo: Path, league: str = "all",
           max_bytes: int = 64 * 1024 * 1024):
    runtime, repo = runtime.resolve(), repo.resolve()
    if max_bytes <= 0:
        raise ValueError("positive evidence byte limit required")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", league):
        raise ValueError("invalid league selection")
    gitdir = Path(subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"], text=True).strip())
    marker_path = gitdir / "football-cloud-state.json"
    marker = json.loads(marker_path.read_text()) if marker_path.is_file() else {}
    patterns = ["advanced_models/study_runs/latest.json",
                "advanced_features/collection/by_selection/*/latest.json",
                "feature_models/schedules/collection/latest.json",
                "advanced_scoring/*/latest.json"]
    selected = "*" if league == "all" else league
    patterns.extend([f"advanced_models/{selected}/*/reports/latest.json",
                     f"advanced_models/{selected}/*/reports/latest_attempt.json",
                     f"advanced_models/checkpoints/{selected}/*/latest.json"])
    candidates = {p for pattern in patterns for p in runtime.glob(pattern) if p.is_file()}
    candidates.update(p for p in repo.glob("*-run-*.json") if p.is_file())
    candidates.update(p for p in repo.glob("advanced-preflight*.json") if p.is_file())
    entries, bodies, total = [], [], 0
    for path in sorted(candidates):
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("evidence export refuses symlinks")
        if not path.is_relative_to(repo) or path.stat().st_size > max_bytes - total:
            raise ValueError("bounded run evidence limit exceeded; no complete bundle written")
        raw = read_bytes(path, runtime=runtime) if path.is_relative_to(runtime) else path.read_bytes()
        if total + len(raw) > max_bytes:
            raise ValueError("bounded run evidence limit exceeded; no complete bundle written")
        _scan_secret(raw)
        name = path.relative_to(repo).as_posix()
        entries.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        bodies.append((name, raw)); total += len(raw)
    catalogs = []
    for path in sorted((runtime / "history_archive").glob("**/*.json")):
        if path.is_symlink():
            raise ValueError("history catalog symlink rejected")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        catalogs.append({"path": path.relative_to(repo).as_posix(),
                         "sha256": digest, "bytes": path.stat().st_size,
                         "included": False, "read_from_fixed_saved_state": True})
    manifest = {"schema_version": "advanced-run-thin-evidence-v1", "bundle_kind": "thin_run_inspection",
                "self_contained_original_closure": False, "league_selection": league,
                "source_commit": os.environ.get("GITHUB_SHA"),
                "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "state_marker": {k: marker.get(k) for k in ("state_oid", "source_sha", "loaded_at")},
                "entries": entries, "catalogs": catalogs, "raw_included_bytes": total,
                "max_raw_included_bytes": max_bytes,
                "original_read_route": {"repository": "wuyan0231-hub/football-model", "state_branch": "model-state",
                    "instruction": "Fix state_oid above; read its transport manifest and history catalog, then verify compressed/raw hashes for each referenced original. This bundle alone does not contain those originals.",
                    "local_reader": "football_model.history_archive.read_state_bytes(repo, state_oid, runtime_relative_path)",
                    "access_guide": "docs/model-access.md"}}
    raw_manifest = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    _scan_secret(raw_manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            for name, raw in [*bodies, ("advanced-evidence-manifest.json", raw_manifest)]:
                info = tarfile.TarInfo(name); info.size = len(raw); info.mode = 0o600
                archive.addfile(info, io.BytesIO(raw))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    with output.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    output.with_suffix(".sha256").write_text(digest + "  " + output.name + "\n")
    return {"archive": str(output), "sha256": digest, "bytes": output.stat().st_size,
            "files": len(entries), "bundle_kind": manifest["bundle_kind"], "self_contained_original_closure": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("advanced-models-evidence.tar.gz"))
    parser.add_argument("--league", default="all")
    args = parser.parse_args()
    print(json.dumps(export(args.runtime, args.output, repo=args.repo, league=args.league), ensure_ascii=False))


if __name__ == "__main__":
    main()
