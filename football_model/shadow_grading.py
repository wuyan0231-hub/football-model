"""Validate optional S2 shadow grades without choosing or changing a formal fit.

The validator checks declarations and references, not football semantics.  Grades
are independently recorded for a single bet and a candidate parlay leg; neither
can establish a whole-ticket grade, a probability, an EV, or a real execution.
Legacy reports are never upgraded or rewritten by this module.
"""
from __future__ import annotations

from typing import Any, Collection


VERSION = "s2-shadow-grading-1.0"
GRADES = frozenset({"A", "B", "C", "D", "U"})
ACTIONS = frozenset({"candidate", "watch_only", "not_recommended", "unable_to_judge", "awaiting_condition"})
CHAIN_FIELDS = ("support", "payment_region", "strongest_counterargument", "counterargument_handling", "strongest_alternative")
MANUAL_CHECKS = [
    "回读原文，确认支持、支付边界、最强反证与真实替代比较实质成立；非空文本或可解析ID不证明语义充分。",
    "分别复核单关与串关腿的声明用途、主要普通分支及风险分布；不得机械把串关降一级。",
    "无模型、无xG、普通首发未知和未提供其他串关腿均不自动降级；定性A/B不对应固定胜率或正EV。",
    "关键条件满足仅触发按当时有效报价和资料重新判断；不得继承原先预想等级。",
    "历史材料的新影子复判只是回溯开发，缺新字段不证明旧报告缺依据，也不能补成原赛前等级。",
]


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_shadow_grading(
    record: Any,
    *,
    candidate: dict[str, Any] | None = None,
    conditions: list[dict[str, Any]] | None = None,
    evidence_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Check a separately versioned shadow record; never mutate any argument.

    ``None`` means no optional shadow evaluation was supplied (not a failed or
    downgraded formal recommendation).  A provided record binds ``decision_id``
    and two views (``single``, ``parlay_leg``), each with candidate/contract/offer
    IDs, explicit action, grade, objective, reason and condition IDs. A/B require
    a complete decision-chain declaration and reason/ref objects. ``candidate``
    accepts the archive's ``quote_id`` alias for the selected ``quote_offer_id``.

    References may point to archive evidence, frozen inputs or derived artifacts;
    resolution proves existence only. With no supplied reference/condition index
    the corresponding checks remain incomplete. A result never means suitable.
    """
    issues: list[dict[str, str]] = []

    def issue(code: str, path: str, message: str, severity: str = "error") -> None:
        issues.append({"code": code, "path": path, "message": message, "severity": severity})

    def result(status: str | None = None) -> dict[str, Any]:
        errors = sum(i["severity"] == "error" for i in issues)
        missing = sum(i["severity"] == "not_evaluated" for i in issues)
        return {
            "validator_version": VERSION,
            "status": status or ("blocked" if errors else "incomplete" if missing else "structural_checks_passed"),
            "issues": issues,
            "error_count": errors,
            "not_evaluated_count": missing,
            "manual_review_required": list(MANUAL_CHECKS),
            "formal_decision_modified": False,
            "source_record_modified": False,
            "automatic_grade_inheritance": False,
            "historical_grade_backfilled": False,
            "whole_ticket_grade_inferred": False,
            "predictive_quality_verified": False,
            "economic_advantage_verified": False,
        }

    if record is None:
        return result("not_evaluated")
    if not isinstance(record, dict):
        issue("invalid_shadow_record", "shadow_grading", "影子记录须为对象；未评估可保留null。")
        return result()

    for key in ("decision_id", "policy_id", "policy_version"):
        if not _text(record.get(key)):
            issue("missing_shadow_identity", key, "影子决定须绑定独立决定ID和明确政策版本。", "not_evaluated")
    if record.get("mode") != "shadow":
        issue("shadow_is_not_formal", "mode", "本模块只核影子等级，不能替代正式四态判断。")
    context = record.get("evaluation_context")
    if not isinstance(context, str) or context not in {"current", "retrospective_development"}:
        issue("invalid_shadow_context", "evaluation_context", "须区分当前判断与回溯开发，不能回填原历史等级。")
    if context == "retrospective_development":
        for key in ("original_report_modified", "historical_grade_claim"):
            if record.get(key) is not False:
                issue("historical_grade_backfill", key, "历史新复判须明确不改原报告、不主张这是原历史等级。")
    elif record.get("original_report_modified") is True or record.get("historical_grade_claim") is True:
        issue("historical_grade_backfill", "evaluation_context", "新影子记录不能自动回填原报告或原历史等级。")

    if candidate is not None and not isinstance(candidate, dict):
        issue("invalid_shadow_candidate", "candidate", "待绑定正式候选须为对象。")
        candidate = None
    if candidate is None:
        issue("unverified_shadow_binding", "candidate", "未提供正式候选，不能核对影子是否绑定同一具体票。", "not_evaluated")
    elif candidate.get("decision_id") is not None and record.get("decision_id") != candidate["decision_id"]:
        issue("shadow_decision_mismatch", "decision_id", "影子等级属于其他决定。")

    known_refs: set[str] | None = None
    if evidence_ids is not None:
        if isinstance(evidence_ids, (str, bytes)) or not isinstance(evidence_ids, Collection) or any(not _text(x) for x in evidence_ids):
            issue("invalid_shadow_evidence_index", "evidence_ids", "引用索引须由非空ID组成。")
        else:
            known_refs = set(evidence_ids)

    def refs(value: Any, path: str) -> None:
        if not isinstance(value, list) or not value or any(not _text(x) for x in value):
            issue("missing_shadow_evidence", path, "需引用实际依据；空栏目不能获得语义通过。", "not_evaluated")
            return
        if len(value) != len(set(value)):
            issue("duplicate_shadow_reference", path, "重复ID不构成多个独立依据。")
        if known_refs is None:
            issue("unverified_shadow_reference", path, "未提供引用索引，不能确认依据可解析。", "not_evaluated")
        elif any(x not in known_refs for x in value):
            issue("unknown_shadow_reference", path, "影子引用不在本决定的证据、输入或派生产物清单中。")

    def argument(value: Any, path: str) -> None:
        if not isinstance(value, dict):
            issue("missing_shadow_argument", path, "须记录实质理由及原文引用。", "not_evaluated")
            return
        if not _text(value.get("reason")):
            issue("missing_shadow_reason", path + ".reason", "没有实质理由不能由非空字段代替。", "not_evaluated")
        refs(value.get("refs"), path + ".refs")

    condition_index: dict[str, dict[str, Any]] = {}
    conditions_known = isinstance(conditions, list)
    if conditions is not None and not conditions_known:
        issue("invalid_shadow_conditions", "conditions", "条件索引须为数组；未检查不能伪装为空数组。")
    for i, condition in enumerate(conditions if conditions_known else []):
        if not isinstance(condition, dict) or not _text(condition.get("condition_id")):
            issue("invalid_shadow_condition", f"conditions[{i}]", "条件需要可解析ID。")
            continue
        cid = condition["condition_id"]
        if cid in condition_index:
            issue("duplicate_shadow_condition", f"conditions[{i}]", "条件ID重复，不能确定作用版本。")
        if not isinstance(condition.get("critical"), bool):
            issue("unknown_shadow_condition_criticality", f"conditions[{i}].critical", "条件影响是否关键须明确，不能将未知当非关键。", "not_evaluated")
        if condition.get("state") not in ("satisfied", "not_satisfied", "unknown", "满足", "不满足", "未知"):
            issue("invalid_shadow_condition_state", f"conditions[{i}].state", "条件须保留满足、不满足或未知。")
        condition_index[cid] = condition

    for use in ("single", "parlay_leg"):
        view = record.get(use)
        if not isinstance(view, dict):
            issue("missing_shadow_use", use, "单关与串关候选腿须各自记录，不从另一项自动推导。", "not_evaluated")
            continue
        for key in ("candidate_id", "contract_id", "quote_offer_id", "objective", "reason"):
            if not _text(view.get(key)):
                issue("missing_shadow_field", use + "." + key, "影子等级须绑定具体票、用途和理由。", "not_evaluated")
        if candidate is not None:
            for key in ("candidate_id", "contract_id", "quote_offer_id"):
                expected = candidate.get(key)
                if key == "quote_offer_id" and expected is None:
                    expected = candidate.get("quote_id")
                if not _text(expected):
                    issue("unverified_shadow_binding", use + "." + key, "正式候选缺少绑定ID。", "not_evaluated")
                elif view.get(key) != expected:
                    issue("shadow_candidate_mismatch", use + "." + key, "影子等级不能贴到不同候选、合约或报价。")
        grade, action = view.get("grade"), view.get("action_status")
        if not isinstance(action, str) or action not in ACTIONS:
            issue("invalid_shadow_action", use + ".action_status", "行动状态须独立填写，不由等级隐式推导。")
        if action == "awaiting_condition":
            if grade is not None:
                issue("conditional_grade_not_null", use + ".grade", "待条件单独表示，当前不能预挂A/B或其他等级。")
        elif not isinstance(grade, str) or grade not in GRADES:
            issue("invalid_shadow_grade", use + ".grade", "非待条件须明确记录A/B/C/D/U，不得推断等级。")
        if ((grade == "C" and action != "watch_only")
                or (grade == "D" and action != "not_recommended")
                or (grade == "U" and action != "unable_to_judge")
                or (grade in ("A", "B") and action not in ("candidate", "watch_only"))):
            issue("shadow_grade_action_conflict", use + ".action_status", "明确填写的行动与等级含义矛盾；校验器不替调用者改写任一项。")
        for key in ("fixed_hit_rate", "fixed_win_probability", "fixed_ev", "grade_probability", "grade_ev"):
            if view.get(key) is not None:
                issue("fixed_grade_quantification", use + "." + key, "影子等级不能绑定固定命中率或EV；独立合格估计另记。")
        if view.get("grade_inherited") is True:
            issue("automatic_grade_inheritance", use + ".grade_inherited", "条件确认或更新后不能自动继承旧等级。")

        condition_ids = view.get("condition_ids")
        if not isinstance(condition_ids, list) or any(not _text(x) for x in condition_ids):
            issue("missing_shadow_condition_ids", use + ".condition_ids", "须显式记录本票条件ID；已检查无条件可用空数组。", "not_evaluated")
            condition_ids = []
        if not conditions_known:
            issue("unverified_shadow_conditions", use + ".condition_ids", "未提供条件索引，不能证明无关键未决。", "not_evaluated")
        relevant_ids = set(condition_ids)
        if candidate is not None:
            formal_ids = candidate.get("condition_ids", [])
            if isinstance(formal_ids, list) and all(_text(x) for x in formal_ids):
                relevant_ids.update(formal_ids)
            else:
                issue("invalid_formal_condition_ids", use + ".condition_ids", "正式候选条件ID异常，不能绕过其关键条件。")
            # Purpose-specific conditions supplement shared conditions; an
            # explicitly empty scope can never erase a common critical fact.
            scoped_ids = candidate.get("formal_condition_ids", {})
            if not isinstance(scoped_ids, dict):
                issue("invalid_formal_condition_ids", use + ".condition_ids", "用途专属条件须按single/parlay_leg记录。")
            else:
                scope = scoped_ids.get(use, [])
                if isinstance(scope, list) and all(_text(x) for x in scope):
                    relevant_ids.update(scope)
                else:
                    issue("invalid_formal_condition_ids", use + ".condition_ids", "用途专属条件ID异常，不能绕过关键条件。")
        unresolved: list[dict[str, Any]] = []
        for cid in relevant_ids:
            condition = condition_index.get(cid)
            if condition is None:
                issue("unknown_shadow_condition", use + ".condition_ids", "条件引用不可解析，不能绕过关键未决。")
                continue
            if condition.get("critical") is True and condition.get("state") not in ("satisfied", "满足"):
                unresolved.append(condition)
        if grade in ("A", "B") and unresolved:
            issue("unresolved_shadow_condition", use + ".grade", "关键条件未满足或未知，不能给当前A/B。")
        if action == "awaiting_condition":
            if not unresolved:
                issue("no_unresolved_shadow_condition", use, "待条件须有真实未决且影响本票的条件。")
            for condition in unresolved:
                actions = condition.get("actions")
                if condition.get("observable") is not True or not isinstance(actions, dict) or not all(_text(actions.get(k)) for k in ("satisfied", "not_satisfied", "unknown")):
                    issue("unactionable_shadow_condition", use + ".condition_ids", "待条件须可核查，并记录满足/不满足/未知三种动作。")

        if grade in ("A", "B"):
            chain = view.get("decision_chain")
            if not isinstance(chain, dict):
                issue("missing_shadow_chain", use + ".decision_chain", "A/B须声明决定链完整并保留依据。", "not_evaluated")
            else:
                if chain.get("complete") is not True:
                    issue("incomplete_shadow_chain", use + ".decision_chain.complete", "A/B需要已完成决定链的声明；仍须独立语义复核。", "not_evaluated")
                for field in CHAIN_FIELDS:
                    argument(chain.get(field), use + ".decision_chain." + field)
            argument(view.get("failure_path"), use + ".failure_path")
            if grade == "A":
                argument(view.get("ordinary_branch_robustness"), use + ".ordinary_branch_robustness")
                argument(view.get("distinguishing_evidence"), use + ".distinguishing_evidence")
                argument(view.get("weakest_assumption"), use + ".weakest_assumption")
            else:
                argument(view.get("not_a_reason"), use + ".not_a_reason")
        elif grade == "C":
            if view.get("c_reason") not in ("evidence_gap", "purpose_mismatch"):
                issue("missing_c_category", use + ".c_reason", "C须区分证据环节未完成和不符当前用途，不能当小注建议。")
            argument(view.get("limitation"), use + ".limitation")
        elif grade == "D":
            argument(view.get("rejection_basis"), use + ".rejection_basis")
        elif grade == "U":
            if not _text(view.get("unknown_reason")):
                issue("missing_u_reason", use + ".unknown_reason", "U须解释哪个必要事实无法判断；不代表概率比D更低。", "not_evaluated")

    single, leg = record.get("single"), record.get("parlay_leg")
    if isinstance(single, dict) and isinstance(leg, dict):
        if (single.get("grade"), single.get("action_status")) != (leg.get("grade"), leg.get("action_status")):
            difference = record.get("difference_reason")
            if not isinstance(difference, dict) or not _text(difference.get("objective_difference")) or not _text(difference.get("risk_distribution_reason")):
                issue("unexplained_shadow_difference", "difference_reason", "单串差级须说明用途和风险分布，不能机械降一级。", "not_evaluated")
            else:
                refs(difference.get("refs"), "difference_reason.refs")

    reassessment = record.get("reassessment")
    transition = record.get("previous_action_status") == "awaiting_condition"
    if transition or reassessment is not None:
        if not isinstance(reassessment, dict):
            issue("missing_shadow_reassessment", "reassessment", "由待条件转为新判断须有重新评价记录。")
        else:
            if reassessment.get("completed") is not True or reassessment.get("inherited_grade") is not False:
                issue("automatic_grade_inheritance", "reassessment", "新等级须来自重新评价，不能自动延续预想等级。")
            if not _text(reassessment.get("previous_decision_id")) or reassessment.get("previous_decision_id") == record.get("decision_id"):
                issue("invalid_reassessment_parent", "reassessment.previous_decision_id", "重评须追加新决定并引用旧决定，不覆盖旧版本。")
            if reassessment.get("decision_id") != record.get("decision_id"):
                issue("shadow_decision_mismatch", "reassessment.decision_id", "重评绑定的新决定ID不一致。")
            argument(reassessment.get("basis"), "reassessment.basis")
            if candidate is not None:
                for key in ("contract_id", "quote_offer_id"):
                    expected = candidate.get(key, candidate.get("quote_id") if key == "quote_offer_id" else None)
                    if not _text(expected) or reassessment.get(key) != expected:
                        issue("stale_shadow_reassessment", "reassessment." + key, "重评须检查本次合约与报价，不能继承旧价下的等级。")
    return result()
