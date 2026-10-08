"""Offline-only lossless shards. This script never edits application runtime.

The destination must be new. Production does not yet read this format.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import zlib


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('invalid_path')
    p = PurePosixPath(value)
    if p.is_absolute() or p.as_posix() != value or any(x in ('', '.', '..') or ':' in x for x in value.split('/')):
        raise ValueError('invalid_path')
    return value


def emit(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _no_symlink_ancestors(path):
    """Check the lexical chain before resolving; include missing destinations.

    Checking only the final directory misses ``alias/new_directory`` where the
    alias redirects a write into the source tree. Keep the original chain so a
    symlink followed by ``..`` cannot disappear during normalization.
    """
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('path_symlink_not_allowed')
    return path


def _checked_roots(source, dest):
    source = _no_symlink_ancestors(source).resolve(strict=False)
    dest = _no_symlink_ancestors(dest).resolve(strict=False)
    if source == dest or source in dest.parents or dest in source.parents:
        raise ValueError('source_destination_overlap')
    return source, dest


def _read_regular(path):
    path = _no_symlink_ancestors(path)
    if not path.is_file():
        raise ValueError('source_not_regular')
    return path.read_bytes()


def create(source, dest, limit=4*1024*1024):
    source, dest = _checked_roots(source, dest)
    if not source.is_dir() or dest.exists() or limit < 1024:
        raise ValueError('invalid_source_destination_or_limit')
    files = []
    for p in sorted(source.rglob('*')):
        if p.is_symlink():
            raise ValueError('source_symlink')
        if p.is_dir():
            continue
        if not p.is_file():
            raise ValueError('source_not_regular')
        rel = safe_path(p.relative_to(source).as_posix())
        raw = _read_regular(p)
        files.append((rel, raw))
    source, dest = _checked_roots(source, dest)
    dest.mkdir(parents=True)
    entries, shards, locations = [], [], {}
    current = bytearray()
    members = []
    def flush():
        nonlocal current, members
        if not current:
            return
        digest = sha(current)
        name = digest + '.bin'
        (dest / name).write_bytes(current)
        shards.append({'file': name, 'sha256': digest, 'size': len(current)})
        for raw_hash, offset, size in members:
            locations[raw_hash] = {'shard': digest, 'offset': offset, 'compressed_bytes': size}
        current, members = bytearray(), []
    pending = set()
    for rel, raw in files:
        raw_hash = sha(raw)
        entries.append({'path': rel, 'raw_sha256': raw_hash, 'raw_bytes': len(raw)})
        if raw_hash in locations or raw_hash in pending:
            continue
        blob = gzip.compress(raw, compresslevel=6, mtime=0)
        if len(blob) > limit:
            raise ValueError('single_object_exceeds_shard_limit')
        if current and len(current) + len(blob) > limit:
            flush()
        members.append((raw_hash, len(current), len(blob)))
        pending.add(raw_hash)
        current.extend(blob)
    flush()
    for entry in entries:
        entry.update(locations[entry['raw_sha256']])
    manifest = {'schema': 'offline_lossless_shards_v1', 'production_format': False,
                'shard_limit_bytes': limit, 'entries': entries, 'shards': shards}
    emit(dest / 'manifest.json', manifest)
    (dest / 'manifest.sha256').write_text(sha((dest/'manifest.json').read_bytes())+'\n')
    return {'files': len(entries), 'shards': len(shards), 'raw_bytes': sum(len(r) for _,r in files),
            'compressed_unique_bytes': sum(s['size'] for s in shards),
            'unique_objects': len(locations), 'manifest_sha256': sha((dest/'manifest.json').read_bytes())}


def restore(source, dest):
    source, dest = _checked_roots(source, dest)
    if not source.is_dir() or dest.exists():
        raise ValueError('invalid_restore_source_destination')
    raw_manifest = _read_regular(source/'manifest.json')
    expected = _read_regular(source/'manifest.sha256').decode('utf-8').strip()
    if sha(raw_manifest) != expected:
        raise ValueError('manifest_hash_mismatch')
    manifest = json.loads(raw_manifest)
    if manifest.get('schema') != 'offline_lossless_shards_v1' or manifest.get('production_format') is not False:
        raise ValueError('unknown_manifest')
    blobs = {}
    for shard in manifest['shards']:
        name = safe_path(shard['file'])
        if name != shard['sha256'] + '.bin' or shard['size'] > manifest['shard_limit_bytes']:
            raise ValueError('invalid_shard_binding')
        p = source/name
        if p.is_symlink() or not p.is_file():
            raise ValueError('missing_or_symlink_shard')
        raw = _read_regular(p)
        if len(raw) != shard['size'] or sha(raw) != shard['sha256'] or shard['sha256'] in blobs:
            raise ValueError('shard_hash_size_or_duplicate')
        blobs[shard['sha256']] = raw
    outputs, paths = [], set()
    for entry in manifest['entries']:
        rel = safe_path(entry['path'])
        if rel in paths:
            raise ValueError('duplicate_path')
        paths.add(rel)
        blob = blobs[entry['shard']]
        start, length, size = entry['offset'], entry['compressed_bytes'], entry['raw_bytes']
        if any(type(x) is not int or x < 0 for x in (start,length,size)) or size > 60*1024*1024 or start+length > len(blob):
            raise ValueError('entry_extent_or_logical_limit')
        engine = zlib.decompressobj(16+zlib.MAX_WBITS)
        plain = engine.decompress(blob[start:start+length], size+1)
        if len(plain) != size or engine.unconsumed_tail or not engine.eof or engine.unused_data or sha(plain) != entry['raw_sha256']:
            raise ValueError('raw_hash_size_or_gzip_mismatch')
        outputs.append((rel,plain))
    # No destination is created until all objects have passed validation.
    source, dest = _checked_roots(source, dest)
    dest.mkdir(parents=True)
    for rel, plain in outputs:
        p = dest/rel
        _no_symlink_ancestors(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        _no_symlink_ancestors(p)
        with p.open('xb') as handle:
            handle.write(plain)
    return {'restored_files': len(outputs), 'restored_raw_bytes': sum(len(r) for _,r in outputs), 'verified_before_write': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['create','restore'])
    parser.add_argument('source', type=Path)
    parser.add_argument('dest', type=Path)
    parser.add_argument('--shard-bytes', type=int, default=4*1024*1024)
    args = parser.parse_args()
    result = create(args.source,args.dest,args.shard_bytes) if args.operation=='create' else restore(args.source,args.dest)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
