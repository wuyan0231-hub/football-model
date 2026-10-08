# F4 独立选票对照：从真实输入到双版本决定

本入口补齐 F3 的范围：F3 原 `policy_evaluation` 仍比较同一张票的正式／影子行动；`decision_trials` 比较两份实际独立产生的正式决定，允许主方向、市场、线位不同。两边使用同场、同截点、同目标任务、同事实输入池、同报价池、同模型及实际采用预测。正式生产规则不因登记实验自动替换，独立候选政策写自己的 `policy_id`。

它不运行 LLM，不生成分析、分级、赔率或投注。执行者必须先按两份冻结政策分别完成真实分析，再提交实际输出；复制旧票后改政策名称不能声称第二次独立判断。同样选择可以是独立结果，但不同 `run_id`、不同 `decision_id` 和不同原件本身不能证明思维独立或未接触未来信息。

## 生命周期与命令

1. 截点到来前登记实验，包括固定请求全集、每个请求目标、规则原件和 F3 评分协议。
2. 到截点时，创建一个没有候选、没有主票的 F2 中立证据包；只包含当时实际取得的信息、所有可用报价和适用模型。打开机会将其冻结。
3. 两边分别从同一机会运行。每边提交真实运行原件，内含自己实际生成的 F2 输入对象，或明确弃权／失败。
4. 开赛后，按已冻结的 F3 协议对每边实际采用的主票单独评分；读取实际评分包并比较。

```sh
python -m football_model.decision_trials register --spec trial-spec.json --source-root evidence --root runtime/decision_trials
python -m football_model.decision_trials open --root runtime/decision_trials --protocol-id PROTOCOL_SHA --request-id REQUEST_ID --bundle neutral-input-bundle --manifest-sha256 COMMON_SHA
python -m football_model.decision_trials submit --root runtime/decision_trials --protocol-id PROTOCOL_SHA --request-id REQUEST_ID --side left --run left-run.json --run-sha256 RAW_RUN_SHA --source-root evidence
python -m football_model.decision_trials submit --root runtime/decision_trials --protocol-id PROTOCOL_SHA --request-id REQUEST_ID --side right --run right-run.json --run-sha256 RAW_RUN_SHA --source-root evidence
python -m football_model.decision_trials compare --root runtime/decision_trials --protocol-id PROTOCOL_SHA --scores actual-score-references.json --output NEW_REPORT.json
```

以上大写值是占位符；实际执行必须使用工具返回的原件摘要，不得把示例当成已经登记。Python 接口为 `register_trial`、`open_opportunity`、`submit_run`、`compare_trials`，参数与 CLI 一致。`read_trial`验证已登记协议及政策／评分原件。均使用真实系统时钟，无历史 `--as-of`。

## 实验协议字段

`register --spec` 需要如下字段，不能赛后增删原协议的请求。新增批次另建协议，沿用预先固定的采样规则；不把只推荐的场次作为全部请求。

| 字段 | 约束 |
|---|---|
| `experiment_id`、`scope_description`、`opportunity_sampling_rule` | 实验身份、连续纳入范围、实际采样方法 |
| `comparison` | `independent_ticket_selection` |
| `primary_objective`、`allowed_degradation`、`review_schedule`、`stopping_rule` | 预先定义目标、代价、检查节点及停止规则；不会自动解释为晋级门槛 |
| `target`、`usage`、`quote_protocol` | `REGULATION`；`single`或`parlay_leg`；`QUOTE_REF/QUOTE_OPEN/QUOTE_ACTUAL`之一 |
| `model_control` | `same_frozen_model_and_forecast`；本模块用于框架选票试验，模型增量另试验 |
| `unit_stake` | 1，已选择票按每机会1单位；不根据赛果改投入 |
| `abstention_zero_exposure` | 显式布尔值，是否把真实明确弃权计为零敞口 |
| `version_selection` | `first_registered_actual_output_including_failure` |
| `policies.left/right` | 各有不同的 `policy_id` 及 `definition={path,sha256}`；原件相对 `source_root` |
| `scoring_protocol` | 已经登记的 F3 评分协议原件 `{path,sha256}`；报价用途、用途和单位投入必须匹配 |
| `requests` | 非空固定全集；每条含 `request_id,competition,match_id,home_id,away_id,kickoff,analysis_cutoff,target_task` |

`target_task`明确双方在回答同一个问题，例如“从输入包三市场内选择一张常规时间主票，并评估单关适配”。同比赛、同截点、同目标任务不得换请求ID重复计数。两个市场专项可以各设目标，但仍按比赛聚类，不能当作两场。

政策原件和评分协议原始字节随协议保存。评分协议须覆盖全部请求，费用未知继续为空；不同评分协议不混合。代码或规则输入可以与共同包一致，或引用该侧已登记政策原件，不能将新事实伪装为额外 `code/rules` 输入绕过同池控制。

## 中立共同证据包

沿用 `decision_record build` 构造并验证 F2 原件。`primary_candidate_id=null`、`archive.candidates=[]`，并写明 `no_primary_reason`为“共同输入机会，尚未执行政策选择”。它不是第三份推荐。模型实际已采用时保留有效 `forecast_id/model_records/forecast_evidence_ids`；无法适用时明确无模型及原因。外部输入、派生父链、时间、报价角色全部沿用 F2。

打开机会时冻结完整原始 F2 包，并记录实际 `opened_at`。两侧运行必须晚于打开机会。保留所有报价行，不能让两边各自删去未选市场后称作共同报价池。外部事实角色、来源ID、实际观测时间、原字节指纹，以及报价全行、模型和预测指纹均匹配；派生预测及其全部父链的实际字节、计算时间、变换说明同样固定。仅仅 `forecast_id` 相同不足以通过。

过去截点的协议、赛后打开或提交、未知／已接触未来信息都保留为研究，不升级为前瞻。预注册本身不是预测已经发布的证据。实际发布、用户收到及真实交易仍用原事件系统追加。

## 真实运行原件

`submit --run` 指向 UTF-8 JSON，摘要核对通过后保存原字节。公共字段为：

```json
{
  "schema_version": "s2-independent-decision-run-1.0",
  "request_id": "实际请求ID",
  "run_id": "本次真实独立运行ID",
  "producer": "真实执行者或实际运行系统",
  "policy_id": "该侧冻结政策ID",
  "policy_definition_sha256": "该侧政策原件摘要",
  "opportunity_manifest_sha256": "open返回的机会清单摘要",
  "generation_mode": "independent_output",
  "started_at": "真实带时区开始时间",
  "completed_at": "真实带时区输出完成时间",
  "future_information_exposure": false,
  "kind": "decision",
  "output_record": {}
}
```

`output_record`必须替换为本次实际完整 F2 决定对象。它与公共运行信息里的比赛、政策、截点和暴露声明相符。`submit`实际创建并核验 F2 包、匹配共同池，不是只存一个预测编号。工具的真实 `registered_at/committed_at`独立于执行者声明时刻；不能通过填写过去时间获取赛前资格。

如果真实分析明确决定不选票，写 `kind="abstention"`，移除 `output_record`，写 `reason`和 `abstention_type="purpose_mismatch"`或`"no_qualifying_contract"`。没有输出、资料缺失、失败或不知道是否适合，不是主动弃权。失败写 `kind="failure"`和真实 `reason`，不伪造决定对象。

同一机会的 `run_id`不能再次使用。第一份已登记实际输出（包括失败或写到一半的失败槽）为比较对象，后来重试保留但不替换。序号断裂使比较报错；崩溃后仍可继续登记新版本。重试不能覆盖原失败。此规则只能观察已经登记的运行；调用方须及时登记全部实际尝试，不能隐藏未登记的失败。

## 评分与比较

对 `submit`返回的 `path/decision`用原 `decision_scoring` 评分，外部清单摘要用 `decision_manifest_sha256`；费用、常规比分和其原始赛果证据由 F3 校验。传入比较器的文件是：

```json
[
  {"request_id":"实际ID","side":"left","path":"实际左侧评分目录","manifest_sha256":"实际SHA"},
  {"request_id":"实际ID","side":"right","path":"实际右侧评分目录","manifest_sha256":"实际SHA"}
]
```

没有评分时传 `[]`即可查看全部请求的缺件和当前决定覆盖。每侧只选一个明确赛果修订；新评分包不改变原决定或增加预测数。评分来源必须绑定首份实际决定及已登记 F3 协议，不能把重试、他场或另一组成本口径接上来。

| 分组 | 含义 |
|---|---|
| `common` | 两侧实际都选择候选，可以不同票 |
| `restored_coverage` | 左侧明确放弃、右侧选择 |
| `lost_coverage` | 左侧选择、右侧明确放弃 |
| `both_abstain` | 两侧都明确放弃，仍在请求全集中 |
| `unpaired` | 一侧缺失／失败／未知／待条件，或身份、同池、独立输出条件不满足 |

正式“适合”映射候选；“不建议”映射明确放弃；“条件适合”不视为已确认；“暂无法判断”保持未知。没有自动从 A/B 等级生成另一份正式决定。单关与串关腿分别登记；整串收益、相关性和组合规则仍未评估。

参考价和开放模拟：分别结算各自真实选票，在同一请求机会比较策略单位收益，允许两票赔率不同。主动弃权只有协议明确允许才视为零敞口；两侧都明确放弃的0/0机会也进入分母，无需编造赛果。若一侧选票，该票缺赛果、价格、费用或未通过评分时，不形成收益配对；缺失／失败始终不是0。`QUOTE_OPEN`另要求开放证据取得时点不晚于共同截点；参考价不能冒充开放价。

`QUOTE_ACTUAL`保留每侧真实成交结果，但标为观察性，不计算受控政策收益差、不为未成交方推造零收益。采用真实单笔收益不代表随机化或同一可成交机会已经证明。

不同票的五态样本空间不同，不能直接以五态 Brier 数值比较谁更准。本模块只保留同一完整1X2概率空间的Brier/Log loss对照；同模型控制下该指标通常相同，不应宣传为模型准确率改善。换票的主要量为每机会策略单位收益；每下注ROI需要另报分母，不能用本模块的等比赛平均收益差冒充ROI。

配对差先在每场内部平均，再按比赛等权，对`competition+match_id`聚类 bootstrap，复用 F3 的固定种子和2000次重采样。单场无区间，空集为空。前瞻与研究分层；小样本只是描述性，协议文本、测试通过或采集成功都不自动证明准确率或盈利改善。

## 原件可信性与回退

所有制品追加保存、输出文件禁止覆盖；中立包、运行原件、决定、失败、重试、比分修订和对照报告分别留存。清单返回值应保存在实际交接记录或远端提交中，哈希不能证明本地管理员从未删除尾部提交，也不能认证独立思考、来源真实性或未接触未来信息。相关声明须由实际原件和运行记录支持。

回退仅停用候选政策／新入口；原F2/F3接口、正式生产判断和已留存记录继续保留。本模块不更改框架正文、生产模型、每天运行时间、预算或自动下注行为。
