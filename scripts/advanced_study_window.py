"""Bind a study report to the actual command, run attempt and execution window."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

WINDOW = Path("advanced-study-run-window.json")
REPORT = Path("runtime/advanced_models/study_runs/latest.json")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("study report window requires timezone")
    return result.astimezone(timezone.utc)


def configured_leagues(selection, config=Path("config.json")):
    if selection != "all":
        return [selection]
    return [c["key"] for c in json.loads(config.read_text())["competitions"] if c.get("mode") != "audit_only"]


def run(command, selection, *, window=WINDOW, report=REPORT, config=Path("config.json")):
    value = {"schema_version": "advanced-study-command-window-v1", "started_at": _now(),
             "github_run_id": os.environ.get("GITHUB_RUN_ID"),
             "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
             "source_commit": os.environ.get("GITHUB_SHA"),
             "configured_leagues": configured_leagues(selection, config),
             "report_path": str(report), "previous_report_sha256": _sha(report)}
    window.write_text(json.dumps(value, indent=2))
    try:
        outcome = subprocess.run(command, check=False).returncode
    except OSError as exc:
        outcome = 127
        value["command_error_code"] = type(exc).__name__
    value.update(ended_at=_now(), command_returncode=outcome, report_sha256=_sha(report))
    value["report_changed"] = value["report_sha256"] is not None and value["report_sha256"] != value["previous_report_sha256"]
    window.write_text(json.dumps(value, indent=2))
    return outcome


def current_report(outcome, selection, *, window=WINDOW, report=REPORT, config=Path("config.json")):
    """A restored latest pointer can be inspected, but never count as this run."""
    if outcome not in ("success", "failure"):
        return {"status": "not_executed_in_this_step", "reason": "study_step_not_attempted"}
    observed = {"path": str(report), "sha256": _sha(report)}
    try:
        value = json.loads(window.read_text())
        raw = report.read_bytes()
        study = json.loads(raw)
        expected = configured_leagues(selection, config)
        identity = (value.get("schema_version") == "advanced-study-command-window-v1"
                    and value.get("github_run_id") == os.environ.get("GITHUB_RUN_ID")
                    and value.get("github_run_attempt") == os.environ.get("GITHUB_RUN_ATTEMPT")
                    and value.get("source_commit") == os.environ.get("GITHUB_SHA")
                    and value.get("source_commit") is not None
                    and value.get("configured_leagues") == expected
                    and study.get("configured_leagues") == expected
                    and study.get("source_commit") == value.get("source_commit"))
        timing = _time(value["started_at"]) <= _time(study["generated_at"]) <= _time(value["ended_at"])
        changed = (value.get("report_changed") is True and value.get("report_sha256") == observed["sha256"]
                   and value.get("previous_report_sha256") != observed["sha256"])
        if not identity or not timing or not changed:
            return {"status": "old_report_not_current", "reason": "report_does_not_match_step_identity_window_and_new_bytes",
                    "observed_report": observed}
        return {"status": "current_report_verified", **observed, "report": study,
                "window_path": str(window), "window_sha256": _sha(window)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "old_report_not_current", "reason": "new_report_or_command_window_unavailable",
                "observed_report": observed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a study command is required")
    return run(command, args.selection)


if __name__ == "__main__":
    raise SystemExit(main())
