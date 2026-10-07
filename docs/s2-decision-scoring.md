# F3：实际采用决定与三种报价协议评分

入口 `football_model.decision_scoring` 读取完整 F2 决定包，先执行 `verify_decision_bundle`，再从该决定指定的 `forecast_id` 及冻结父证据取得矩阵。后台首个合格预测仍由原 `prospective` 协议独立评分；本入口不替换其选版、300 秒报价／3600 秒模型限制或历史分母。

正式框架与图规仍为修订批次5；本入口是评价能力，不能把测试通过、恢复覆盖或一场结算写成准确率或盈利提升。40 场已用开发材料继续标研究；没有实际已冻结的未来决定时，不合成前瞻样本、历史等级或用户成交。

## 预注册及调用

以下是可执行接口示意，路径、ID须由实际产物替换。协议以程序真实时钟登记，不提供历史 `as_of`。

```python
from football_model.decision_scoring import (
    register_scoring_protocol, score_decision, read_decision_score,
)

protocol = register_scoring_protocol({
    "name": "F3-reference-paper-v1",
    "quote_protocol": "QUOTE_REF",
    "usage": "parlay_leg",
    "scope": {"competitions": "all", "match_ids": "all"},
    "evaluation_mode": "research",
    "decision_selection": "explicit_adopted_version_no_retrospective_switch",
    "condition_policy": "satisfied_or_no_conditions_only",
    "cost_per_unit": None,
    "unit_stake": 1,
}, "runtime/decision_scoring")

score = score_decision(
    "path/to/verified-f2-bundle",
    expected_manifest_sha256="actual_f2_manifest_sha256",
    protocol_root="runtime/decision_scoring",
    protocol_id=protocol["protocol_id"],
    result=None,
    output_dir="runtime/decision_scoring/new-score-version",
)
checked = read_decision_score(score["output_dir"], score["manifest_sha256"])
```

`scope.competitions/match_ids` 可用非空、去重的字符串列表，或明确 `all`；`usage` 仅 `single/parlay_leg`。整体串关必须另有全腿、整票价格、执行与结算协议，此入口不会把候选腿收益当整串收益。固定1单位是纸面计量单位，并不声明实际投注1单位。费用 `None` 保留未知；显式0才表示预声明零成本。

命令行支持：

```bash
python -m football_model.decision_scoring register --spec scoring-protocol.json --root runtime/decision_scoring
python -m football_model.decision_scoring score --request scoring-request.json
python -m football_model.decision_scoring verify --bundle NEW_SCORE_DIR --manifest-sha256 ACTUAL_SHA256
```

`scoring-request.json` 是 `score_decision` 的同名关键字参数。新输出目录不得已存在；相同结果重算、赛果修订、事件补齐及新版决定都用新目录，保留旧分数和旧父指纹。评分包复制完整决定、协议、赛果、开放原件、执行事件及其清单，之后可以脱离原外部路径复算。读取评分包不仅核文件清单，还重新计算结算与概率并比较 `score.json`。

## 三协议边界

| 协议 | 取得什么才可算净收益 | 输出与边界 |
|---|---|---|
| `QUOTE_REF` | 冻结决定内具体合约及有效参考数值、该用途关键条件已满足、费用已明示 | `paper_reference_return` 是该票1单位假设净结算，不证明当前开放、政策实际推荐或真实交易 |
| `QUOTE_OPEN` | 以上条件，另有独立开放原件、精确票与价格映射、真实开放/观察时点、具名语义核验 | `open_simulated_return` 是有开放证据的1单位模拟，不是实盘；不同开放机会不得混配 |
| `QUOTE_ACTUAL` | 经核验的 F2 execution event、原成交/确认材料、精确票、已接受状态、已知成交时点/价格/费用/单关用途、具名核验 | `actual_single_return` 是实际单关每单位净收益；已知真实 stake 时另算 `actual_stake_return`；整串恒为空 |

三个字段不相互回填。异步参考价不会因“超过5分钟”在本入口被统一否决；原严格模拟协议仍原样保留。API `reference_only` 数值可作 `QUOTE_REF`，其自身不提供开放或成交资格。

纸面票结算与正式／影子行动分开：正式不建议但影子选择的同一票，仍可保存有证据的假设票收益，再由预注册策略决定是否实际纳入模拟。不能为了算收益将原适配改为“适合”。条件未知仍不算该票模拟收益；条件事件只要求创建新决定重评，不自动使旧条件或等级生效。实际成交层保存已核实的交易，即使它与原建议不同，也不能删除真实不利执行。

## 实际采用预测

只检查 `archive.model_records` 中与正式 `forecast_id` 对应的 `forecast_evidence_ids`。支持 F2 冻结的显式预测对象，及原 `prospective` 内容寻址 forecast 原件；后一种 ID 必须等于原字节 SHA-256。

必须核对比赛、赛事、主客 ID、开球时刻；矩阵行列必须明确从0开始的主客进球。进一步核模型版本、训练指纹、模型 ID、真实时钟语义、运行开始／资料取得／拟合／生成顺序、冻结父证据取得时点、矩阵归一及其与1X2概率一致性。缺版本、指纹或时点则概率保持未量化，定性票仍能结算。相同预测 ID 指向不同内容时阻断，不能从后台首条或后来文件偷换。

冻结输入后的计算可以晚于外部信息截点，但不能早于父资料取得；派生产物关系由 F2 核验。研究回放、开球后生成的模型及 F2 已属研究的材料不会升级为前瞻。完整模型身份未保存在决定里时，不猜主客身份来计算概率。

## 独立90分钟赛果

`result={"path": "result.json", "sha256": "原字节摘要"}`，配合 `result_source_root`。JSON可直接使用原 `prospective/results` 对象；必要字段为 `competition/match_id/home_id/away_id/kickoff/home_goals/away_goals/observed_at`，以及：

- `score_scope="normalized_API_Football_score_fulltime_90_plus_stoppage"`，或明确 `regulation_time_90_plus_stoppage`。
- `captured_at` 可选，缺失时只用已证据化的 `observed_at` 作时间检查，不声称另有真实捕获事件。
- 必须与决定已保存身份匹配，非负整数比分；未知、含加时或点球的口径拒绝。沿用既有四小时保守成熟缓冲，缓冲本身不能证明完赛或供应商正确。

赛果只在评分层连接，不写入决定白名单；未知赛果为 `pending`，不填0。上游赛果真实性和正常时间规范化仍须依据来源核验，本模块不能从一个自报 `score_scope` 独立证明供应商原数据正确。

结果描述符另可附 `source_evidence` 原件数组。原生 prospective 结果必须提供与 `source_id` SHA-256 相同的 source 对象，核其 `kind="normalized_regulation_data"`、真实观测时点、唯一比赛身份与比分，保存为 `native_normalized_parent_verified`。这是规范数据父层核验，不表示本次已重读所有上游API响应。人工规范结果需独立来源原件与具名语义审核；描述符及每个来源分别绑定自己的摘要和核验人，记为 `independent_originals_reviewed`。

F3 独立赛果同步的新 `kind="independent_fixture_result_response"` 同样支持：核完整响应指纹、`/fixtures` 精确请求、分页完整性、单场计数及真实抓取时点，再实际执行 `normalize_fixtures` 重放，使用 `score.fulltime` 比对身份与常规时间比分。原生同步→真实内容寻址结果路径→评分保存→离线重读已有端到端工程回归；不把含加时的总进球或最终胜者替代常规比分。

没有父原件／审核时仍可保存声明结果和五态研究算术，但必须记 `result_source_status="unverified_normalized_declaration"`、降为研究，不进入前瞻主指标或输出实际单关收益。已提供的父来源有身份、比分或指纹冲突则拒绝，不能作为“缺件”默默通过。评分包只宣称冻结和核验实际提供的父层，不宣称取得未提供的上游原件。

## 开放与成交原件

`QUOTE_OPEN` 的 `quote_evidence` 为 `{path,sha256,semantic_review}`，其JSON应包含完整比赛身份、`decision_id/version_id/candidate_id/contract_id/quote_offer_id`、标准 settlement 合约、同一 `decimal_odds`、`status="open"`、`conditions_status="satisfied"`、`asserted_at/observed_at`，以及非空 `source_evidence`。

`source_evidence` 每项为实际独立截图／响应 `{path,sha256,observed_at,semantic_review}`；路径相对 `quote_source_root`，原件必须与声明文件不同。每份 `semantic_review` 为 `{status:"verified", reviewer:具名复核者, source_sha256:被核原件摘要, reviewed_at:真实核验时刻}`。外层核验绑定声明映射，原件核验绑定底层内容。只有声明布尔值、重复使用参考报价自身或没有底层原件均不能模拟开放收益。人工核验仍是有归属的声明，不是供应商签名认证；报告必须保留这一能力边界。

`events=[{path:实际F2事件目录, manifest_sha256:实际摘要, semantic_review:核验对象}]`。成交审核的 `source_sha256` 指整个事件清单摘要，核验者须实际阅读包内凭证。除了 F2 已有字段，实际收益需要事件明确 `usage="single"`、`conditions_status="satisfied"`、`cost_per_unit`；`stake` 缺失时只算每单位，不假定实际金额。仅用户确认“已下”但价格／时间未知可保留 accepted，不推收益。`parlay_leg` 不获得单关或整串收益。一次评分必须明确选择一个执行事件，避免看结果挑选多个成交中的赢家。

## 输出、对照与资格

`binding` 保存比赛、决定、版本、政策、实际采用模型、候选、合约、整行、单票和信息截点；`source` 保存父清单／协议／结果／事件／开放证据指纹；`decision_times` 保留原冻结与报告完成时间。`source_pool` 的基础输入池排除规则、代码和派生报告，报价池保留实际完整适配行。原材料缺失不由文件名或聊天记忆补全。

`probability` 分别保留五态概率、费用后的 `P(R>0)`、`P(R>=0)`、净EV；未知费用时后三者为空。`metrics.one_x_two/five_state` 分别给 Brier、Log loss、校准桶及原发生事件零概率信息；数学无穷Log loss显示为明确状态和空数值，不伪造有限数学值。五态指标排除数学上不可能的状态，不能由有限矩阵的0推断结构不可能。

`qualification=prospective_decision_candidate` 仅表示协议先于截点、决定已有合法前瞻输入资格；不证明报告已发表、用户收到、样本独立、策略正确或历史盲性。协议晚登记、研究材料、开球后补证等为 `research_only`。研究与前瞻必须分组，不能混在同一个前瞻主指标里。

同截点策略评价由 `policy_evaluation` 消费 `read_decision_score(path, expected_manifest_sha256)` 的核验结果，再独立核预注册、请求全集、共同输入、报价机会、选版、行动与用途。同场多票和版本不能当独立比赛；费用缺失只阻断净收益，不应删除可用的分布评价；两侧均明确无模型的定性票仍可比较合约与纸面政策收益，概率保持空。

## 验收与回退

回归涵盖三市场、客队让球反号、四分之一盘、采用预测与后台首版不同、主客身份冲突、缺模型血缘、数学无穷损失、费用未知、中文条件、研究隔离、开放证据、真实单关／候选腿区分、结果修订、父指纹和追加防覆盖。测试均为工程构造，不是足球预测效果样本。

停用新评分时保留已保存协议、旧分数、失败／缺件及原决定。不得删除亏损票或用新成绩覆盖旧版。新协议能力实际发布、真实资料验收与未来效果验证分别记账。
