"""Retire UNL from a fully restored runtime without rewriting Git history.

Run cloud_state.py load first and its ordinary check/save afterwards. This
module never edits a transport manifest, fetches football data, trains, or pushes.
Anonymous/cold originals are read through the existing verified archive reader.
Shared historical originals needed by domestic predictions remain byte exact;
their retention is explicitly reported rather than called complete erasure.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from football_model import history_archive as history
from football_model import state_storage

SCHEMA = "competition-retirement-v1"
TARGET = "unl"
_HASH = re.compile(r"[0-9a-f]{64}")
_OID = re.compile(r"[0-9a-f]{40}")
_TOKEN = re.compile(r"(?:^|[^a-z0-9])unl(?:$|[^a-z0-9])", re.I)
_RETIRED = {"unl", "uefanationsleague", "nationsleague", "europeannationsleague",
            "欧国联", "欧洲国家联赛"}
_IDENTITY_FIELDS = {"competition", "competition_key", "league", "key"}
_MUTABLE_INDEXES = {"integration/index.json", "integration/f5c-index.json"}


class RetirementError(RuntimeError):
    """Safe error codes only; original provider text is never printed."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    return state_storage._manifest_json(raw)


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       indent=2) + "\n").encode()


def _retired(value):
    return isinstance(value, str) and re.sub(r"[\W_]+", "", value.casefold()) in _RETIRED


def _target_text(value):
    return isinstance(value, str) and (_retired(value) or bool(_TOKEN.search(value))
        or "欧国联" in value or "欧洲国家联赛" in value or "UEFA Nations League" in value)


def _owner(name, competitions):
    for part in Path(name).parts:
        key = part.removesuffix(".json").removesuffix(".csv").removesuffix(".md")
        if key in competitions or key == TARGET:
            return key
    if name.startswith("registries/nations_venue"):
        return TARGET
    return None


def _current_pointer(name):
    leaf = Path(name).name
    return leaf.startswith("latest") or leaf in {"index.json", "index.md", "queue.json"}


def _declares_target(value):
    if not isinstance(value, dict):
        return False
    if any(_retired(value.get(field)) for field in _IDENTITY_FIELDS):
        return True
    competition = value.get("competition")
    if isinstance(competition, dict) and _declares_target(competition):
        return True
    # Provider 5 is meaningful only in an actual league identity, never a
    # generic team/player/venue/match `id`.
    league = value.get("league")
    if isinstance(league, dict) and (str(league.get("id")) == "5"
            or _retired(league.get("name"))):
        return True
    params = value.get("params")
    return (value.get("endpoint") in {"/fixtures", "/leagues", "/odds"}
        and isinstance(params, dict)
        and (str(params.get("league")) == "5" or _retired(params.get("name"))
             or (value.get("endpoint") == "/leagues" and str(params.get("id")) == "5")))


def _facts(value, *, inherited=False):
    """Collect identities without treating all IDs in a mixed report as UNL."""
    strings, matches, target_matches = set(), set(), set()
    target = False
    stack = [(value, inherited)]
    while stack:
        item, scope = stack.pop()
        if isinstance(item, dict):
            scope = scope or _declares_target(item)
            target |= scope
            for field in ("match_id", "fixture_id"):
                mid = item.get(field)
                if type(mid) is int and mid > 0:
                    matches.add(mid)
                    if scope:
                        target_matches.add(mid)
            fixture = item.get("fixture")
            if isinstance(fixture, dict) and type(fixture.get("id")) is int:
                matches.add(fixture["id"])
                if scope:
                    target_matches.add(fixture["id"])
            params = item.get("params")
            if isinstance(params, dict):
                mid = params.get("fixture")
                if item.get("endpoint") == "/fixtures":
                    mid = params.get("id", mid)
                if type(mid) is int and mid > 0:
                    matches.add(mid)
                    if scope:
                        target_matches.add(mid)
            for key, child in item.items():
                strings.add(key)
                # Map keys such as `unl:1528939` are identities too.
                if _target_text(key):
                    target = True
                stack.append((child, scope or _retired(key)))
        elif isinstance(item, list):
            stack.extend((child, scope) for child in item)
        elif isinstance(item, str):
            strings.add(item)
            target |= _target_text(item)
            # Preserved JSON response text still contains real API identities.
            if item.lstrip().startswith(("{", "[")):
                try:
                    nested = _json(item.encode())
                except state_storage.StorageError:
                    pass
                else:
                    stack.append((nested, scope))
    return target, matches, target_matches, strings


_DROP = object()


def _project_index(value):
    """Only derived indexes are projected; immutable originals are never edited."""
    if isinstance(value, dict):
        if _declares_target(value):
            return _DROP
        output = {}
        for key, child in value.items():
            if _target_text(key):
                continue
            projected = _project_index(child)
            if projected is not _DROP:
                output[key] = projected
        return output
    if isinstance(value, list):
        return [projected for child in value
                if (projected := _project_index(child)) is not _DROP]
    return _DROP if _target_text(value) else value


def _references(strings, names, hash_names, parent):
    refs = set()
    for value in strings:
        name = value[8:] if value.startswith("runtime/") else value
        if name in names:
            refs.add(name)
        # Object identifiers without paths are used in prospective ledgers.
        if _HASH.fullmatch(value):
            refs.update(hash_names.get(value, ()))
    refs.discard(parent)
    return refs


def _reach(seeds, edges):
    reached = set(seeds)
    queue = deque(seeds)
    while queue:
        for name in edges.get(queue.popleft(), ()):
            if name not in reached:
                reached.add(name)
                queue.append(name)
    return reached


def _write(path, raw):
    history._safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(raw)
    temp.replace(path)


def retire(runtime, *, competition, receipt, apply=False, base_commit=None,
           competitions=None, config=None):
    """Return a complete readback receipt; any unresolved scan prevents writes."""
    if competition != TARGET:
        raise RetirementError("retirement_scope_not_authorized")
    runtime = history._safe_path(Path(runtime).absolute())
    receipt = history._safe_path(Path(receipt).absolute())
    if not runtime.is_dir() or runtime.name != "runtime":
        raise RetirementError("restored_runtime_required")
    if receipt.is_relative_to(runtime):
        raise RetirementError("receipt_must_be_outside_runtime")
    if apply and (not isinstance(base_commit, str) or not _OID.fullmatch(base_commit)):
        raise RetirementError("fixed_loaded_state_required")
    competitions = set(competitions or ()) - {TARGET}
    state_storage.validate_runtime(runtime)
    catalog = history._catalog_unscoped(runtime, use_cache=False)
    catalog_before = {name: dict(entry) for name, entry in catalog.items()}
    records, edges, replacements, target_ids = {}, {}, {}, set()
    with history.verified_catalog_read_session(runtime):
        paths = [p for p in history.iter_paths(runtime)
                 if not history.is_catalog_member(p.relative_to(runtime).as_posix())]
        names = {p.relative_to(runtime).as_posix() for p in paths}
        hash_names = defaultdict(set)
        for name in names:
            if _HASH.fullmatch(Path(name).stem):
                hash_names[Path(name).stem].add(name)
        for position, path in enumerate(paths):
            name = path.relative_to(runtime).as_posix()
            owner = _owner(name, competitions)
            if name in catalog and not path.is_file():
                # Catalog originals are already historical, with exact fixed
                # source/transport/raw-byte routes. Do not fetch whole ancestor
                # snapshots merely to erase historical mentions from a shared
                # compact object. This scope is disclosed in the receipt.
                entry = catalog[name]
                records[name] = {"path": name, "sha256": entry["raw_sha256"],
                    "bytes": entry["raw_bytes"], "owner": owner,
                    "target": owner == TARGET, "matches": set(), "cold": True,
                    "physical": False, "historical_route_only": True, "domestic": False}
                edges[name] = set()
                continue
            raw, digest = history.read_bytes_with_digest(path, runtime=runtime)
            payload = (state_storage._expand(raw, state_storage.MAX_FILE_BYTES)
                       if name.startswith("replay/bundles/") and name.endswith(".json.gz") else raw)
            if name.endswith((".json", ".json.gz")):
                value = _json(payload)
                if name in _MUTABLE_INDEXES:
                    projected = _project_index(value)
                    if projected is _DROP:
                        raise RetirementError("index_projection_removed_all_competitions")
                    if projected != value:
                        if name == "integration/index.json" and isinstance(projected.get("competitions"), list):
                            from football_model.cli import availability_summary
                            projected["forecast_availability_summary"] = availability_summary(projected["competitions"])
                        replacements[name] = _encode(projected)
                    inspected = projected
                else:
                    inspected = value
                target, mids, scoped_ids, strings = _facts(inspected, inherited=owner == TARGET)
                target_ids.update(scoped_ids)
                edges[name] = _references(strings, names, hash_names, name)
            else:
                # Human derived indexes may contain retired menu entries.
                try:
                    target = _target_text(payload.decode("utf-8"))
                except UnicodeDecodeError:
                    target = False
                mids, edges[name], strings = set(), set(), set()
            records[name] = {"path": name, "sha256": digest, "bytes": len(raw),
                "owner": owner, "target": target or owner == TARGET,
                "matches": mids, "cold": name in catalog, "physical": path.is_file(),
                "historical_route_only": False, "domestic": bool(strings & competitions)}
            if position and position % 2000 == 0:
                print("RETIREMENT_SCAN=" + json.dumps({"read": position,
                    "total": len(paths)}), flush=True)
    # IDs learned from exact UNL scopes identify its nameless fundamentals,
    # fixture snapshots and API caches; an ordinary `id=5` never does.
    for row in records.values():
        row["target"] |= bool(row["matches"] & target_ids)
    target_id_parts = {str(mid) for mid in target_ids}
    for name, row in records.items():
        if name.startswith("fundamentals/") and set(Path(name).parts) & target_id_parts:
            row["target"] = True
    if config is not None:
        for name in ("integration/index.json", "integration/index.md"):
            if name in records:
                replacements.setdefault(name, (runtime / name).read_bytes())
    direct = {name for name, row in records.items() if row["target"]}
    # All domestic namespaces, and safe global roots, retain their exact closure.
    domestic_roots = {name for name, row in records.items()
        if row["owner"] in competitions or row["domestic"] or (not row["target"]
            and not _HASH.fullmatch(Path(name).stem))}
    retired_namespaces = {name for name, row in records.items() if row["owner"] == TARGET}
    retired_global_pointers = {name for name, row in records.items() if row["target"]
        and row["owner"] is None and _current_pointer(name) and name not in replacements}
    excluded_current = retired_namespaces | retired_global_pointers
    domestic_roots -= excluded_current
    # A mixed historical all-league report contains the old UNL row's paths as
    # well. That row does not make the UNL live model a domestic dependency.
    # Domestic consumers retain their shared parent bytes, while retired rows
    # resolve only through their fixed historical state, never today's entry.
    domestic = _reach(domestic_roots,
        {name: refs - excluded_current for name, refs in edges.items()})
    historical_routes = {name for name, row in records.items() if row["historical_route_only"]}
    protected = domestic | historical_routes
    retired_closure = _reach(direct, edges)
    shared = direct & protected
    # Current UNL entry points are never kept as historical dependencies.
    forbidden_shared = sorted(name for name in shared if records[name]["owner"] == TARGET
        and (Path(name).name.startswith("latest") or name.startswith(("models/", "data/"))))
    if forbidden_shared:
        raise RetirementError("domestic_dependency_on_live_retired_entrypoint")
    deleted = (retired_closure - protected) | (direct - shared)
    deleted -= set(replacements)
    retained = set(records) - deleted
    # No graph is silently discarded: shared originals are explicitly historical.
    shared_historical = sorted((retired_closure & domestic) - set(replacements))
    unresolved = sorted(name for name in retained if records[name]["target"]
        and name not in shared_historical and name not in replacements)
    if unresolved:
        raise RetirementError("unresolved_retired_records")
    route = lambda name: {"state_commit": base_commit, "logical_path": name,
        "raw_sha256": records[name]["sha256"], "raw_bytes": records[name]["bytes"]}
    report = {"schema_version": SCHEMA, "competition": TARGET,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "planned" if not apply else "applied",
        "base_state_commit": base_commit, "git_history_rewritten": False,
        "football_api_requests": 0, "new_fits": 0, "state_pushed": False,
        "logical_routes_inventoried": len(records),
        "physical_payloads_scanned": sum(row["physical"] for row in records.values()),
        "cold_routes_validated": len(catalog), "cold_payloads_reverified": False,
        "unexpanded_historical_originals_retained": sum(row["historical_route_only"] for row in records.values()),
        "unattributed_anonymous_originals_retained": sum(
            not row["owner"] and not row["target"] and name not in retired_closure
            and bool(_HASH.fullmatch(Path(name).stem)) for name, row in records.items()),
        "retired_match_ids_count": len(target_ids),
        "deleted": [route(name) for name in sorted(deleted)],
        "mutable_indexes_projected": [{"path": name,
            "before_sha256": records[name]["sha256"], "after_sha256": _sha(raw)}
            for name, raw in sorted(replacements.items())],
        "shared_historical_objects_retained_due_to_domestic_dependencies":
            [route(name) for name in shared_historical],
        "retired_references_to_unexpanded_historical_originals_retained":
            [route(name) for name in sorted((retired_closure & historical_routes) - domestic)],
        "unresolved": [], "current_unl_entrypoints_removed": True,
        "all_related_contents_removed": False,
        "remaining_scope": "Fixed historical shared originals, unexpanded anonymous cold originals and Git ancestors remain; current configuration rejects the retired competition. No physical all-history erasure is claimed.",
        "other_competitions_original_bytes_preserved": False}
    # Recheck every physical original and catalog route before first mutation.
    for name, row in records.items():
        if row["physical"] and _sha((runtime / name).read_bytes()) != row["sha256"]:
            raise RetirementError("runtime_changed_since_plan")
    current_physical = {name for name, _ in state_storage._files(runtime)
                        if not history.is_catalog_member(name)}
    if current_physical != {name for name, row in records.items() if row["physical"]}:
        raise RetirementError("runtime_file_set_changed_since_plan")
    if history._catalog_unscoped(runtime, use_cache=False) != catalog_before:
        raise RetirementError("catalog_changed_since_plan")
    if apply:
        for name in sorted(deleted):
            (runtime / name).unlink(missing_ok=True)
        for name, raw in replacements.items():
            if config is None or name not in {"integration/index.json", "integration/index.md"}:
                _write(runtime / name, raw)
        if config is not None:
            # This is a current derived view of the same saved production
            # originals, not a new forecast or a retroactive parent binding.
            from football_model.integration import build_index
            rebuilt = build_index(runtime, config)
            if any(row.get("key") == TARGET for row in rebuilt["competitions"]):
                raise RetirementError("rebuilt_index_contains_retired_competition")
            for name in ("integration/index.json", "integration/index.md"):
                replacements[name] = (runtime / name).read_bytes()
        if catalog_before:
            history._write_catalog(runtime,
                {name: entry for name, entry in catalog_before.items() if name not in deleted})
        # Empty retired namespaces do not remain in a saved Git tree.
        for path in sorted(runtime.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        state_storage.validate_runtime(runtime)
    # Kept cold routes remain exact; hot originals are read again byte for byte.
    for name in sorted(retained - set(replacements)):
        row = records[name]
        if row["physical"] and _sha((runtime / name).read_bytes()) != row["sha256"]:
            raise RetirementError("retained_original_changed")
    if apply:
        after_catalog = history._catalog_unscoped(runtime, use_cache=False)
        if after_catalog != {name: entry for name, entry in catalog_before.items() if name not in deleted}:
            raise RetirementError("retained_cold_route_changed")
    report["other_competitions_original_bytes_preserved"] = True
    report["mutable_indexes_projected"] = [{"path": name,
        "before_sha256": records.get(name, {}).get("sha256"),
        "after_sha256": _sha(raw),
        "rebuilt_from_saved_originals": bool(config is not None and name in {
            "integration/index.json", "integration/index.md"})}
        for name, raw in sorted(replacements.items())]
    report["domestic_core_originals"] = [{"path": name, "sha256": row["sha256"],
        "bytes": row["bytes"]} for name, row in sorted(records.items())
        if row["owner"] in competitions and name.split("/")[0] in {"models", "predictions", "data"}]
    manifest = b"".join(_encode({"path": name, "sha256": records[name]["sha256"],
        "bytes": records[name]["bytes"], "action": "delete" if name in deleted else
        "project_index" if name in replacements else "retain_exact"}).replace(b"\n", b"") + b"\n"
        for name in sorted(records))
    manifest_raw = gzip.compress(manifest, compresslevel=6, mtime=0)
    manifest_path = receipt.with_name(receipt.stem + "-originals.jsonl.gz")
    _write(manifest_path, manifest_raw)
    report["originals_manifest"] = {"path": manifest_path.name, "sha256": _sha(manifest_raw),
        "bytes": len(manifest_raw), "uncompressed_sha256": _sha(manifest), "rows": len(records)}
    _write(receipt, _encode(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--competition", required=True, choices=[TARGET])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        runtime = args.state_dir.absolute()
        repo = runtime.parent
        marker = repo / ".git" / "football-cloud-state.json"
        baseline = _json(marker.read_bytes()) if marker.is_file() else {}
        config = _json((repo / "config.json").read_bytes())
        leagues = {row["key"] for row in config["competitions"]}
        if TARGET in leagues:
            raise RetirementError("current_configuration_still_enables_retired_competition")
        result = retire(runtime, competition=args.competition, receipt=args.receipt,
            apply=args.apply, base_commit=baseline.get("state_oid"), competitions=leagues,
            config=config)
        print("RETIREMENT_RESULT=" + json.dumps({"status": result["status"],
            "deleted": len(result["deleted"]), "shared_historical_retained":
            len(result["shared_historical_objects_retained_due_to_domestic_dependencies"])}))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, RetirementError) else type(exc).__name__
        # A failed scan never reaches mutations; failed apply is not saved by
        # this command. The workflow must not run cloud_state save on failure.
        print("RETIREMENT_FAILED=" + code, file=sys.stderr)
        try:
            if not args.receipt.absolute().is_relative_to(args.state_dir.absolute()):
                _write(args.receipt.absolute(), _encode({"schema_version": SCHEMA,
                    "competition": TARGET, "status": "incomplete", "error_code": code,
                    "state_pushed": False, "all_related_contents_removed": False}))
        except (OSError, state_storage.StorageError):
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
