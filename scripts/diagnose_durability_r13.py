"""Read-only replay of the original durability traversal on one fixed Git tree.

No runtime restoration, API access, fitting, state writes, or authentication.
The output is diagnostic evidence only; it is never a qualifying durability proof.
Run from the source checkout with PYTHONPATH=. and a fetched state commit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re

from football_model.advanced_durability import _git, _tree_file


FIRST = re.compile(r"advanced_snapshots/[^/]+/captures/[^/]+/[^/]+/[^/]+/first\.json$")
HASH = re.compile(r"[0-9a-f]{64}$")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    p.add_argument("--state-commit", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--max-evidence-bytes", type=int, default=256 * 1024 * 1024)
    args = p.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.state_commit):
        raise ValueError("full_fixed_state_commit_required")
    if not 1024 * 1024 <= args.max_evidence_bytes <= 256 * 1024 * 1024:
        raise ValueError("diagnostic_budget_out_of_range")
    repo = Path(args.repo).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("new_diagnostic_output_directory_required")
    output.mkdir(parents=True)
    rawdir = output / "objects"
    rawdir.mkdir()
    report = {"schema_version": "durability-read-only-diagnostic-v1", "qualifying_proof": False,
        "state_commit": args.state_commit, "observed_at": datetime.now(timezone.utc).isoformat(),
        "network_used": False, "runtime_restored": False, "state_modified": False,
        "traversal": "original_stamp_sorted_first_then_unfiltered_children", "anchors": [],
        "objects": [], "edges": [], "first_failure": None, "evidence_bytes": 0,
        "omitted_evidence": []}
    # This fixed manifest is the only large index parsed; no state is expanded.
    manifest_raw = _git(repo, "show", args.state_commit + ":state_storage.json")
    manifest = json.loads(manifest_raw)
    entries = manifest.get("entries", [])
    archive = {e["path"]: e for e in entries}
    if manifest.get("schema_version") != 1 or len(archive) != len(entries):
        raise ValueError("durable_manifest_invalid")
    manifest_gz = gzip.compress(manifest_raw, mtime=0)
    if len(manifest_gz) > args.max_evidence_bytes // 2:
        raise ValueError("manifest_exceeds_diagnostic_budget")
    (output / "state_storage.json.gz").write_bytes(manifest_gz)
    report["evidence_bytes"] += len(manifest_gz)
    report["manifest_sha256"] = sha(manifest_raw)
    report["manifest_raw_bytes"] = len(manifest_raw)
    report["manifest_gzip_sha256"] = sha(manifest_gz)
    for name in ("state_metadata.json",):
        raw = _git(repo, "show", args.state_commit + ":" + name)
        (output / name).write_bytes(raw)
        report["evidence_bytes"] += len(raw)
    names_raw = _git(repo, "ls-tree", "-r", "--name-only", args.state_commit, "--", "runtime/")
    direct = {n.removeprefix("runtime/") for n in names_raw.decode().splitlines()}
    names = direct | set(archive)
    anchors = sorted(name for name in names if FIRST.fullmatch(name))
    report["first_anchor_count"] = len(anchors)
    saved = {}

    def preserve(name, raw, *, critical=False):
        digest = sha(raw)
        if digest in saved:
            return saved[digest]
        compressed = gzip.compress(raw, mtime=0)
        # Reserve one maximum-size raw object for the actual failing bytes.
        reserve = 1024 * 1024 if critical else 61 * 1024 * 1024
        if report["evidence_bytes"] + len(raw) + reserve <= args.max_evidence_bytes:
            relative = "objects/" + digest + ".json"
            (output / relative).write_bytes(raw)
            report["evidence_bytes"] += len(raw)
        elif report["evidence_bytes"] + len(compressed) + reserve <= args.max_evidence_bytes:
            relative = "objects/" + digest + ".json.gz"
            (output / relative).write_bytes(compressed)
            report["evidence_bytes"] += len(compressed)
        else:
            relative = None
            report["omitted_evidence"].append({"path": name, "sha256": digest,
                "raw_bytes": len(raw), "critical": critical, "reason": "diagnostic_budget"})
        saved[digest] = relative
        return relative

    class Failure(Exception):
        pass

    for anchor_name in anchors:
        anchor_raw = _tree_file(repo, args.state_commit, anchor_name, archive)
        anchor_ref = {"path": anchor_name, "sha256": sha(anchor_raw)}
        root = Path(anchor_name).parents[4]
        proof_name = (root / "durability" / (anchor_ref["sha256"] + ".json")).as_posix()
        if proof_name in names:
            proof_raw = _tree_file(repo, args.state_commit, proof_name, archive)
            if json.loads(proof_raw).get("verified") is True:
                report["anchors"].append({"path": anchor_name, "status": "original_stamp_skips_verified",
                    "proof_path": proof_name, "proof_sha256": sha(proof_raw)})
                continue
        visited = {}
        values = {}
        def read(ref, chain):
            name = ref.get("runtime_relative_path", ref.get("path"))
            expected = ref.get("sha256")
            if not isinstance(name, str) or not HASH.fullmatch(str(expected)):
                raise ValueError("invalid_durable_ref")
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("unsafe_durable_ref")
            if name in visited:
                if visited[name] != expected:
                    raise ValueError("conflicting_durable_ref")
                return values[name]
            raw = _tree_file(repo, args.state_commit, name, archive)
            actual = sha(raw)
            item = {"path": name, "expected_sha256": expected, "actual_sha256": actual,
                "raw_bytes": len(raw), "chain": chain,
                "evidence_file": preserve(name, raw, critical=actual != expected)}
            report["objects"].append(item)
            if actual != expected:
                report["first_failure"] = {"error": "durable_source_hash_mismatch", "anchor": anchor_ref,
                    **item, "reference": ref, "interpretation": "unclassified_until_semantic_review"}
                raise Failure()
            visited[name] = expected
            value = json.loads(raw)
            values[name] = value
            def children(x, pointer):
                if isinstance(x, dict):
                    if isinstance(x.get("runtime_relative_path", x.get("path")), str) and HASH.fullmatch(str(x.get("sha256", ""))):
                        edge = {"source_path": name, "json_pointer": pointer,
                            "reference": x, "classification": "unclassified_original_traversal"}
                        report["edges"].append(edge)
                        read(x, chain + [edge])
                    for k, v in x.items():
                        key = str(k).replace("~", "~0").replace("/", "~1")
                        children(v, pointer + "/" + key)
                elif isinstance(x, list):
                    for i, v in enumerate(x):
                        children(v, pointer + "/" + str(i))
            children(value, "")
            return value
        try:
            read(anchor_ref, [])
        except Failure:
            report["anchors"].append({"path": anchor_name, "status": "first_failure",
                "verified_files_before_failure": len(visited)})
            break
        except Exception as exc:
            report["first_failure"] = {"error": type(exc).__name__, "message": str(exc),
                "anchor": anchor_ref, "interpretation": "unclassified_until_semantic_review"}
            report["anchors"].append({"path": anchor_name, "status": "other_failure"})
            break
        report["anchors"].append({"path": anchor_name, "status": "hash_closure_passed",
            "verified_files": len(visited)})
    report["diagnostic_complete"] = True
    dump(output / "diagnostic.json", report)
    total = sum(x.stat().st_size for x in output.rglob("*") if x.is_file())
    if total > args.max_evidence_bytes:
        raise ValueError("diagnostic_output_budget_exceeded")
    print(json.dumps({"state_commit": args.state_commit, "first_anchor_count": len(anchors),
        "processed_anchors": len(report["anchors"]), "objects_read": len(report["objects"]),
        "output_bytes": total, "first_failure": report["first_failure"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
