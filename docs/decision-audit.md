# 决策发布校验与同输入重复检查

此模块把框架§11—12、§25的部分明确约束变成可执行检查。它不调用语言模型，不选边，不设统一EV门槛，不修改生产概率，不以一致性证明预测正确。真正的独立重复分析须由执行者在独立上下文中运行并保留全部输出；复制一个JSON、改run_id，不构成独立执行证据。

## 使用

```bash
python -m football_model.decision_audit validate --record record.json --output decision-audit-r1.json
python -m football_model.decision_audit repeat --inputs run1.json run2.json run3.json --output decision-repeat.json
```

Python接口为 `validate_decision_record(record)` 和 `compare_decision_runs(records)`，均返回JSON兼容对象且不修改输入。命令只创建新的输出文件，拒绝覆盖原档或已有输出；审计记录含实际输入文件SHA-256。

发布校验结果：

| 状态 | 含义 |
|---|---|
| `blocked` | 至少一个明确结构矛盾，应修正后重跑；保留旧报告与失败审计 |
| `incomplete` | 暂未发现明确矛盾，但旧字段或输入不足，相关检查为 `not_evaluated` |
| `structural_checks_passed` | 此处检查的字段声明一致；仍需人工语义及最终报告核对，不等于允许自动投注 |

`validate`完整结构通过退出0，blocked/incomplete退出2。`repeat`输入可比较退出0，不能比较退出2；**repeat的退出0不表示每次决策无违规**，必须查看逐运行的 `rule_violations_by_run` / `not_evaluated_by_run`。

## 兼容字段

保留 `schema_version="match-archive-1.0"`，新记录追加 `decision_audit_version="decision-audit-1.0"`。既有归档字段不删除。缺失新字段的旧档按实际可检查范围审计，输出不足，不回填旧概率、条件、引文或原选择。

稳定ID全包唯一，支持可解析的 `sources/source_id`、`facts/fact_id`、`contracts/contract_id`、`quotes/quote_id`、`candidates/candidate_id`、`conditions/condition_id`、`model_records/model_record_id`、`calculation_records/calculation_record_id`、`versions/version_id`。`*_ref`/`*_refs`引用上述记录；外部记录需在 `external_references[]` 提供 `reference_id`、固定URL和description，记录其未打包限制。此模块检查引用关系，原件内容、哈希、可访问性由证据包核验另行检查。

| 位置 | 新审计明确使用的字段 |
|---|---|
| contract | `market=1x2/ah/ou`、`selection=home/draw/away/over/under`（须匹配市场）、`line`（1x2为null；其余有限四分之一球数值）、`settlement_basis` |
| quote | `contract_id`、`source_id`、`decimal_odds`、`status`；若重复记录市场/方向/线/范围，须与contract一致 |
| candidate | `version_id`、`contract_id`、`quote_id`、`fit`、`support_refs`、`mechanism`、`payment_requirement`、`counterevidence`、`condition_ids`、`model_record_ids`、`value` |
| condition | `state=satisfied/not_satisfied/unknown`（或中文三态）、`critical`布尔值、`observable`、`actions`（三态分别记录动作） |
| value | `status=unquantified/unclear/confirmed_positive/negative`、`ev`、`breakeven_odds`、`calculation_record_id`、`disclosed_model_record_ids` |
| model record | 本次候选价值评估绑定 `candidate_id/version_id`；`applicable`、`status`、`probability_role=F/MIX/MKT`；有依据的明确负面评估另标 `value_assessment=valid_negative` |
| uncertainty | model record上记录 `uncertainty_assessed`、`uncertainty_support`、`uncertainty_refs`、`validation_refs`；`confirmed_positive`须有正EV、`uncertainty_support=positive`及可解析依据 |
| calculation | 绑定 `model_record_id/contract_id/quote_id`，保存 `ev/breakeven_odds`与原计算凭证；此模块只核字段对应，不取代真实五态计算 |
| version | `primary_candidate_id`、`summary_decision`、`body_decision`；没有主方向须保留实际 `no_primary_reason` |

`fit`仍为“适合／条件适合／不建议／暂无法判断”。没有模型或xG并不否决定性适合；无关键条件用空 `condition_ids`，不虚构“满足”。没有量化时，`ev/breakeven_odds/calculation_record_id`为null，不是0。`status=unclear`可保留已复算正或负点估计，不能改称已证明优势。

报价状态使用 `open_reference`（清楚的最新公开参考，不代表账户已接受）、`accepted`、`closed`、`superseded`、`hypothetical`、`unknown`；其他值不能默认为有效价。普通首发未知若不是改变该票判断的关键条件，不会由程序自动阻断。`counterevidence`必须为数组，各项记录 `source_refs`、`handling`、`reason`；处理或理由缺失记未评估，内容是否完整、处理是否合理仍需阅读原文判断。合约或报价绑定为null时保留未知并判资料不足，即使该票为“不建议”也不冒充完整核验。

同一基础比分模型可供多个候选使用，但**模型是否对某价格有明确不利必须按候选、报价、版本绑定评估记录**；不要把一个候选的负EV标记套给其他票，或从被选候选列表删除不利模型来逃避披露。

`summary_decision`与`body_decision`均包括 `candidate_id/fit/contract_id/quote_id/condition_ids/value_status`。它们应来自最终报告的同一结构化渲染入口；若只是手填副本，程序只能证明副本一致。未结构化的正文、自由文本、HTML内容仍须实际打开核对。不同版本可以合理改变方向，但每一版本的主卡、正文、摘要须自洽，不拿r2覆盖r1。

## 仅字段结构的演示

以下为虚构演示，来源/机制文字、价格和指纹均不是比赛事实，不能直接用作真实预测。

```json
{
  "schema_version": "match-archive-1.0",
  "decision_audit_version": "decision-audit-1.0",
  "match_id": "demo",
  "sources": [{"source_id": "s1", "url": "https://example.invalid/demo"}],
  "contracts": [{"contract_id": "ct1", "market": "ah", "selection": "home", "line": -0.25, "settlement_basis": "REGULATION"}],
  "quotes": [{"quote_id": "q1", "contract_id": "ct1", "source_id": "s1", "status": "open_reference", "decimal_odds": 1.95}],
  "candidates": [{
    "candidate_id": "c1", "version_id": "r1", "contract_id": "ct1", "quote_id": "q1",
    "fit": "适合", "support_refs": ["s1"],
    "mechanism": "演示文字；真实报告须有人工作语义核验",
    "payment_requirement": "演示支付要求",
    "counterevidence": [{"source_refs": ["s1"], "handling": "retained", "reason": "演示处理"}],
    "condition_ids": [], "model_record_ids": [],
    "value": {"status": "unquantified", "ev": null, "breakeven_odds": null, "calculation_record_id": null, "disclosed_model_record_ids": []}
  }],
  "conditions": [], "model_records": [], "calculation_records": [],
  "versions": [{
    "version_id": "r1", "primary_candidate_id": "c1",
    "summary_decision": {"candidate_id": "c1", "fit": "适合", "contract_id": "ct1", "quote_id": "q1", "condition_ids": [], "value_status": "unquantified"},
    "body_decision": {"candidate_id": "c1", "fit": "适合", "contract_id": "ct1", "quote_id": "q1", "condition_ids": [], "value_status": "unquantified"}
  }]
}
```

## 重复检查协议

每次真实独立运行的record另带：

```json
{
  "decision_context": {
    "run_id": "实际独立运行ID",
    "version_id": "本次版本ID",
    "evidence_bundle_sha256": "实际冻结输入清单或输入包的64位小写SHA-256",
    "rules_sha256": "实际系统/框架/图规/提示输入的64位小写SHA-256",
    "model_settings_sha256": "实际语言模型版本/采样设置/统计模型固定版本等清单的64位小写SHA-256"
  }
}
```

1. 先冻结输入；用证据包核验实际字节与清单。三个指纹必须真实生成、相同，并绑定同场比赛；不能为了合并实验手写相同指纹。输出文件不纳入输入包指纹，否则不同判断本身会使输入不同。
2. 独立上下文逐次运行，使用相同提示和输入，记录真实run_id；执行过程中不读取赛果或前次决定。次数与停止规则事先固定，不在看到某个方向后停止；保留失败与违规输出。
3. `repeat`拒绝指纹不一致、缺少身份/版本/主方向、重复run_id的输入。主方向比较完整的 `market/selection/line/settlement_basis`，不同本地candidate/contract ID不当作方向差异。
4. 报告主方向**两两一致率**（一致运行对数／全部运行对数）、适配和价格价值状态分布、每次规则违规与未评估数。违规运行仍保留以暴露问题；没有多数投票选边或自动更改正式基线。
5. 全部重复运行只计一场独立比赛。不使用赛果挑选最佳一次，也不能将“3/3一致”解释为预测正确率100%。本文提供检查器和执行协议，未声称已经完成真实GPT一致性样本实验。

## 尚需人工审查

自动程序可发现“有记录却互相矛盾”，不能确认来源是真的、遗漏反证已经齐全、文字机制足够支持盘口、不确定性分析具有说服力。`structural_checks_passed`始终附 `manual_review_required`，且 `predictive_quality_verified=false`、`economic_advantage_verified=false`。假数据自洽仍可能形式通过，必须和原文/原图、证据包、真实模型调用及最终阅读报告一起审查。
