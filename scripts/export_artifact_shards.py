"""Split an already downloaded artifact ZIP without changing its bytes."""
import argparse
import hashlib
import json
from pathlib import Path


def split_artifact(source, destination, expected_sha256, artifact_id, part_bytes=256 * 1024 * 1024):
    source, destination = Path(source), Path(destination)
    if not 1 <= part_bytes <= 256 * 1024 * 1024:
        raise ValueError('part size outside bounded export range')
    if len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256):
        raise ValueError('expected SHA256 required')
    destination.mkdir(parents=True, exist_ok=False)
    whole, parts, total = hashlib.sha256(), [], 0
    with source.open('rb') as stream:
        while True:
            first = stream.read(min(1024 * 1024, part_bytes))
            if not first:
                break
            name = f'artifact.zip.part{len(parts):03d}'
            digest, size = hashlib.sha256(), 0
            with (destination / name).open('xb') as output:
                block = first
                while block:
                    output.write(block)
                    digest.update(block)
                    whole.update(block)
                    size += len(block)
                    total += len(block)
                    if size == part_bytes:
                        break
                    block = stream.read(min(1024 * 1024, part_bytes - size))
            parts.append({'name': name, 'bytes': size, 'sha256': digest.hexdigest()})
    if not parts or whole.hexdigest() != expected_sha256:
        raise ValueError('original artifact bytes do not match expected SHA256')
    manifest = {'schema_version': 'github-artifact-byte-shards-v1', 'original_artifact_id': artifact_id,
                'original_zip_sha256': whole.hexdigest(), 'original_zip_bytes': total,
                'reconstruction': 'concatenate parts in manifest order; then verify original ZIP SHA256',
                'part_limit_bytes': part_bytes, 'parts': parts,
                'original_artifact_unchanged': True, 'production_state_modified': False}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--artifact-id', type=int, required=True)
    args = parser.parse_args()
    result = split_artifact(args.source, args.output, args.sha256, args.artifact_id)
    print(json.dumps({'artifact_id': args.artifact_id, 'parts': len(result['parts']),
                      'original_zip_sha256': result['original_zip_sha256']}))
