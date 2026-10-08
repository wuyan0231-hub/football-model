"""Offline, mode-specific launch checks; never manufacture a live match receipt.

This gate composes existing evidence, integration and decision validators. Human
content/market observations remain explicitly labelled attestations. Missing
numerical evidence removes numerical use, not a supported qualitative judgment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys

from .decision_audit import validate_decision_record
from .decision_quotes import adapt_quote_row, _unverified_api_asian_handicap
from .decision_record import verify_decision_bundle
from .evidence import load_analysis_inputs, select_quotes, verify_bundle
from .integration import match_packet
from .model import RESULT_AVAILABILITY_DELAY, _validated_matches
from .state_storage import (estimate_runtime, validate_runtime, validate_storage_tree,
                            MAX_PHYSICAL_BYTES, MAX_LOGICAL_BYTES)


VERSION = "s2-pretest-1.0"
MODES = {"ordinary", "flow_pilot", "framework_comparison", "f5_confirmation"}
STRICT_MODES = MODES - {"ordinary"}


class PretestError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    if not isinstance(value, str):
        raise PretestError("aware_time_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PretestError("aware_time_required")
    return parsed.astimezone(timezone.utc)


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _path(root, value):
    if not isinstance(value, str) or "\\" in value:
        raise PretestError("unsafe_path")
    relative = PurePosixPath(value)
    if not value or relative.is_absolute() or any(p in {".", ".."} for p in value.split("/")):
        raise PretestError("unsafe_path")
    root = Path(root)
    if root.is_symlink():
        raise PretestError("symlink_not_allowed")
    target = root
    for part in relative.parts:
        target /= part
        if target.is_symlink():
            raise PretestError("symlink_not_allowed")
    if not target.resolve().is_relative_to(root.resolve()):
        raise PretestError("unsafe_path")
    return target


def _read_ref(root, reference):
    if (not isinstance(reference, dict)
            or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))):
        raise PretestError("file_reference_and_digest_required")
    target = _path(root, reference.get("path"))
    if not target.is_file():
        raise PretestError("regular_file_required")
    content = target.read_bytes()
    if _hash(content) != reference["sha256"]:
        raise PretestError("readback_digest_mismatch")
    return content


def _positive_ids(values):
    return (isinstance(values, list) and all(type(v) is int and v > 0 for v in values)
            and len(set(values)) == len(values))


def _refs(value, source_ids):
    return isinstance(value, list) and bool(value) and all(v in source_ids for v in value)


def _failure_code(exc):
    # Do not echo source text, raw paths, account information or exception payloads.
    return str(exc) if isinstance(exc, PretestError) else "invalid_or_unreadable_evidence"


def assess_pretest(record, *, source_root, mode=None):
    """Read real files, rerun the saved forecast, and classify limited uses.

    A passing gate is engineering readiness, not observed prediction improvement.
    The input is match-archive-1.0 with one added ``pretest_checks`` object; all
    substantive data stays in existing source/quote/decision/model artifacts.
    """
    root = Path(source_root)
    if not isinstance(record, dict) or record.get("schema_version") != "match-archive-1.0":
        raise PretestError("match_archive_record_required")
    mode = mode or record.get("pretest_mode", "ordinary")
    if mode not in MODES:
        raise PretestError("unsupported_pretest_mode")
    checks = record.get("pretest_checks", {})
    if not isinstance(checks, dict):
        raise PretestError("pretest_checks_required")
    cutoff, kickoff = _time(record.get("analysis_cutoff")), _time(record.get("kickoff_at"))
    if cutoff > _now():
        raise PretestError("analysis_cutoff_in_future")
    if record.get("match_id") is None or not record.get("version_id"):
        raise PretestError("match_and_version_required")
    findings, details = [], {}
    numerical = True
    excluded_rows = []

    def finding(code, gate, message, *, block_modes=STRICT_MODES, fallback=None, hard=False):
        blocked = hard or mode in block_modes
        findings.append({"code": code, "gate": gate, "status": "blocked" if blocked else "local_fallback",
                         "message": message, "fallback": fallback})

    if cutoff >= kickoff or _now() >= kickoff:
        finding("not_current_prematch", "time", "已开赛或截点不在赛前；不得登记新的赛前测试。", hard=True,
                fallback="historical_review_only")

    source_ids, frozen_record, inputs, frozen_by_path = set(), None, {}, {}
    frozen = checks.get("evidence_bundle", {})
    try:
        bundle = _path(root, frozen.get("path"))
        anchor = frozen.get("manifest_sha256")
        if not isinstance(anchor, str) or not re.fullmatch(r"[0-9a-f]{64}", anchor):
            raise PretestError("independent_manifest_anchor_required")
        verified = verify_bundle(bundle, anchor)
        details["evidence"] = verified
        if verified.get("status") != "verified":
            raise PretestError("frozen_bundle_integrity_failed")
        frozen_record = json.loads((bundle / "record.json").read_bytes())
        if (frozen_record.get("match_key") != record.get("match_key")
                or frozen_record.get("version_id") != record["version_id"]
                or _time(frozen_record["analysis_cutoff"]) != cutoff
                or _time(frozen_record["kickoff_at"]) != kickoff):
            raise PretestError("frozen_identity_or_cutoff_mismatch")
        loaded = load_analysis_inputs(bundle, anchor)
        inputs = loaded["inputs"]
        source_ids = set(inputs)
        frozen_by_path = {s["original_path"]: inputs[s["source_id"]] for s in frozen_record["sources"]}
        required = frozen.get("required_source_ids")
        if not _refs(required, source_ids):
            raise PretestError("declared_required_input_missing")
        review = frozen.get("content_review", {})
        if (frozen.get("completeness_status") != "declared_complete"
                or review.get("status") != "reviewed" or review.get("no_result_content") is not True
                or not _refs(review.get("evidence_refs"), source_ids)):
            finding("input_semantic_completeness_unknown", "G3", "白名单字节可核，资料范围或赛果夹带语义尚未复核。",
                    fallback="no_prospective_comparison_claim")
        if verified.get("qualification") != "prospective_input_candidate":
            finding("input_not_prospective", "G3", "冻结包仅有研究资格，不能升级为前瞻样本。",
                    fallback="research_only")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        numerical = False
        details["evidence_error"] = _failure_code(exc)
        finding("evidence_unverified", "G3", "同截点白名单输入未完成完整性及真实时点核验。",
                fallback="qualitative_with_disclosed_missing_evidence")

    model = checks.get("model", {})
    packet = None
    if model.get("status") in {"not_applicable", "unavailable"} and model.get("reason"):
        numerical = False
        details["model"] = {"status": model["status"], "reason": model["reason"], "recomputed": False}
        finding("model_unavailable_or_inapplicable", "G2", "无适用模型只限制量化，定性判断可继续。",
                block_modes={"f5_confirmation"}, fallback="probabilities_and_ev_unquantified")
    else:
        try:
            packet_bytes = _read_ref(root, model["packet"])
            packet = json.loads(packet_bytes)
            receipt = json.loads(_read_ref(root, model["execution_receipt"]))
            if (packet.get("packet_type") != "saved_exploratory_forecast_for_framework"
                    or packet.get("status") != "baseline_verified_external_checks_pending"
                    or packet.get("provenance", {}).get("read_and_recomputed") is not True
                    or packet["provenance"].get("fit_performed") is not False):
                raise PretestError("actual_integration_packet_required")
            argv = receipt.get("argv", [])
            if (receipt.get("exit_code") != 0 or not isinstance(argv, list)
                    or ["-m", "football_model.integration", "match"] != argv[1:4]
                    or receipt.get("packet_sha256") != _hash(packet_bytes)
                    or _time(receipt["started_at"]) > _time(receipt["completed_at"])
                    or _time(packet.get("created_at")) > _time(receipt["completed_at"])
                    or _time(receipt["completed_at"]) >= kickoff
                    or _time(receipt["completed_at"]) > _now()):
                raise PretestError("integration_execution_receipt_invalid")
            fixture = packet["fixture"]
            if (str(fixture["match_id"]) != str(record["match_id"])
                    or fixture["home_id"] != record.get("home_id")
                    or fixture["away_id"] != record.get("away_id")
                    or _time(fixture["kickoff"]) != kickoff
                    or _time(packet["analysis_as_of"]) > cutoff):
                raise PretestError("integration_match_identity_or_cutoff_mismatch")
            for field in ("source_commit", "state_commit"):
                if (not re.fullmatch(r"[0-9a-f]{40}", str(model.get(field, "")))
                        or (field == "state_commit" and packet["provenance"].get(field) != model[field])
                        or receipt.get(field) != model[field]):
                    raise PretestError("fixed_source_or_state_commit_mismatch")
            prediction_source = packet["provenance"].get("source_commit")
            if (not re.fullmatch(r"[0-9a-f]{40}", str(prediction_source or ""))
                    or model.get("prediction_source_commit") != prediction_source):
                raise PretestError("prediction_source_commit_missing_or_mismatched")
            actions = model.get("actions", {})
            if (actions.get("status") != "checked" or not _refs(actions.get("evidence_refs"), source_ids)
                    or actions.get("failure_scope") not in {"none", "other_leagues_only"}
                    or _time(actions.get("checked_at")) > cutoff):
                raise PretestError("latest_target_or_global_attempt_not_cleared")
            runtime = _path(root, model["runtime_path"])
            config_path = model["config"]["path"]
            config_bytes = _read_ref(root, model["config"])
            required_paths = {config_path}
            relative_runtime = model["runtime_path"]
            for field in ("model_file", "prediction_file", "report_file", "venue_registry_snapshot"):
                name = packet["provenance"].get(field)
                if name:
                    if not name.startswith("runtime/"):
                        raise PretestError("unsupported_runtime_reference")
                    required_paths.add(relative_runtime + "/" + name[len("runtime/"):])
            required_paths.add(relative_runtime + "/integration/index.json")
            required_paths.add(relative_runtime + "/reports/history/" + packet["latest_league_attempt"]["run_id"] + ".json")
            for name in required_paths:
                if name not in frozen_by_path or _path(root, name).read_bytes() != frozen_by_path[name]:
                    raise PretestError("integration_raw_input_not_in_frozen_allowlist")
            rerun = match_packet(runtime, json.loads(config_bytes), model["league"],
                                 int(record["match_id"]), as_of=packet["analysis_as_of"],
                                 state_commit=model["state_commit"])
            if (rerun["prediction"] != packet["prediction"] or rerun["fixture"] != fixture
                    or rerun["provenance"] != packet["provenance"]):
                raise PretestError("saved_packet_recomputation_mismatch")
            details["model"] = {"status": "recomputed", "packet_sha256": _hash(packet_bytes),
                                "model_id": packet["provenance"]["model_id"],
                                "declared_recomputation_source_commit": model["source_commit"],
                                "actual_runtime_source_commit_verified": False,
                                "actual_runtime_implementation_sha256": {
                                    name: _hash(Path(__file__).with_name(name).read_bytes())
                                    for name in ("integration.py", "model.py", "pretest.py")},
                                "prediction_source_commit": prediction_source, "state_commit": model["state_commit"],
                                "integration_original_function_rerun": True,
                                "external_fixture_checks": packet.get("external_checks", {})}
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            numerical = False
            details["model_error"] = _failure_code(exc)
            finding("model_not_currently_verified", "G2", "真实单场复算、固定原件或最近相关失败范围未通过。",
                    block_modes={"f5_confirmation"}, fallback="probabilities_and_ev_unquantified")

    freshness = checks.get("freshness", {})
    try:
        if (freshness.get("coverage_status") != "complete"
                or freshness.get("scope") != "model_supported_competition_and_requested_seasons"
                or not _refs(freshness.get("expected_schedule_source_ids"), source_ids)):
            raise PretestError("expected_schedule_completeness_unknown")
        teams = freshness.get("teams")
        if (not isinstance(teams, list) or len(teams) != 2
                or {t.get("team_id") for t in teams} != {record.get("home_id"), record.get("away_id")}):
            raise PretestError("both_teams_freshness_required")
        data_reference = freshness["training_data"]
        data_bytes = _read_ref(root, data_reference)
        if frozen_by_path.get(data_reference["path"]) != data_bytes:
            raise PretestError("training_data_not_frozen")
        if packet is None:
            raise PretestError("actual_model_training_digest_unknown")
        data = json.loads(data_bytes)
        fitted_cutoff = _time(packet["provenance"]["training_cutoff"])
        validated = _validated_matches(data["matches"])
        fitted_rows = [m for m in validated if m["_dt"] + RESULT_AVAILABILITY_DELAY <= fitted_cutoff]
        serial = [{k: v for k, v in m.items() if k != "_dt"} for m in fitted_rows]
        digest = _hash(json.dumps(serial, sort_keys=True, ensure_ascii=False).encode())
        if digest != packet["provenance"]["training_data_sha256"]:
            raise PretestError("training_rows_do_not_match_actual_fitted_model")
        team_checks = []
        for team in teams:
            expected = team.get("expected_match_ids")
            if not _positive_ids(expected) or not _refs(team.get("evidence_refs"), source_ids):
                raise PretestError("expected_team_match_ids_and_sources_required")
            actual = sorted(m["match_id"] for m in fitted_rows if team["team_id"] in (m["home_id"], m["away_id"]))
            if not expected:
                raise PretestError("empty_expected_scope_requires_independent_semantic_resolution")
            if set(actual) - set(expected):
                raise PretestError("declared_complete_expected_scope_omits_actual_training_matches")
            missing = sorted(set(expected) - set(actual))
            # Expected IDs are eligible completed matches by ANALYSIS cutoff,
            # within the declared model scope, not every game in every event.
            team_checks.append({"team_id": team["team_id"], "expected_match_ids": expected,
                                "actual_model_match_ids": actual, "missing_match_ids": missing})
        details["freshness"] = {"status": "complete" if all(not t["missing_match_ids"] for t in team_checks)
                                else "missing_expected_matches", "teams": team_checks,
                                "actual_training_data_sha256": digest, "file_mtime_used": False,
                                "expected_scope_verification": "human_attestation_not_authenticated",
                                "status_scope": "declared_expected_ids_compared_with_digest_verified_training_rows"}
        if details["freshness"]["status"] != "complete":
            numerical = False
            finding("expected_matches_not_in_model", "G2", "截至截点应有的合格比赛未全部入模；不能称资料齐全。",
                    block_modes={"f5_confirmation"}, fallback="use_disclosed_historical_baseline_only_no_current_ev_claim")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        numerical = False
        details["freshness"] = {"status": "unknown", "reason": _failure_code(exc), "file_mtime_used": False}
        finding("freshness_completeness_unknown", "G2", "两队应有比赛或实际入模指纹无法核齐，保留未知。",
                block_modes={"f5_confirmation"}, fallback="no_current_data_completeness_or_quantitative_value_claim")

    market = checks.get("quotes", {})
    try:
        row_bytes = _read_ref(root, market["rows"])
        if frozen_by_path.get(market["rows"]["path"]) != row_bytes:
            raise PretestError("quote_rows_not_in_frozen_inputs")
        rows = json.loads(row_bytes)
        selection = select_quotes(rows, record["analysis_cutoff"])
        details["quotes"] = {"selection": selection, "isolated_row_ids": excluded_rows,
                             "current_open_verified": False, "execution_verified": False}
        reviewed = market.get("row_reviews", [])
        reviews = {r["row_id"]: r for r in reviewed}
        usable = []
        for group in selection["groups"]:
            row = group["as_of_latest"]
            if row is None or group["status"] not in {"open", "reference_only"}:
                if group["blocking_row"] or row is not None:
                    excluded_rows.append((group["blocking_row"] or row)["row_id"])
                continue
            review = reviews.get(row["row_id"], {})
            if (review.get("source_kind") not in {"screenshot", "api", "sourced_page"}
                    or row.get("observed_at") is None or _time(row["observed_at"]) > cutoff):
                excluded_rows.append(row["row_id"])
                continue
            api_ah = _unverified_api_asian_handicap(row) or (review.get("source_kind") == "api" and row["market"] == "asian_handicap")
            if api_ah:
                excluded_rows.append(row["row_id"])
                finding("api_ah_sign_unverified_isolated", "G3", "API亚洲盘符号未独立认证，本行隔离。",
                        block_modes=set(), fallback="use_other_verified_rows_or_unpriced_qualitative_analysis")
                continue
            is_image = review.get("source_kind") == "screenshot"
            if (review.get("whole_visible_history_reviewed") is not True
                    or review.get("whole_row_and_odds_format_verified") is not True
                    or not _refs(review.get("evidence_refs"), source_ids)
                    or (is_image and (review.get("second_image_reviewed") is not True
                                      or row.get("image_id") not in source_ids
                                      or next((s["role"] for s in (frozen_record or {}).get("sources", [])
                                               if s["source_id"] == row.get("image_id")), None) != "screenshot"))):
                excluded_rows.append(row["row_id"])
                continue
            # Converted away AH line is derived by the existing adapter, never guessed.
            adapted = adapt_quote_row(row, match_id=str(record["match_id"]))
            if not adapted["contracts"] or adapted.get("line_mapping_status") == "quarantined_unverified_api_asian_handicap":
                excluded_rows.append(row["row_id"])
                continue
            usable.append({"row_id": row["row_id"], "row_sha256": adapted["row_sha256"],
                           "contracts": adapted["contracts"], "offers": adapted["offers"]})
        details["quotes"]["usable_rows"] = usable
        details["quotes"]["review_is_human_attestation"] = True
        if not usable:
            finding("no_verified_price_row", "G3", "没有完整、回核且适用的报价行；不填价格EV或开放/成交。",
                    block_modes={"framework_comparison", "f5_confirmation"}, fallback="unpriced_qualitative_analysis")
        selected = market.get("selected_row_ids", [])
        if (not isinstance(selected, list) or any(row not in {u["row_id"] for u in usable} for row in selected)):
            finding("selected_quote_row_is_not_verified", "G3", "采用行未通过整行/符号/时点核验，应撤除本行价格。",
                    hard=True, fallback="remove_affected_quote")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        details["quote_error"] = _failure_code(exc)
        finding("quotes_unverified", "G3", "报价整行与截点选择尚未通过。",
                block_modes={"framework_comparison", "f5_confirmation"}, fallback="unpriced_qualitative_analysis")

    semantic = checks.get("semantic_review", {})
    try:
        archive = json.loads(_read_ref(root, semantic["decision_archive"]))
        audit = validate_decision_record(archive)
        details["decision_structure"] = audit
        if audit["status"] == "blocked":
            finding("decision_structural_contradiction", "G5", "正文投影或合约存在明确结构矛盾，须修正重验。", hard=True)
        elif audit["status"] != "structural_checks_passed":
            finding("decision_structure_incomplete", "G5", "旧字段或记录不足，不能标结构检查全部通过。",
                    fallback="disclose_not_evaluated_checks")
        if str(archive.get("match_id")) != str(record["match_id"]):
            raise PretestError("decision_match_mismatch")
        if not any(v.get("version_id") == record["version_id"] for v in archive.get("versions", [])):
            raise PretestError("decision_version_mismatch")
        decision_bundle = semantic.get("decision_bundle")
        if decision_bundle is not None:
            bundle_path = _path(root, decision_bundle["path"])
            bundle_result = verify_decision_bundle(bundle_path, decision_bundle["manifest_sha256"])
            actual_decision = json.loads((bundle_path / "decision.json").read_bytes())
            if (str(actual_decision.get("match_id")) != str(record["match_id"])
                    or actual_decision.get("version_id") != record["version_id"]
                    or _time(actual_decision.get("analysis_cutoff")) != cutoff
                    or _time(actual_decision.get("kickoff_at")) != kickoff):
                raise PretestError("decision_bundle_identity_or_cutoff_mismatch")
            views = json.loads((bundle_path / "audit_archives.json").read_bytes())
            if archive not in views.values():
                raise PretestError("decision_archive_not_generated_by_bound_bundle")
            details["decision_bundle"] = bundle_result
        elif (archive.get("version_id") != record["version_id"]
              or _time(archive.get("analysis_cutoff")) != cutoff
              or _time(archive.get("kickoff_at")) != kickoff):
            raise PretestError("decision_time_binding_missing")
        if decision_bundle is None:
            finding("generated_decision_views_not_verified", "G5", "直接档案字段仅可核声明，尚未回读统一入口生成的正文及全候选视图。",
                    fallback="direct_archive_review_only")
        if (semantic.get("status") != "reviewed"
                or not _refs(semantic.get("evidence_refs"), source_ids)
                or any(semantic.get(key) is not True for key in
                       ("mechanism_supports_payment", "strongest_counterevidence_handled",
                        "nearest_alternative_compared", "all_candidates_match_body", "conditions_not_reminders"))):
            finding("body_semantic_review_incomplete", "G5", "机制、反证、最近备选、条件和全候选原文复核未齐。",
                    fallback="no_complete_semantic_acceptance_claim")
        details["semantic_review_is_human_attestation"] = True
    except (ValueError, TypeError, KeyError, OSError) as exc:
        details["semantic_error"] = _failure_code(exc)
        finding("body_and_contract_unverified", "G5", "正文及合约核查证据缺失。",
                fallback="disclose_review_gap")

    saving = checks.get("save", {})
    try:
        artifacts = saving.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise PretestError("saved_artifacts_required")
        readback = [{"path": item["path"], "sha256": _hash(_read_ref(root, item)),
                     "bytes": len(_read_ref(root, item))} for item in artifacts]
        details["save"] = {"local_readback_status": "verified", "artifacts": readback,
                           "remote_persistence_status": "not_verified_by_this_offline_gate"}
    except (ValueError, TypeError, KeyError, OSError) as exc:
        details["save_error"] = _failure_code(exc)
        finding("save_readback_not_verified", "G5", "原件/输出未完成实际保存读回指纹核验。",
                fallback="do_not_claim_saved_or_archived")
    capacity = saving.get("capacity", {})
    try:
        state_root = _path(root, capacity["state_root"])
        limit = capacity["hard_limit_bytes"]
        reserve = capacity["reserve_bytes"]
        if not state_root.is_dir() or type(limit) is not int or not 0 < limit <= MAX_PHYSICAL_BYTES or type(reserve) is not int or not 0 <= reserve < limit:
            raise PretestError("capacity_limit_and_root_required")
        validate_runtime(state_root)
        native = estimate_runtime(state_root)
        used, files = native["raw_bytes"], native["file_count"]
        effective_limit = limit
        if mode == "f5_confirmation":
            from .shadow_models import STORAGE_LIMIT_BYTES
            effective_limit = min(effective_limit, STORAGE_LIMIT_BYTES)
        headroom = effective_limit - reserve - native["persisted_bytes"]
        growth, days = capacity.get("observed_daily_growth_bytes"), capacity.get("planned_days")
        projected = None
        if (type(growth) in {int, float} and math.isfinite(growth) and growth >= 0
                and type(days) is int and days > 0 and _refs(capacity.get("growth_evidence_refs"), source_ids)):
            projected = growth * days
        details["capacity"] = {"measured_bytes": used, "measured_files": files,
                               "hard_limit_bytes": limit, "reserve_bytes": reserve,
                               "current_headroom_bytes": headroom, "projected_growth_bytes": projected,
                               "native_estimate": native, "effective_write_limit_bytes": effective_limit,
                               "logical_limit_bytes": MAX_LOGICAL_BYTES,
                               "long_cycle_supported": None,
                               "projection_status": "diagnostic_only_not_a_retention_or_funding_proof",
                               "actual_state_tree": None,
                               "measurement_scope": "declared_local_restored_runtime_not_authenticated_complete_remote_state"}
        if capacity.get("state_tree"):
            details["capacity"]["actual_state_tree"] = validate_storage_tree(_path(root, capacity["state_tree"]))
        if headroom <= 0:
            finding("capacity_reserve_exhausted", "G5", "当前保存容量已侵占预留；须先整理无损归档。",
                    fallback="defer_new_test_persistence")
        if mode == "f5_confirmation":
            finding("long_cycle_capacity_not_demonstrated", "G5", "确认期增长证据或保存安排尚不能支持整个周期。",
                    block_modes={"f5_confirmation"}, fallback="do_not_activate_confirmation")
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        details["capacity"] = {"status": "unknown", "reason": _failure_code(exc)}
        finding("capacity_not_measured", "G5", "未实测保存范围与容量，长期能力保持未知。",
                fallback="no_long_cycle_storage_claim")

    # Unknown publication/execution is allowed; assertions require separately
    # verified append-event evidence, not file creation or a reference price.
    for name in ("publication", "execution"):
        state = checks.get(name, {})
        if state.get("status", "unknown") != "unknown":
            finding(name + "_claim_needs_event_verification", "G3", "发表或交易声明须另按追加事件核原凭证，当前不升级。",
                    block_modes=set(), fallback=name + "_unknown")
    details["publication"] = {"status": "unknown", "occurred_at": None}
    details["execution"] = {"status": "unknown", "occurred_at": None, "actual_profit": None}

    if mode in {"framework_comparison", "f5_confirmation"}:
        purpose = "comparison_plan" if mode == "framework_comparison" else "confirmation_protocol"
        plan = checks.get(purpose, {})
        try:
            from .pretest_protocols import read_preparation, check_activation
            prepared = read_preparation(_path(root, plan["path"]), plan["manifest_sha256"])
            content = prepared["protocol"]
            preparation_path = plan["path"] + "/preparation.json"
            if frozen_by_path.get(preparation_path) != _path(root, preparation_path).read_bytes():
                raise PretestError("evaluation_preparation_reference_not_in_frozen_inputs")
            if _time(prepared["frozen_at"]) > cutoff:
                raise PretestError("evaluation_preparation_after_cutoff")
            if purpose == "comparison_plan":
                policies = prepared["policies"]
                if (content.get("schema_version") != "s2-framework-comparison-prepared-1.0"
                        or set(policies) != {"left", "right"}
                        or policies["left"]["policy_id"] == policies["right"]["policy_id"]):
                    raise PretestError("independent_comparison_plan_incomplete")
                details[purpose] = {"status": "frozen_launch_plan", "independent_runs_observed": False,
                                   "preparation_manifest_sha256": plan["manifest_sha256"],
                                   "policies": policies,
                                   "model_control": "same_frozen_model_and_forecast_with_equal_disclosed_fallbacks"}
            else:
                if plan.get("activation_request"):
                    request = json.loads(_read_ref(root, plan["activation_request"]))
                    details["activation_review"] = check_activation(_path(root, plan["path"]),
                        plan["manifest_sha256"], request, evidence_root=root)
                # The current capture pipeline explicitly keeps pilot eligibility
                # false. A preparation or a ready-for-activation review is never
                # an immutable actual activation/capture registration.
                raise PretestError("actual_confirmation_capture_activation_not_implemented")
        except (ValueError, TypeError, KeyError, OSError) as exc:
            details[purpose] = {"status": "not_ready", "reason": _failure_code(exc)}
            finding(purpose + "_not_ready", "evaluation", "预定比较或确认协议尚未满足本模式启动要求。",
                    block_modes={mode}, fallback="ordinary_or_flow_pilot_only")

    blocked = any(f["status"] == "blocked" for f in findings)
    return {"schema_version": VERSION, "mode": mode, "match_id": record["match_id"],
            "version_id": record["version_id"], "checked_at": _now().isoformat().replace("+00:00", "Z"),
            "status": "blocked" if blocked else "ready_with_local_fallbacks" if findings else "engineering_checks_passed",
            "findings": findings, "details": details,
            "numerical_use": "current_baseline_only" if numerical else "unquantified_or_disclosed_historical_only",
            "qualitative_model_absence_is_not_veto": True,
            "publication_status": "unknown", "actual_execution_status": "unknown",
            "predictive_quality_verified": False, "economic_advantage_verified": False,
            "new_match_runtime_acceptance_observed": False,
            "human_attestations_independently_authenticated": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="分模式核验开测证据；不联网、不训练、不下注。")
    parser.add_argument("--record", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--mode", choices=sorted(MODES))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        source = Path(args.record).read_bytes()
        result = assess_pretest(json.loads(source), source_root=args.source_root, mode=args.mode)
        result["input_record_sha256"] = _hash(source)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        data = _json_bytes(result)
        with output.open("xb") as stream:
            stream.write(data)
        readback = output.read_bytes()
        if readback != data:
            raise PretestError("output_readback_failed")
        print(json.dumps({"status": result["status"], "output_sha256": _hash(readback),
                          "output_readback_verified": True, "mode": result["mode"],
                          "new_match_runtime_acceptance_observed": False}, ensure_ascii=False))
        return 2 if result["status"] == "blocked" else 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "rejected", "reason": _failure_code(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
