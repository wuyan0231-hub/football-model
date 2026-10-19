"""Choose the single new domestic league for a release's first training run.

An explicit newly added training-request file can retry a configured league.
Normal code releases without a newly requested league return none (no training). Scheduled and manual
runs do not call this script. It reads local files only; no API or fitting.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _competitions(config):
    items = config["competitions"]
    if not isinstance(items, list):
        raise ValueError("competitions must be a list")
    result = {}
    for item in items:
        key = item["key"]
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
            raise ValueError("invalid competition key")
        if key in result:
            raise ValueError("duplicate competition key")
        result[key] = item
    return result


def select_league(before, current, request=None):
    old, new = _competitions(before), _competitions(current)
    added = [key for key in new if key not in old]
    if request is not None:
        if (not isinstance(request, dict) or request.get('schema_version') != 1
                or request.get('mode') != 'train'):
            raise ValueError('invalid release training request')
        key = request.get('league')
        if (not isinstance(key, str) or key not in new
                or new[key].get('mode') != 'domestic_baseline'):
            raise ValueError('training request must select a configured domestic league')
        if added and added != [key]:
            raise ValueError('new competition and training request targets conflict')
        return key
    if not added:
        return "none"
    if len(added) != 1 or new[added[0]].get("mode") != "domestic_baseline":
        raise ValueError("release first training requires exactly one new domestic league")
    return added[0]


def read_request(requests_list):
    if requests_list is None:
        return None
    names = requests_list.read_text(encoding='utf-8').splitlines()
    if not names:
        return None
    if (len(names) != 1 or re.fullmatch(
            r'\.github/training-requests/[a-z0-9][a-z0-9_-]*\.json', names[0]) is None):
        raise ValueError('expected one newly added training-request file')
    path = Path.cwd() / names[0]
    if path.is_symlink() or not path.is_file():
        raise ValueError('training request must be a regular file')
    return json.loads(path.read_text(encoding='utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-config", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    parser.add_argument("--requests-list", type=Path)
    args = parser.parse_args(argv)
    before = json.loads(args.before_config.read_text(encoding="utf-8"))
    current = json.loads(args.config.read_text(encoding="utf-8"))
    league = select_league(before, current, read_request(args.requests_list))
    with args.github_env.open("a", encoding="utf-8") as stream:
        stream.write(f"LEAGUE={league}\n")
    print(f"release training target: {league}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
