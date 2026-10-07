# S2 单一决定、追加事件与影子分级

本入口将框架 v1.7 修订批次5的决定、报价、时间和追加记录接成同一数据流。正式适配仍为“适合／条件适合／不建议／暂无法判断”；A/B/C/D/U仅属于另有版本的影子政策。它不运行足球模型、不选择球队、不自动评级、不自动下注，也不把结构一致称为预测正确。

## 使用与职责

Python发布接口为 `build_decision_bundle(record, output_dir, *, source_root)`，位于 `football_model.decision_record`。`source_root`只读取清单命名的本地原件；输出目录必须是新目录，决定与后续事件均只创建新文件。报价整行适配由 `decision_quotes` 负责，影子政策的结构校验由 `shadow_grading` 负责，现有 `decision_audit.validate_decision_record` 分别检查单关与候选腿正式档案的引用与价值权限。

```sh
python -m football_model.decision_record build --record record.json --source-root evidence --output new-decision
python -m football_model.decision_record verify --bundle new-decision --manifest-sha256 EXPECTED_SHA256
python -m football_model.decision_record append-event --bundle new-decision --event event.json --source-root event-evidence --events-dir events
```

`verify_decision_bundle(bundle_dir, expected_manifest_sha256=None)`检查真实文件、清单、决定重编译、审计重放与全部阅读视图。提供独立保存的清单摘要才能核对外部锚点；包内自洽不证明历史时点确实存在过该包。`append_decision_event(bundle_dir, event, events_dir, *, source_root)`另存事件及证据，不改变基础决定包。

本入口的HTML是由同一决定生成的阅读视图。完整比赛报告要求的事实原件、真实模型复算、完整截图、赛后记录与累计ZIP仍须由原归档流程保存。不得把一个视图包称为已经完成全部赛前研究或累计归档。

## 一个正式决定

输入使用 `schema_version="s2-decision-1.0"`；`policy_id="framework-v1.7-r5"` 表示正式判断所用政策，不能用影子政策覆盖它。

| 字段 | 语义 |
|---|---|
| `match_id`、`decision_id`、`version_id` | 比赛身份、唯一决定身份、实际版本；更新新建，原版本保留 |
| `analysis_cutoff`、`kickoff_at` | 带时区的信息边界和开赛时间；并非决定已存在或已发布的证明 |
| `future_information_exposure` | 本次执行者是否接触未来信息；未知不能作未接触 |
| `inputs` | 外部来源、事前规则、代码、模型原件的白名单及实际指纹、取得时间 |
| `derived_artifacts` | 仅由已声明依赖计算的派生结果，保留父ID、字节指纹和计算完成时间 |
| `archive` | 正式来源、事实、合约、候选、条件、模型评估与计算记录 |
| `quote_rows` | 原完整两侧或三项报价；从这些行派生绑定具体合约的单票 |
| `primary_candidate_id` | 本版唯一相对主方向；没有主选时明确记录原因 |
| `forecast_id` | 报告实际采用的已保存预测身份；没有适用预测时保持空并说明，不能借用后台首条预测 |
| `decision_chain` | 最强有据备选及比较理由；程序核关联，语义仍需原文复判 |
| `shadow_grading` | 可为空；若填写，带自身政策版本、候选/合约/报价绑定、用途与依据 |

正文决定、主卡、摘要、候选角色、membership与评分导出均从这一对象派生。不得手工维护另一份 `body_decision` 或 `summary_decision` 后仅比较副本。自由文字仍需实际阅读，以识别遗漏或与结构记录冲突的表达。

候选的正式用途写成`formal_fit={"single": ..., "parlay_leg": ...}`，每个值分别采用四态之一；未评用途保持空并记缺件，不能把只评单腿的旧结论复制为单关。兼容字段`fit`仅为单关别名；两者冲突会阻断。`fit`与`value`分开。定性适合并不要求模型概率；未量化的概率、EV和保本价保持空，不填0。同一基础模型的价值判断仍须绑定本候选、合约、报价与版本，沿用 [S2对称价值权限](s2-value-permissions.md)。

两种用途的补充条件可写`formal_condition_ids={"single": [...], "parlay_leg": [...]}`；显式提供时须两键齐全，每种用途按公共`condition_ids`与本用途补充项的并集派生审计。省略时沿用公共条件；用途空数组不能擦除既有公共关键条件。例如仅串关腿须待某条件，应放入`parlay_leg`补充列表，不能误列为两种用途的公共条件。

`archive`包含`sources/facts/contracts/candidates/conditions/model_records/calculation_records`数组。无主选时填写`no_primary_reason`；没有可绑定的实际采用预测原件时填写`forecast_missing_reason`，并随主卡及评分视图显示；历史报告原来复算过但本轮未重建预测，不应写成原来未复算。采用预测时，`model_records.forecast_id`与`forecast_evidence_ids`须指向实际保存的对应预测证据，不能只写一个任意ID。最强备选须为已归档且不同于主选的候选，比较理由保留其支付差异。

## 输入、派生与真实时间

每一项外部输入记录 `input_id/source_id/path/sha256/observed_at/published_at/source_claimed_published_at`。`path`相对`source_root`且不能越界或经符号链接跳转；`sha256`须与读取字节一致。`role`可为`screenshot/source/model/prediction/rules/code`，赛果不属于此处的输入角色。`observed_at`是本流程真实取得该版本的时刻，不能用文章日期或重新读取缓存的时刻代替。`published_at`仅填有证据的可信发布时间；页面自报、未核或相互矛盾的时间放入 `source_claimed_published_at`，保留冲突原因。

派生结果记录 `artifact_id/path/sha256/parent_ids/calculated_at`，并填写`kind="computed_from_frozen_inputs"`及具体`transformation`。允许冻结输入后的计算晚于信息截点；其父对象必须可解析、无环，计算不得早于已知父输入取得或上游计算。派生项不能带`observed_at/published_at/source_id/source_url`外部来源字段；真正新增的外部信息仍受原截点约束，不能通过标成派生结果绕过。外部取得时间未知会保留缺件并限制资格，不能代填截点。

外部输入子包可由 [evidence](evidence-contract.md) 冻结并核验；派生报告或晚算结果不混入严格外部输入白名单。输入清单摘要、完整证据包清单摘要、决定摘要、阅读视图摘要的范围不同，不可当同一指纹互换。

程序分别记录`freeze_started_at`（开始读取和冻结）、`freeze_committed_at`（原输入字节复制完成）和`report_generated_at`（决定视图及HTML写完）的真实时刻。最后一项保存在`times.json`，HTML引用该记录，避免为了把完成时间嵌入自身而先填一个开始时刻。`freeze_committed_at`不是整个包的清单落盘时刻。实际发表只由后续发布事件提供；创建文件不等于用户收到。全部内部完成时间由程序产生，不能通过过去 `analysis_cutoff` 倒填。若完成时已开赛，只能保存对应实际资格，不能称开赛前已公开预测。

## 报价行与单票

完整原行保留公司、来源/原图、市场独立行号、页面序列、显示时间及精度、制式、盘口和全部两侧/三项；未知来源属性保留空，不补造。原行字段沿用[evidence报价选择契约](evidence-contract.md)，另须显式声明常规时间`settlement_basis`。单票使用独立 `quote_offer_id` 并关联 `quote_row_id` 与 `contract_id`；保留从原主队盘口到选择方盘口的符号转换。

框架和历史图表里的`quote_id`可能指完整行，旧`decision_audit`里的`quote_id`则指具体单票。本入口保留`original_row.row_id`，生成`quote_id_mapping`与`quote_id_semantics="offer_id_in_decision_audit_view"`，并记录`quote_adapter_version="decision-quotes-v1"`。不能只把旧行ID改名后丢掉行与票的对应关系。

| 对象 | 保留边界 |
|---|---|
| 1X2原行 | 三项同公司、同一行，不能跨行拼成分布 |
| 让球原行 | 盘口按主队符号存；客队票明确反号，不能按队名猜方向 |
| 大小球原行 | 大小共用原总球线，选择方不改变线值 |
| 赔率转换 | 使用已确认制式；原值与十进制值并存，不凭数值猜制式 |
| 封盘、不完整行 | 原状态保留；不能跳过顶部再把旧有价行当当前 |
| 历史截点 | 复用`snapshot_latest/as_of_latest`分层；分钟歧义不补造秒数 |
| 公开参考 | 不自动升级为当前开放，更不升级为已成交 |

不同市场变化时间异步本身不使清晰最新参考失效。本入口不设置通用“5分钟报价／1小时预测”推荐门槛；旧严格模拟协议仍由现有评分器独立执行。参考价纸面结算、开放模拟、实际交易的新评分协议属于F3。

`adapt_quote_row`只转换已结构化的一整行，不执行OCR、最新行选择或成交验真。需要历史行选择时先用`evidence.select_quotes`。数值行生成`open_reference`只表示价格参考；`current_open_claim`和`actual_execution_claim`即使带元数据也标为声明，仍需回证据核验，不能从转换成功推定当前可买或已经成交。

## 影子分级与条件

正式适配与影子等级分别显示，并注明影子政策版本。影子单关与候选腿分别写用途、行动状态、等级、支持和风险理由；整票另评，缺其他腿不使单腿自动变U。

| 影子状态 | 必要说明 |
|---|---|
| A | 自身支持、具体支付边界、最强反证、最强有据备选与普通变化下的稳定性；最脆弱假设及主要失手路径 |
| B | 决定链已完整，仍支持的原因及未到A的主要弱点 |
| C | 明确区分证据链未完成与不符合当前用途，不能因缺某个标题机械降级 |
| D | 否定这张具体票的决定性依据；探索性负EV单独不具该权限 |
| U | 对象或必要状态无法判定、又无法形成可用条件分支；写最小缺件 |
| 待条件 | `action_status="awaiting_condition"`且当前等级为空；记录满足/不满足/未知三态及动作 |

无模型、无xG、普通首发未知、异步来源、较深盘口或较高赔率不自动降级。定性A级不对应固定胜率或已证明正EV。等级结构通过只说明声明自洽，不能代替对足球机制的语义核查。

本轮只校验同一正式主候选的影子分级：`schema_version="s2-shadow-grading-1.0"`、`mode="shadow"`，并绑定`decision_id/candidate_id/contract_id/quote_offer_id`及影子`policy_id/policy_version`。`single`与`parlay_leg`各自给出用途、理由、行动状态和等级；差级另填`difference_reason`解释目标及风险分布差异。`evaluation_context`区分当前判断与历史开发复核，历史复核须明确未修改原报告、未追认历史等级。

行动状态为`candidate/watch_only/not_recommended/unable_to_judge/awaiting_condition`。A/B仍可只观察，等级不代替行动；C对应观察，D对应不建议，U对应无法判断。待条件必须可观察并写明满足、不满足和未知时的动作；A/B不能带该用途尚未解决的关键条件。

`same_candidate_grading`描述本轮比较范围。换票的`policy_selection_compare`保留为后续设计，本入口尚未实现独立影子选票；后续须绑定影子主选、合约和报价，并保留正式主选。不能把新票的等级贴给旧票；相同票的两套等级也不形成两场独立样本。

条件确认是追加事件。条件满足只触发依据当时价格与合约重评，不能直接让旧“目标A级”生效。若价格、线位、方向或实质判断改变，应创建新决定版本。

## 追加事件与回退

本轮事件类型为`publication/user_received/execution/condition_changed`，分别保存发表、用户收到、执行声明和条件变化。事件绑定`match_id/decision_id/version_id`及程序生成的基础清单摘要，保存原证据字节、发生时间和真实`recorded_at`。`occurred_at`无凭证时保持空并标记`timing_status="unknown"`；已知时刻不得晚于记录时刻或早于其所引用制品的真实完成时刻。

执行事件保留`accepted/rejected/cancelled/unknown`，绑定具体候选与`quote_offer_id`。用户明确确认已下可用`execution_basis="user_confirmation"`并附同类原消息证据；未知成交价或成交时刻保持空，不推收益。成交单路径仍需实际价格和`transaction_reference`；程序保存的是证据声明，其真实性仍需阅读原件。参考价、条件满足、模型信号或口头计划本身不能推定已成交。原`membership/scoring`不会被事件回写，后续消费事件和计算收益属于另外的评价流程。

条件事件仅接受已登记条件和满足／不满足／未知三态，并明确要求重评；它不会改正式四态或影子等级。纠错与报告制品的专门事件尚未接通；本轮须通过新决定版本或原有追加归档流程保存原值、修正值及其依据，不覆盖原报告。

新版本和新事件不得覆盖已有文件。回退时停用新渲染或影子政策，保留已经生成的决定、事件、失败审计和指纹；不能回退为删除不利记录或修写历史主方向。

## 验收与已知限制

| 制品 | 内容与边界 |
|---|---|
| `input_record.json`、`decision.json` | 提交对象及一次编译后的唯一决定，后者是下游视图来源 |
| `originals/`、`evidence.json`、`manifest.json` | 原字节、输入与依赖清单、逐文件摘要；外部摘要另存 |
| `times.json`、`eligibility.json`、`validation.json` | 真实程序时钟、时点资格和结构/语义待核问题，三者分别解释 |
| `main_card.json`、`membership.json` | 唯一主卡及主候选的用途与影子状态；不代表整张串关已评估或执行 |
| `scoring.json` | 绑定比赛、决定、预测、合约、行和单票；协议为`UNASSIGNED`，结果与收益为空 |
| `audit_archives.json`、`report.html` | 按单关/候选腿派生的旧审计视图，以及含原行、反证、备选和诊断的决定阅读视图 |

结构状态为`blocked/incomplete/structural_checks_passed`；缺件不伪装通过。时间资格另为`research_only/prospective_input_candidate`，后一值也不证明已经公开、未来信息未被接触或预测已验证。CLI对结构通过、核验通过或事件追加成功返回0，其余返回2；失败诊断包仍应保留。`publishable`不会因结构通过自动变为真，人工语义复核始终保留。

本入口需验收引用一致性、真实时序、无环依赖、追加防覆盖、整行转换、定性四态、影子绑定、报告转义及最终阅读视图。发布和线上运行应另看真实提交、CI与保存证据，不以本文件的存在推定已经部署。

真实原文开发复核至少检查：萨瓦德尔原文已有定位球/传中与三球支付路径；马耳他r2撤回包含模型分歧、价格和转化多个因素；希腊—德国原文已写小3全输路径与最近替代。这些用于防止接口吞掉原语义或审计机械降级，不给旧报告追认新等级。原文有依据不等于依据充分，也不证明网站事实已在本轮重新核验。

已用于设计的40场仍为开发样本。源文件指纹、结构测试、同输入一致性或历史重演都不能证明未来准确率、盈利优势或分级有效。下一阶段仍需F3同场同截点评价、真实前瞻样本及独立语义复判。
