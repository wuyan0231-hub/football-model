"""Offline structural decision checks; no football judgement or automatic selection.

The optional decision-audit-1.0 extension leaves match-archive-1.0 intact.
Missing legacy fields remain not_evaluated. Formal consistency never establishes
football evidence sufficiency, predictive skill, or economic value.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Any

VERSION = "decision-audit-1.0"
FITS = {"适合", "条件适合", "不建议", "暂无法判断"}
VALUES = {"unquantified", "unclear", "confirmed_positive", "negative"}
ID_FIELDS = {
    "sources": "source_id", "facts": "fact_id", "quotes": "quote_id",
    "contracts": "contract_id", "candidates": "candidate_id",
    "conditions": "condition_id", "model_records": "model_record_id",
    "calculation_records": "calculation_record_id", "versions": "version_id",
    "external_references": "reference_id",
}
CONTRACT_FIELDS = ("market", "selection", "line", "settlement_basis")
CONTEXT_FIELDS = ("evidence_bundle_sha256", "rules_sha256", "model_settings_sha256")
MANUAL_CHECKS = [
    "逐项阅读原文/原图，确认事实来源真实且本场、时点、语义适用；ID可解析不等于事实已核实。",
    "判断支持机制是否足以支持该合约的赢/走/输要求；文本非空不等于足球依据充分。",
    "核查最强反证是否完整、处理是否合理，替代选择和三市场比较是否公平。",
    "核查模型适用性、验证与不确定性依据是否可信；字段声明不替代实际复算。",
    "打开最终HTML/正文核对结构字段，确认未标记的自由文本没有矛盾或遗漏；本程序不解析自然语言。",
]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _has(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_decision_record(record: dict[str, Any]) -> dict[str, Any]:
    """Check resolvable declarations without modifying an archive or inferring facts.

    blocked: a definite structural contradiction; incomplete: missing/legacy
    evidence prevents checks; structural_checks_passed: checked declarations
    agree but manual semantic review is still required. No state means suitable.
    """
    issues: list[dict[str, str]] = []
    checks: list[dict[str, str]] = []

    def issue(code: str, path: str, message: str, severity: str = "error") -> None:
        item = {"code": code, "path": path, "message": message, "severity": severity}
        if item not in issues:
            issues.append(item)

    def need(obj: dict, fields: tuple | list, path: str) -> None:
        for key in fields:
            if key not in obj:
                issue("missing_field", path + "." + key, "缺少明确记录，未执行此项判定。", "not_evaluated")

    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    if record.get("schema_version") != "match-archive-1.0":
        issue("legacy_schema", "schema_version", "非match-archive-1.0记录，仅检查明确给出的字段。", "not_evaluated")
    if record.get("decision_audit_version") != VERSION:
        issue("legacy_decision_fields", "decision_audit_version", "旧记录未声明本次审计字段，不能声称完整检查。", "not_evaluated")

    tables: dict[str, dict[str, dict]] = {}
    refs: dict[str, dict] = {}
    for block, id_field in ID_FIELDS.items():
        rows = record.get(block, [])
        if not isinstance(rows, list):
            issue("invalid_array", block, "记录块必须为数组。")
            rows = []
        tables[block] = {}
        for i, row in enumerate(rows):
            path = f"{block}[{i}]"
            if not isinstance(row, dict):
                issue("invalid_record", path, "记录必须为对象。")
                continue
            key = row.get(id_field)
            if not isinstance(key, str) or not key:
                issue("missing_id", path + "." + id_field, "缺少非空稳定ID。", "not_evaluated")
                continue
            if key in refs:
                issue("duplicate_id", path + "." + id_field, "稳定ID重复，引用不可唯一解析。")
            tables[block][key] = row
            refs[key] = row
            if block == "external_references" and not (row.get("url") and row.get("description")):
                issue("external_reference_incomplete", path, "外部记录需要明确URL及说明。", "not_evaluated")

    def resolve(key: Any, block: str, path: str) -> dict:
        if not isinstance(key, str) or key not in tables[block]:
            issue("unresolved_reference", path, f"无法解析到{block}中的记录。")
            return {}
        return tables[block][key]

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                here = path + "." + key
                typed_block = next((block for block, field in ID_FIELDS.items() if field == key), None)
                if typed_block is not None and value is not None:
                    resolve(value, typed_block, here)
                elif key.endswith("_refs") and value is not None:
                    if not isinstance(value, list):
                        issue("invalid_references", here, "引用集合必须为数组。")
                    else:
                        for ref in value:
                            if not isinstance(ref, str) or ref not in refs:
                                issue("unresolved_reference", here, "来源或证据引用无法解析。")
                elif key.endswith("_ref") and value is not None:
                    if not isinstance(value, str) or value not in refs:
                        issue("unresolved_reference", here, "来源或证据引用无法解析。")
                walk(value, here)
        elif isinstance(obj, list):
            for i, value in enumerate(obj):
                walk(value, f"{path}[{i}]")

    walk(record, "record")
    candidates = tables["candidates"]
    if not candidates:
        issue("no_candidates", "candidates", "无可审计候选，不推定分析通过。", "not_evaluated")

    for cid, candidate in candidates.items():
        path = f"candidates[{cid}]"
        need(candidate, ("version_id", "contract_id", "quote_id", "fit", "support_refs",
                         "mechanism", "payment_requirement", "counterevidence", "condition_ids",
                         "model_record_ids", "value"), path)
        fit = candidate.get("fit")
        if fit is None:
            issue("unknown_fit", path + ".fit", "未记录适配状态，不能视为完成发布核验。", "not_evaluated")
        if fit is not None and (not isinstance(fit, str) or fit not in FITS):
            issue("invalid_fit", path + ".fit", "适配只允许原四态。")
        if candidate.get("version_id") is not None:
            resolve(candidate["version_id"], "versions", path + ".version_id")
        for key in ("contract_id", "quote_id"):
            if candidate.get(key) is None:
                issue("unknown_candidate_binding", path + "." + key, "合约或报价绑定未知，不能声称完整核验通过。", "not_evaluated")
        contract = resolve(candidate["contract_id"], "contracts", path + ".contract_id") if candidate.get("contract_id") is not None else {}
        quote = resolve(candidate["quote_id"], "quotes", path + ".quote_id") if candidate.get("quote_id") is not None else {}
        if contract:
            need(contract, CONTRACT_FIELDS, f"contracts[{contract.get('contract_id')}]")
            market = contract.get("market")
            selections = {"1x2": {"home", "draw", "away"}, "ah": {"home", "away"}, "ou": {"over", "under"}}
            if not isinstance(market, str) or market not in selections:
                issue("unknown_contract_market", path + ".contract_id", "市场编码必须为1x2、ah或ou。")
            elif "selection" in contract and (not isinstance(contract["selection"], str) or contract["selection"] not in selections[market]):
                issue("invalid_contract_selection", path + ".contract_id", "方向与市场类型不匹配。")
            if market in ("ah", "ou") and "line" in contract:
                line = contract["line"]
                if not _number(line) or not math.isclose(line * 4, round(line * 4), abs_tol=1e-9):
                    issue("invalid_contract_line", path + ".contract_id", "线位必须为有限的四分之一球数值。")
            if market == "1x2" and contract.get("line") is not None:
                issue("invalid_contract_line", path + ".contract_id", "胜平负没有让球线，应记null。")
            for key in CONTRACT_FIELDS:
                for source, label in ((candidate, path), (quote, f"quotes[{quote.get('quote_id')}]")):
                    if key in source and key in contract and source[key] != contract[key]:
                        issue("contract_mismatch", label + "." + key, "方向、线位或结算范围与合约不一致。")
        if quote and quote.get("contract_id") != candidate.get("contract_id"):
            issue("quote_contract_mismatch", path + ".quote_id", "报价未绑定候选的同一合约。")
        if quote:
            need(quote, ("status", "decimal_odds", "source_id"), f"quotes[{quote.get('quote_id')}]")
            if quote.get("status") not in (None, "open_reference", "accepted", "closed", "superseded", "hypothetical", "unknown", "封"):
                issue("invalid_quote_status", path + ".quote_id", "未知报价状态不能当作当前有效价。")
            if "decimal_odds" in quote and (not _number(quote["decimal_odds"]) or quote["decimal_odds"] <= 1):
                issue("invalid_quote_odds", path + ".quote_id", "采用的十进制赔率必须为大于1的有限数值。")
            if quote.get("status") in (None, "unknown"):
                issue("unknown_quote_state", path + ".quote_id", "报价状态未明确，未评估当前适用性。", "not_evaluated")
        if fit == "适合" and (not quote or quote.get("status") in ("closed", "superseded", "hypothetical", "封")):
            issue("unsuitable_quote", path, "无有效绑定报价，或使用封盘/已替代/假设价，不能无条件适合。")
        supports = candidate.get("support_refs", [])
        if isinstance(supports, list):
            for ref in supports:
                if isinstance(ref, str) and ref in refs and ref not in tables["sources"] and ref not in tables["facts"] and ref not in tables["external_references"]:
                    issue("invalid_support_reference", path + ".support_refs", "正面事实支持须引用来源/事实，不可循环引用候选结论。")
        if fit in ("适合", "条件适合"):
            for field in ("support_refs", "mechanism", "payment_requirement", "counterevidence"):
                if field in candidate and not _has(candidate[field]):
                    issue("unsupported_fit", path + "." + field, "有利适配缺少已记录支持、机制、支付要求或反证处理。")
        counterevidence = candidate.get("counterevidence")
        if isinstance(counterevidence, list):
            for i, counter in enumerate(counterevidence):
                if not isinstance(counter, dict) or not _has(counter.get("handling")) or not _has(counter.get("reason")):
                    issue("unhandled_counterevidence", f"{path}.counterevidence[{i}]", "已记录反证缺少处理及理由，未完成反证审查。", "not_evaluated")
        elif "counterevidence" in candidate:
            issue("invalid_counterevidence", path + ".counterevidence", "结构化反证必须为数组，不能用字符串或null替代。")

        condition_ids = candidate.get("condition_ids", [])
        if not isinstance(condition_ids, list):
            issue("invalid_conditions", path + ".condition_ids", "条件ID应为数组。")
            condition_ids = []
        unresolved_critical = False
        for condid in condition_ids:
            cond = resolve(condid, "conditions", path + ".condition_ids")
            if not cond:
                continue
            need(cond, ("state", "critical", "observable", "actions"), f"conditions[{condid}]")
            if "state" in cond and cond["state"] not in ("satisfied", "not_satisfied", "unknown", "满足", "不满足", "未知"):
                issue("invalid_condition_state", f"conditions[{condid}].state", "条件必须保留满足/不满足/未知三态。")
            if "critical" in cond and not isinstance(cond["critical"], bool):
                issue("invalid_condition_critical", f"conditions[{condid}].critical", "critical必须为布尔值。")
            if cond.get("critical") is True and cond.get("state") not in ("satisfied", "满足"):
                unresolved_critical = True
                if fit == "适合":
                    issue("unresolved_critical_condition", path, "关键条件未知或未满足，与无条件适合矛盾。")
            if fit == "条件适合":
                actions = cond.get("actions", {})
                if not isinstance(actions, dict) or not all(_has(actions.get(k)) for k in ("satisfied", "not_satisfied", "unknown")) or not cond.get("observable"):
                    issue("unactionable_condition", f"conditions[{condid}]", "条件适合须有可观察标准和三态动作。")
        if fit == "条件适合" and not unresolved_critical:
            issue("missing_critical_condition", path, "条件适合缺少会改变该票的明确未决关键条件。")

        model_ids = candidate.get("model_record_ids", [])
        if not isinstance(model_ids, list):
            issue("invalid_model_ids", path + ".model_record_ids", "模型ID必须为数组。")
            model_ids = []
        models = [resolve(mid, "model_records", path + ".model_record_ids") for mid in model_ids]
        value = candidate.get("value")
        if not isinstance(value, dict):
            issue("missing_value_state", path + ".value", "价格价值须与适配单列。", "not_evaluated")
            for model in tables["model_records"].values():
                if (model.get("candidate_id") == cid and model.get("version_id") == candidate.get("version_id")
                        and model.get("applicable") is True and model.get("value_assessment") == "valid_negative"):
                    issue("hidden_adverse_model", path + ".value", "已有明确适用的不利模型，却未记录价格价值。")
            continue
        need(value, ("status", "ev", "breakeven_odds", "calculation_record_id", "disclosed_model_record_ids"), path + ".value")
        status = value.get("status")
        if status is None:
            issue("unknown_value_state", path + ".value.status", "价格价值状态未知，未完成核验。", "not_evaluated")
        if status is not None and (not isinstance(status, str) or status not in VALUES):
            issue("invalid_value_state", path + ".value.status", "价格价值状态未知。")
        ev, be = value.get("ev"), value.get("breakeven_odds")
        for key, number in (("ev", ev), ("breakeven_odds", be)):
            if number is not None and not _number(number):
                issue("invalid_numeric_value", path + ".value." + key, "数值必须有限；未知用null，不能用字符串或NaN。")
        if _number(be) and be < 1:
            issue("invalid_breakeven", path + ".value.breakeven_odds", "十进制保本赔率不能小于1。")
        if status == "unquantified" and (ev is not None or be is not None):
            issue("unquantified_is_not_zero", path + ".value", "未量化必须保持null，不能填0或其他数值。")
        numeric = ev is not None or be is not None
        if numeric:
            if not models:
                issue("numeric_without_model", path + ".value", "没有引用适用概率模型，不能生成EV或保本价。")
            for model in models:
                need(model, ("candidate_id", "version_id"), path + ".model_record_ids")
                if (("candidate_id" in model and model["candidate_id"] != cid)
                        or ("version_id" in model and model["version_id"] != candidate.get("version_id"))):
                    issue("model_candidate_mismatch", path + ".value", "模型适用/价值评估记录属于其他候选或版本。")
                if model.get("applicable") is not True or model.get("status") not in ("recomputed", "已复算"):
                    issue("inapplicable_model_numeric", path + ".value", "模型未确认适用且已复算，不能支持所报数字。")
                if model.get("probability_role") not in ("F", "MIX"):
                    issue("non_model_probability", path + ".value", "须区分F/MIX预测与MKT同价市场参照。")
            calid = value.get("calculation_record_id")
            calc = resolve(calid, "calculation_records", path + ".value.calculation_record_id")
            if calc:
                if calc.get("contract_id") != candidate.get("contract_id") or calc.get("quote_id") != candidate.get("quote_id"):
                    issue("calculation_contract_mismatch", path + ".value", "计算引用的合约/报价不匹配。")
                if calc.get("model_record_id") not in model_ids:
                    issue("calculation_model_mismatch", path + ".value", "计算使用的模型未在候选中披露。")
                for key, number in (("ev", ev), ("breakeven_odds", be)):
                    if number is not None and (not _number(calc.get(key)) or not _number(number) or not math.isclose(number, calc[key], abs_tol=1e-12, rel_tol=1e-9)):
                        issue("calculation_value_mismatch", path + ".value." + key, "所报数字缺少一致计算记录。")
        if status == "confirmed_positive":
            if not _number(ev) or ev <= 0:
                issue("positive_without_positive_ev", path + ".value", "正向价值声明缺少正EV点估计。")
            if not models or not all(m.get("uncertainty_assessed") is True and m.get("uncertainty_support") == "positive"
                                     and _has(m.get("uncertainty_refs")) and _has(m.get("validation_refs")) for m in models):
                issue("positive_without_uncertainty", path + ".value", "未经有依据的不确定性和验证审查，正点估计只能记优势不清。")
        if status == "negative" and (not _number(ev) or ev >= 0):
            issue("negative_without_negative_ev", path + ".value", "不利价值声明缺少负EV记录。")
        disclosed = value.get("disclosed_model_record_ids", [])
        if not isinstance(disclosed, list):
            issue("invalid_disclosure", path + ".value.disclosed_model_record_ids", "披露ID必须为数组。")
            disclosed = []
        for mid in disclosed:
            resolve(mid, "model_records", path + ".value.disclosed_model_record_ids")
        # Inspect all bound records, not just the cherry-picked model references.
        for mid, model in tables["model_records"].items():
            bound = model.get("candidate_id") == cid and model.get("version_id") == candidate.get("version_id")
            if bound and model.get("applicable") is True and model.get("value_assessment") == "valid_negative":
                if mid not in disclosed or mid not in model_ids or status == "unquantified":
                    issue("hidden_adverse_model", path + ".value", "本版本已记录适用且有充分依据的不利模型，不能省略或退回未量化。")
                if fit in ("适合", "条件适合"):
                    issue("adverse_model_fit_conflict", path + ".fit", "已记录明确适用的绝对收益不利，与有利适配矛盾。")

    versions = tables["versions"]
    if not versions:
        issue("no_versions", "versions", "未记录版本，无法核对正文/摘要一致性。", "not_evaluated")
    for vid, version in versions.items():
        path = f"versions[{vid}]"
        need(version, ("primary_candidate_id", "summary_decision", "body_decision"), path)
        primary = version.get("primary_candidate_id")
        if primary is None:
            if not _has(version.get("no_primary_reason")):
                issue("missing_primary", path, "无主方向须保留身份不明等实际原因。", "not_evaluated")
            continue
        candidate = resolve(primary, "candidates", path + ".primary_candidate_id")
        if not candidate:
            continue
        if candidate.get("version_id") != vid:
            issue("cross_version_primary", path, "本版主方向引用了其他版本候选。")
        expected = {"candidate_id": primary, "fit": candidate.get("fit"), "contract_id": candidate.get("contract_id"),
                    "quote_id": candidate.get("quote_id"), "condition_ids": candidate.get("condition_ids"),
                    "value_status": candidate.get("value", {}).get("status") if isinstance(candidate.get("value"), dict) else None}
        for field in ("summary_decision", "body_decision"):
            rendered = version.get(field)
            if not isinstance(rendered, dict):
                issue("missing_rendered_decision", path + "." + field, "缺少正文或摘要字段，不能声称核对一致。", "not_evaluated")
                continue
            need(rendered, tuple(expected), path + "." + field)
            for key, target in expected.items():
                if key in rendered and rendered[key] != target:
                    issue("rendered_decision_mismatch", path + "." + field + "." + key, "正文/摘要与同版结构记录不一致。")
    errors = sum(i["severity"] == "error" for i in issues)
    missing = sum(i["severity"] == "not_evaluated" for i in issues)
    checks.append({"check": "manual_semantic_review", "status": "required"})
    return {"schema_version": VERSION, "status": "blocked" if errors else "incomplete" if missing else "structural_checks_passed",
            "error_count": errors, "not_evaluated_count": missing, "issues": issues, "checks": checks,
            "candidate_count": len(candidates), "version_count": len(versions),
            "manual_review_required": MANUAL_CHECKS, "predictive_quality_verified": False,
            "economic_advantage_verified": False, "source_record_modified": False}


def compare_decision_runs(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare one decision per run only when declared frozen inputs coincide.

    Agreement uses all pairs, not a majority vote. The function does not choose a
    winner and repeated runs never add independent matches to a validation set.
    """
    if not isinstance(records, list) or len(records) < 2:
        raise ValueError("at least two independently recorded runs are required")
    audits = [validate_decision_record(r) for r in records]
    reasons: list[str] = []
    identities, directions, fits, values, run_ids = [], [], [], [], []
    for i, record in enumerate(records):
        context = record.get("decision_context", {})
        if not isinstance(context, dict):
            context = {}
        if not all(_hash(context.get(key)) for key in CONTEXT_FIELDS):
            reasons.append(f"run[{i}]缺少完整证据包/规则/模型设置指纹")
        identities.append(tuple(context.get(key) for key in CONTEXT_FIELDS))
        runid = context.get("run_id")
        if not isinstance(runid, str) or not runid or runid in run_ids:
            reasons.append(f"run[{i}]缺少独立且唯一run_id")
        run_ids.append(runid)
        versions = record.get("versions", [])
        version = next((v for v in versions if isinstance(v, dict) and v.get("version_id") == context.get("version_id")), None) if isinstance(versions, list) else None
        if version is None:
            reasons.append(f"run[{i}]未指定待比较版本")
            directions.append(None)
            fits.append(None)
            values.append(None)
            continue
        candidates = record.get("candidates", [])
        primary = next((c for c in candidates if isinstance(c, dict) and c.get("candidate_id") == version.get("primary_candidate_id")), None) if isinstance(candidates, list) else None
        if primary is None:
            reasons.append(f"run[{i}]主方向未形成或不可解析")
            directions.append(None)
            fits.append(None)
            values.append(None)
            continue
        if primary.get("fit") not in ("适合", "条件适合", "不建议", "暂无法判断"):
            reasons.append(f"run[{i}]适配状态缺失或非法")
        contracts = record.get("contracts", [])
        contract = next((c for c in contracts if isinstance(c, dict) and c.get("contract_id") == primary.get("contract_id")), {}) if isinstance(contracts, list) else {}
        if not all(key in contract for key in CONTRACT_FIELDS):
            reasons.append(f"run[{i}]合约字段不足")
        direction = {key: contract.get(key) for key in CONTRACT_FIELDS}
        # JSON 0 and 0.0 (or -0.0) describe the same legal line, not a new vote.
        if _number(direction["line"]):
            direction["line"] = 0.0 if direction["line"] == 0 else float(direction["line"])
        directions.append(_digest(direction))
        fits.append(primary.get("fit"))
        value_state = primary.get("value", {}).get("status") if isinstance(primary.get("value"), dict) else None
        if value_state not in ("unquantified", "unclear", "confirmed_positive", "negative"):
            reasons.append(f"run[{i}]价格价值状态缺失或非法")
        values.append(value_state)
    if any(identity != identities[0] for identity in identities[1:]):
        reasons.append("证据包、规则或模型设置不同，不能合并为同输入重复实验")
    match_ids = [r.get("match_id") for r in records]
    if any(mid is None for mid in match_ids) or any(mid != match_ids[0] for mid in match_ids[1:]):
        reasons.append("比赛身份不一致或缺失")
    comparable = not reasons
    count = len(records)
    pairs = count * (count - 1) // 2
    equal = sum(directions[i] == directions[j] for i in range(count) for j in range(i + 1, count)) if comparable else None
    return {"schema_version": VERSION, "status": "comparable" if comparable else "not_comparable",
            "run_count": count, "independent_match_count": 1 if comparable else None,
            "not_comparable_reasons": reasons,
            "primary_direction_pairwise_agreement": equal / pairs if comparable else None,
            "pair_count": pairs if comparable else None,
            "fit_distribution": dict(Counter(fits)) if comparable else None,
            "value_distribution": dict(Counter(values)) if comparable else None,
            "rule_violations_by_run": [a["error_count"] for a in audits],
            "not_evaluated_by_run": [a["not_evaluated_count"] for a in audits],
            "audits": audits, "selected_direction": None, "predictive_quality_verified": False,
            "warnings": ["同输入资格依赖真实冻结包与指纹核验；本命令比较声明，不能鉴别虚构指纹。",
                         "一致性不是正确率或收益证据；不得多数投票自动选边或事后按赛果挑选运行。",
                         "同一比赛重复分析只计一场，违规与资料不足运行保留在报告。"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate")
    check.add_argument("--record", required=True, type=Path)
    repeat = commands.add_parser("repeat")
    repeat.add_argument("--inputs", required=True, nargs="+", type=Path)
    for sub in (check, repeat):
        sub.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    paths = [args.record] if args.command == "validate" else args.inputs
    if args.output.resolve() in {p.resolve() for p in paths}:
        parser.error("output must not overwrite an input archive")
    try:
        blobs = [p.read_bytes() for p in paths]
        records = [json.loads(b) for b in blobs]
        result = validate_decision_record(records[0]) if args.command == "validate" else compare_decision_runs(records)
        result["input_files"] = [{"name": p.name, "sha256": hashlib.sha256(b).hexdigest()} for p, b in zip(paths, blobs)]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"output": str(args.output), "status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] in {"structural_checks_passed", "comparable"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
