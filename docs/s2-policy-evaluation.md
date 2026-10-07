# F3 同截点、同票政策比较

本入口消费真实保存的决定评分包，不生成候选、不补等级，不重新计算模型。主范围是同一具体票的正式适配与影子等级／行动比较。独立换票、整个串关、自动策略晋级尚未实现。原严格报价300秒／预测3600秒协议不修改。

## 先预注册，再读取实际输出

```bash
python -m football_model.policy_evaluation register --spec policy-spec.json --root runtime/policy_evaluation/protocols
python -m football_model.policy_evaluation compare --root runtime/policy_evaluation/protocols --protocol-id ACTUAL_ID --inputs actual-score-references.json --output NEW_REPORT.json
```

`register` 使用真实程序时钟，协议按原字节SHA-256命名且禁止覆盖。没有历史 `--as-of`。登记时只要已有请求截点过去，整个协议标记 `research_only`；仍可开发复核，不能倒填成前瞻。后续增加请求或改政策须新协议，不能修改已冻结分母。协议资格仅说明登记时序，每一对输出还须分别核对真实决定时间和评分资格。

规范对象须包含：

| 字段 | 要求 |
|---|---|
| `experiment_id/scope_description` | 实验身份与连续纳入范围，保留所有请求；不能赛后仅提交已推荐场次作为完整范围 |
| `comparison` | 固定为 `same_ticket_grading_and_action` |
| `version_selection` | 固定为 `first_actual_output_per_request`，最早失败也保留，不看结果挑后版 |
| `quote_protocol/usage` | `QUOTE_REF/QUOTE_OPEN/QUOTE_ACTUAL`之一；`single/parlay_leg`之一 |
| `abstention_zero_exposure` | 显式布尔值，是否在完整可结算纸面票上将实际主动弃权记为零敞口 |
| `primary_objective/allowed_degradation/review_schedule/stopping_rule` | 事先声明主要目标、允许退化、复查节点和停止规则；本模块不代替效果验收 |
| `policies.left/right` | `policy_id/view`，其中 `view=formal/shadow`；影子另须 `policy_version/included_grades`，本轮仅允许事先选择A/B |
| `requests` | 非空固定请求全集；每条有 `request_id/competition/match_id/home_id/away_id/kickoff/analysis_cutoff`，明确时区且截点早于开赛 |

未来输入和报价的原字节尚未产生，协议先冻结采样范围和截点，实际配对时核对两侧真实保存的共同输入池和报价池指纹。不得提前编造未来指纹。只有协议事先登记、实际决定在开赛前生成、输入资格合格且未用于历史开发的一对，才能进入前瞻分组。过去40场始终保留开发性质。

`actual-score-references.json` 是如下列表；这里的占位路径不能作为真实数据使用：

```json
[
  {
    "request_id": "真实预注册请求ID",
    "left": [{"path": "真实评分包目录", "manifest_sha256": "真实清单SHA256"}],
    "right": [{"path": "真实评分包目录", "manifest_sha256": "真实清单SHA256"}]
  }
]
```

两侧通过 `decision_scoring.read_decision_score` 实际验指纹。可以引用同一评分包中已存在的正式适配与影子等级；这不是两次独立LLM运行，也不是两场比赛。缺影子输出保持缺件，不根据正式标签猜影子等级。

实际失败也可提供 `kind="failure"`、原文件路径及 `sha256`；原件须有对应 `request_id/policy_id/status="failed"/reason/completed_at`。所有失败和后续重试都保留；最早实际输出原则包括失败。若同一时钟精度下两个真正不同决定无法排序，标记 `earliest_output_order_ambiguous`，不能用文件名排序冒充真实先后。完全重复指纹不增加版本数。

协议固定 `score_revision_policy="explicit_single_score_revision_for_selected_decision"`。先按 `source.decision_manifest_sha256` 聚合同一实际决定，再选首个决定；同一决定的不同结果修订／评分包不增加 `actual_output_versions`，另列 `score_artifact_versions` 并保留全部评分和结果指纹。首个决定同时传入多个评分修订而未显式选定一个时，保守返回 `multiple_score_revisions_require_explicit_selection`。调用方选择两侧相同、合格的单一结果修订重新计算后即可继续；旧比较报告仍保留。不同结果修订不能按收益选优，也不按摘要值大小自动选一个。

所有预注册请求进入报告。未提交记录显示missing；多余或重复请求ID、来源损坏及比赛身份冲突使运行报错，不静默丢弃。文件指纹不能证明调用方没有遗漏其他版本，因此保留该限制；有完整发布日志时应另核版本清单。

## 分层比较，避免混合分母

同票流程配对核对比赛、截点、基础输入池、可用报价池、实际合约及单票、相同模型指纹和采用预测版本。允许两侧明确均未调用模型的合法定性票继续比较行动与票收益；概率保持空。模型绑定未知与明确无模型不同。

概率、五态、收益各自判断可比性。缺费用或价格只阻断收益，不删除本可比较的概率记录。收益还须同价格、同成本、同结果修订。开放模拟另核同一开放证据指纹及其取得时点不晚于共同截点；不同真实成交不能当成同一模拟机会。实际成交协议不为未交易方构造零收益。

覆盖分组：`common`两侧纳入、`restored_coverage`仅新侧纳入、`lost_coverage`仅旧侧纳入、`both_abstain`双方明确弃权、`unpaired`缺件／失败／关键条件未知／口径不一致。这些是行动覆盖，不能替代收益、概率的可比性及实际评分数量。

正式“适合”映射候选，“不建议”映射主动弃权，“条件适合”保持待条件，“暂无法判断”保持未知。影子只消费实际行动：A/B与事先包含等级共同决定纳入；D明确不建议、C明确 `purpose_mismatch` 可弃权；C的 `evidence_gap`、U及缺件不补成弃权。条件满足须新版本重评，不自动升为候选。

主动弃权的零敞口仅适用于预注册允许、报价协议为参考或开放模拟、**该具体票已经具备非空合格票收益**的情况。没有赛果、报价、费用、条件或执行证据时仍为null。两侧纳入率、请求数、实际输出版本数、独立比赛数分别保存。

## 区间与效果边界

分别输出1X2 Brier／Log loss、五态 Brier／Log loss、票单位收益与策略单位收益的配对差。指标消费已评分原件，未知不补零。模型原概率为0的无限数学Log loss及地板计数留在评分原件；有限 `log_loss` 字段为既有显示地板值，不能解读为数学无穷问题消失。

每个指标先取同一比赛内各配对请求差值的均值，再对比赛等权平均；按 `competition+match_id` 聚类重采样。固定seed `20261007`、2000次bootstrap、95%百分位区间。相同比赛多个截点、合约和版本不伪装成独立比赛；不同截止请求可同时留存，区间仍按比赛聚类。单场不提供区间；空集指标为null。小样本区间不稳定，30场只诊断流程，无自动显著性、准确率或盈利验收线。

`prospective_paired_metrics` 与 `research_paired_metrics` 独立，另有明确命名的全部描述性结果。不能用研究结果替代前瞻改善声明。报告保留每项有效配对行数与独立比赛数；已知结果用于开发的材料即使遮住赛果，也不能因此恢复干净验收资格。

全部协议与报告追加保存，旧决定、原失败、旧主方向、价格和结果修订不改写。回退仅停用候选政策／新比较入口，不能删除不利样本。
