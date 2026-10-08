"""Restore selected, connector-verified archived evidence without fetching or training."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from football_model.state_storage import read_archive_bytes


def restore_selected(root, paths, output):
    root, output = Path(root), Path(output)
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise ValueError("archive root ancestry must not contain symlinks")
    root = root.resolve()
    if (output.exists() or output.is_symlink()
            or any(p.is_symlink() or (p.exists() and not p.is_dir()) for p in output.parents)):
        raise ValueError("archive access output exists or has unsafe ancestry")
    output = output.resolve()
    access = json.loads((root / "model_access_manifest.json").read_text())
    if access.get("status") != "files_materialized_not_model_verified" or not re.fullmatch(r"[0-9a-f]{40}", access.get("state_commit", "")):
        raise ValueError("verified connector materialization manifest required")
    records = {r["path"]: r for r in access["files"]}
    manifest_bytes = (root / "state_storage.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    wanted = list(paths)
    if not wanted or len(set(wanted)) != len(wanted):
        raise ValueError("selected logical paths must be nonempty and unique")
    # Validate the authoritative manifest and safely read each selected member
    # before using any of its fields as filesystem paths.
    bodies = {path: read_archive_bytes(root, path) for path in wanted}
    entries = {e["path"]: e for e in manifest["entries"]}
    dependencies = {"state_storage.json"}
    for path in wanted:
        dependencies.add("state_archives/" + entries[path]["blob_sha256"] + ".json.gz")
    for path in dependencies:
        record = records.get(path)
        actual = root / path
        if (not record or record.get("role") != "state" or record.get("commit") != access["state_commit"]
                or actual.is_symlink() or any(p.is_symlink() for p in actual.parents)
                or hashlib.sha256(actual.read_bytes()).hexdigest() != record.get("sha256")):
            raise ValueError("archive dependency differs from verified connector bytes")
    prepared = []
    for path in wanted:
        body = bodies[path]
        target = root / "runtime" / path
        if target.exists() or target.is_symlink() or any(p.is_symlink() for p in target.parents):
            raise ValueError("archive destination already exists or is symlinked")
        if output == target or output in target.parents or target in output.parents:
            raise ValueError("archive access output conflicts with restored evidence")
        prepared.append((path, target, body))
    output.parent.mkdir(parents=True, exist_ok=True)
    for _, target, body in prepared:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(body)
    result = {"schema_version": 1, "state_commit": access["state_commit"],
              "restored_at": datetime.now(timezone.utc).isoformat(),
              "status": "selected_archived_bytes_restored_not_full_state_or_model_verified",
              "files": [{"path": "runtime/" + path, "bytes": len(body),
                         "sha256": hashlib.sha256(body).hexdigest(),
                         "archive_blob_sha256": entries[path]["blob_sha256"]}
                        for path, _, body in prepared]}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--path", action="append", required=True,
                        help="Archived logical path relative to runtime, repeatable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = restore_selected(args.state_root, args.path, args.output)
    print(json.dumps({"status": result["status"], "restored_files": len(result["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
