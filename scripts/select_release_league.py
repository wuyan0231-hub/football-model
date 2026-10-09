"""Choose the single new domestic league for a release's first training run.

Normal code releases keep the existing unl/train check. Scheduled and manual
runs do not call this script. It reads configuration only; no API or fitting.
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


def select_league(before, current):
    old, new = _competitions(before), _competitions(current)
    added = [key for key in new if key not in old]
    if not added:
        return "unl"
    if len(added) != 1 or new[added[0]].get("mode") != "domestic_baseline":
        raise ValueError("release first training requires exactly one new domestic league")
    return added[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-config", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    args = parser.parse_args(argv)
    before = json.loads(args.before_config.read_text(encoding="utf-8"))
    current = json.loads(args.config.read_text(encoding="utf-8"))
    league = select_league(before, current)
    with args.github_env.open("a", encoding="utf-8") as stream:
        stream.write(f"LEAGUE={league}\n")
    print(f"release training target: {league}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
