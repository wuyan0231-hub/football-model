"""Materialize previously fetched GitHub connector responses, without network access.

This checks supplied response consistency, not authentication or model validity.
It neither fetches credentials nor trains or executes any downloaded source.
"""
import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from urllib.parse import unquote, urlsplit


REPOSITORY = "wuyan0231-hub/football-model"
MANIFEST = "model_access_manifest.json"
SHA = re.compile(r"[0-9a-f]{40}\Z")


class MaterializationError(ValueError):
    """A supplied response or destination cannot be safely materialized."""


def _require(condition, message):
    if not condition:
        raise MaterializationError(message)


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _path(value):
    _require(isinstance(value, str) and bool(value), "file path must be nonempty")
    parts = value.split("/")
    _require(not value.startswith("/") and "\\" not in value and ":" not in value
             and all(part not in ("", ".", "..") for part in parts)
             and all(part.casefold() != ".git" for part in parts)
             and not any(ord(char) < 32 or ord(char) == 127 for char in value)
             and str(PurePosixPath(value)) == value,
             "file path must be a canonical relative path without .git")
    _require(parts[0] != MANIFEST, "file path is reserved for the manifest")
    return value


def _bytes(response):
    content = response.get("content")
    _require(isinstance(content, str), "response content must be a string")
    encoding = response.get("encoding")
    try:
        if encoding == "utf-8":
            return content.encode("utf-8")
        if encoding == "base64":
            # GitHub wraps base64 in ASCII whitespace; all other bytes remain
            # subject to strict decoding rather than silently being discarded.
            compact = re.sub(rb"[ \t\r\n\v\f]", b"", content.encode("ascii"))
            return base64.b64decode(compact, validate=True)
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise MaterializationError("response content has invalid encoding") from exc
    raise MaterializationError("response encoding must be utf-8 or base64")


def validate(payload):
    """Validate every response before any destination is written."""
    _require(isinstance(payload, dict), "input must be a JSON object")
    _require(type(payload.get("schema_version")) is int
             and payload["schema_version"] == 1, "unsupported schema_version")
    _require(payload.get("repository") == REPOSITORY, "unexpected repository")
    for key in ("source_commit", "state_commit"):
        _require(_sha(payload.get(key)), key + " must be a full lowercase commit SHA")
    files = payload.get("files")
    _require(isinstance(files, list) and bool(files), "files must be a nonempty list")
    records, paths = [], set()
    for item in files:
        _require(isinstance(item, dict), "each file must be an object")
        role = item.get("role")
        _require(role in ("source", "state"), "file role must be source or state")
        path = _path(item.get("path"))
        _require((role == "state" and path.startswith("runtime/"))
                 or (role == "source" and path != "runtime"
                     and not path.startswith("runtime/")), "file role and path disagree")
        _require(path not in paths, "duplicate file path")
        paths.add(path)
        response = item.get("response")
        _require(isinstance(response, dict), "file needs its connector response object")
        url = response.get("display_url")
        _require(isinstance(url, str), "response display_url is required")
        try:
            parsed = urlsplit(url)
            decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise MaterializationError("invalid response display_url") from exc
        commit = payload[role + "_commit"]
        _require(parsed.scheme == "https" and parsed.netloc == "github.com"
                 and not parsed.query and not parsed.fragment
                 and decoded == "/" + REPOSITORY + "/blob/" + commit + "/" + path,
                 "response URL does not bind repository, role commit and file path")
        _require(_sha(response.get("sha")), "response needs a full Git blob SHA")
        data = _bytes(response)
        blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
        _require(blob == response["sha"], "response bytes do not match Git blob SHA")
        records.append((path, data, {
            "role": role, "path": path, "commit": commit,
            "display_url": url, "bytes": len(data), "git_blob_sha": blob,
            "sha256": hashlib.sha256(data).hexdigest(),
        }))
    for path in paths:
        _require(not any(str(parent) in paths for parent in PurePosixPath(path).parents),
                 "file paths conflict as parent and child")
    return records


def _destination(destination):
    target = Path(os.path.abspath(os.fspath(destination)))
    _require(not os.path.lexists(target), "destination must not already exist")
    for ancestor in (target.parent, *target.parent.parents):
        _require(not ancestor.is_symlink(), "destination ancestry must not contain symlinks")
    _require(target.parent.is_dir(), "destination parent directory must already exist")
    return target


def materialize(payload, destination):
    records = validate(payload)
    target = _destination(destination)
    manifest = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "source_commit": payload["source_commit"],
        "state_commit": payload["state_commit"],
        "materialized_at": datetime.now(timezone.utc).isoformat(),
        "status": "files_materialized_not_model_verified",
        "scope": "Supplied connector-response consistency only; not authentication, "
                 "complete-model verification or integration match verification. "
                 "No network access, credential access, training or downloaded-code execution.",
        "files": [record for _, _, record in records],
    }
    temporary = Path(tempfile.mkdtemp(prefix=".model-access-", dir=target.parent))
    try:
        for path, data, _ in records:
            output = temporary / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        (temporary / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Recheck immediately before publishing; never intentionally overwrite.
        _destination(target)
        temporary.rename(target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON response bundle, or - for stdin")
    parser.add_argument("--destination", required=True, help="new directory under an existing parent")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            payload = json.load(sys.stdin)
        else:
            with open(args.input, encoding="utf-8") as handle:
                payload = json.load(handle)
        manifest = materialize(payload, args.destination)
    except (MaterializationError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Do not print supplied content, URLs or operating-system paths on errors.
        message = str(exc) if isinstance(exc, MaterializationError) else type(exc).__name__
        print("Materialization failed: " + message, file=sys.stderr)
        return 1
    print(json.dumps({"status": manifest["status"], "file_count": len(manifest["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
