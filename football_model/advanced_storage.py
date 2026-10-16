"""Lossless compact JSON objects, shared subtrees and bounded immutable chunks.

Every transport reference uses actual bytes SHA-256. Loading reconstructs the
exact original JSON value; no predictions, failures or provenance are omitted.
This is an application format within the existing bounded Git state storage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .history_archive import read_bytes, exists

MAX_PIECE_BYTES = 4 * 1024 * 1024
INLINE_BYTES = 256 * 1024
# Actual Serie A reports/training inputs show the 512-byte threshold creates
# thousands of tiny objects whose manifest costs dominate their compressed data.
# 1024 reduces those files by 84% with only 10% more logical bytes; larger
# thresholds give little further physical benefit and duplicate more content.
INTERN_MINIMUM = 1024
OBJECT_MARKER = "$advanced_object_ref"
LIST_MARKER = "$advanced_list_chunks"
FORMAT = "advanced-compact-json-v1"


def _raw(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False) + "\n").encode()


def _ref(runtime, path):
    return {"path": path.resolve().relative_to(runtime.resolve()).as_posix(),
            "sha256": hashlib.sha256(read_bytes(path)).hexdigest()}


def _persist(runtime, directory, value, guard=None, final=False):
    raw = _raw(value)
    if len(raw) > MAX_PIECE_BYTES:
        raise ValueError("advanced JSON piece exceeds fixed bound")
    key = hashlib.sha256(raw).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (key + ".json")
    if exists(path):
        if read_bytes(path) != raw:
            raise ValueError("immutable advanced object collision")
    else:
        ticket = guard.check_write(path, raw, final=final) if guard is not None else None
        with path.open("xb") as handle:
            handle.write(raw)
        if ticket is not None:
            guard.commit_write(ticket)
    return path


def save_artifact(runtime, directory, value, *, inline_bytes=INLINE_BYTES, guard=None, final=False,
                  intern_minimum=INTERN_MINIMUM):
    runtime, directory = Path(runtime).resolve(), Path(directory).resolve()
    if not directory.is_relative_to(runtime):
        raise ValueError("advanced artifact directory escapes runtime")
    if type(inline_bytes) is not int or inline_bytes < 64 or inline_bytes >= MAX_PIECE_BYTES:
        raise ValueError("invalid advanced inline limit")
    objects = runtime / "advanced_models" / "objects"
    memo = {}
    occurrences = {}
    if type(intern_minimum) is not int or intern_minimum < 64 or intern_minimum > MAX_PIECE_BYTES:
        raise ValueError("invalid advanced intern minimum")

    def count(item):
        if not isinstance(item, (dict, list)):
            return
        encoded = _raw(item)
        if len(encoded) < intern_minimum:
            return
        key = hashlib.sha256(encoded).hexdigest()
        occurrences[key] = occurrences.get(key, 0) + 1
        for child in item.values() if isinstance(item, dict) else item:
            count(child)

    count(value)

    def pack(item):
        if not isinstance(item, (dict, list)):
            if len(_raw(item)) > inline_bytes:
                raise ValueError("single advanced scalar exceeds inline storage limit")
            return item
        encoded = _raw(item)
        if len(encoded) < min(intern_minimum, inline_bytes):
            return item
        digest = hashlib.sha256(encoded).hexdigest()
        if digest in memo:
            return memo[digest]
        shared = occurrences.get(digest, 0) > 1
        if isinstance(item, dict):
            if OBJECT_MARKER in item or LIST_MARKER in item:
                raise ValueError("reserved advanced storage field in original payload")
            transformed = {key: pack(value) for key, value in item.items()}
            if len(_raw(transformed)) > MAX_PIECE_BYTES:
                # Large maps use a list of key/value pairs with the same exact
                # key identities; dictionary order is not part of JSON meaning.
                pairs = pack([[key, value] for key, value in transformed.items()])
                transformed = {"$advanced_dict_items": pairs}
        elif isinstance(item, list):
            if len(encoded) <= inline_bytes:
                transformed = [pack(value) for value in item]
                if not shared:
                    return transformed
                path = _persist(runtime, objects, transformed, guard, final)
                result = {OBJECT_MARKER: _ref(runtime, path)}
                memo[digest] = result
                return result
            chunks, pending, size = [], [], 3
            for value in item:
                packed = pack(value)
                length = len(_raw(packed)) + 1
                if pending and size + length > MAX_PIECE_BYTES - 128:
                    chunks.append(_ref(runtime, _persist(runtime, objects, pending, guard, final)))
                    pending, size = [], 3
                pending.append(packed); size += length
            if pending:
                chunks.append(_ref(runtime, _persist(runtime, objects, pending, guard, final)))
            transformed = {LIST_MARKER: chunks, "length": len(item)}
        if len(encoded) <= inline_bytes and not shared:
            return transformed
        path = _persist(runtime, objects, transformed, guard, final)
        result = {OBJECT_MARKER: _ref(runtime, path)}
        memo[digest] = result
        return result

    packed = pack(value)
    # Small cards remain ordinary JSON for interoperable existing readers.
    # Large roots carry a content-addressed reference understood by load_report.
    return _persist(runtime, directory, packed, guard, final)


def load_artifact(runtime, ref):
    """Verify and reconstruct ordinary or chunked JSON; reject missing/cycles."""
    runtime = Path(runtime).resolve()
    active, cache = set(), {}

    def read(reference):
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise ValueError("invalid advanced object reference")
        relative = reference.get("runtime_relative_path", reference["path"])
        path = (runtime / relative).resolve()
        if not path.is_relative_to(runtime) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("advanced object reference escapes runtime")
        key = (path.as_posix(), reference.get("sha256"))
        if key in active:
            raise ValueError("cyclic advanced object graph")
        if key in cache:
            return cache[key]
        raw = read_bytes(path, runtime=runtime)
        if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
            raise ValueError("advanced object fingerprint mismatch")
        # Legacy full reports may exceed the new piece size; preservation is
        # allowed when reading, while every new write obeys the fixed bound.
        value = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite advanced JSON")))
        active.add(key)
        value = unpack(value)
        active.remove(key); cache[key] = value
        return value

    def unpack(value):
        if isinstance(value, dict):
            if OBJECT_MARKER in value:
                if set(value) != {OBJECT_MARKER}:
                    raise ValueError("ambiguous advanced object wrapper")
                return read(value[OBJECT_MARKER])
            if LIST_MARKER in value:
                if set(value) != {LIST_MARKER, "length"} or type(value["length"]) is not int or value["length"] < 0:
                    raise ValueError("invalid advanced list wrapper")
                out = []
                for reference in value[LIST_MARKER]:
                    part = read(reference)
                    if not isinstance(part, list):
                        raise ValueError("advanced list chunk is not a list")
                    out.extend(part)
                if len(out) != value["length"]:
                    raise ValueError("advanced list length mismatch")
                return out
            if "$advanced_dict_items" in value:
                if set(value) != {"$advanced_dict_items"}:
                    raise ValueError("ambiguous advanced dictionary wrapper")
                items = unpack(value["$advanced_dict_items"])
                out = dict(items)
                if len(out) != len(items):
                    raise ValueError("duplicate advanced dictionary keys")
                return out
            return {key: unpack(item) for key, item in value.items()}
        if isinstance(value, list):
            return [unpack(item) for item in value]
        return value
    return read(ref)


load_report = load_artifact
