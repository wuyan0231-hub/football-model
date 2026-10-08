# S2 开测检查：G2、G3、G5

入口 `python -m football_model.pretest` 是**离线证据门禁**。它实际读取白名单原件、复跑原单场函数、核报价整行、核正文投影、保存读回及容量；不联网取球队新闻，不替分析者选边，也不生成下注或发表事件。合成测试通过不代表已完成一场真实新比赛。

普通单场、首场流程、框架对照和F5正式确认采用不同资格。无模型、无xG、普通首发未知和不同市场报价异步均不自动否决定性分析。任何缺件不补0，不给缓存换新取得时间，也不把文件日期当球队资料齐全的证明。

| 模式 | 用途 | 核验缺口怎样处理 |
|---|---|---|
| `ordinary` | 现行普通单场分析 | 数值缺件退回未量化；价格缺件退回未定价；原文或保存未核如实披露。明确结构矛盾及采用隔离报价仍须修正。 |
| `flow_pilot` | 首场实际赛前—更新—赛后流程试运行 | 要求冻结、统一生成的决定视图、原文语义复核、保存读回和当前容量通过。无模型仍可用明确披露的定性流程。 |
| `framework_comparison` | 已预定的新旧框架对照 | 同上，加完整冻结的左右政策包与预定方案；两侧使用相同模型或相同披露的量化回退。准备任务模板不算独立分析已经运行。 |
| `f5_confirmation` | 新候选正式前瞻确认 | 要求模型、数据及报价适用，并另需真实成熟链、长期容量及不可变激活/采集链。**当前准备稿和激活检查器均未激活采集，所以本模式继续阻断。** |

## 执行入口

```sh
python -m football_model.pretest \
  --record 本场开测记录.json \
  --source-root 本场原件与派生目录 \
  --mode flow_pilot \
  --output 新目录/开测检查.json
```

工具仅创建新的输出文件，拒绝覆盖。输出实际回读后，标准输出给出真实SHA-256回执。`status=blocked` 或无效输入退出2；工程检查通过或有明确局部回退退出0。必须看 `findings` 和各模块状态，不把退出0称作资料齐全、报告已发表或效果验证通过。

输入沿用 `schema_version="match-archive-1.0"`，保存 `match_id/match_key/version_id/home_id/away_id/analysis_cutoff/kickoff_at`，只增加一个 `pretest_checks` 对象。原有合约、报价、决定、模型、时间记录仍从现有制品读取，不另外手填概率。以下字段由执行者自动采集或核原件填写，用户不需要手工填表。

| `pretest_checks` 分项 | 必要字段及实际核验 |
|---|---|
| `evidence_bundle` | `path/manifest_sha256/required_source_ids`，指向现有 `evidence.freeze` 包；核外部锚点、白名单全文、身份、版本、截点及真实取得时间。`completeness_status="declared_complete"` 与 `content_review={status:"reviewed",no_result_content:true,evidence_refs:[...]}` 是人工范围/语义复核声明，工具不能认证声明者。 |
| `model` | 已适用时为 `status="applicable"`，带 `packet/execution_receipt/config` 三个 `{path,sha256}` 原件引用、`runtime_path/league/source_commit/prediction_source_commit/state_commit`。缺模型可用 `status="unavailable"` 或 `"not_applicable"` 和真实原因。 |
| `model.actions` | `status="checked"/checked_at/failure_scope/evidence_refs`；`failure_scope` 仅 `none/other_leagues_only` 可清除本场的数值失败门禁，目标或全局失败范围未知不宣称已清除。访问渠道和近期Actions仍由模型授权读取流程实际取得。 |
| `freshness` | `coverage_status="complete"`、`scope="model_supported_competition_and_requested_seasons"`、独立核对的 `expected_schedule_source_ids`、`training_data={path,sha256}`、两队 `teams=[{team_id,expected_match_ids,evidence_refs}]`。应有ID是截至截点本模型赛事与请求赛季内已具合格常规赛果的比赛，不能拿所有杯赛等混作遗漏。 |
| `quotes` | `rows={path,sha256}` 指向冻结白名单中的完整行数组；`selected_row_ids` 保存实际采用行；每条 `row_reviews` 有 `row_id/source_kind/whole_visible_history_reviewed/whole_row_and_odds_format_verified/evidence_refs`。截图另要求 `second_image_reviewed=true` 且行的 `image_id` 指向真实冻结角色为 `screenshot` 的原件。来源种类为 `screenshot/api/sourced_page`。 |
| `semantic_review` | `decision_archive={path,sha256}` 指向 F2 `audit_archives.json` 中的一份用途投影；`decision_bundle={path,manifest_sha256}` 指向统一生成包。检查完整包重编译、主客/截点/开赛/版本及投影完全相同。另保存 `status/evidence_refs/mechanism_supports_payment/strongest_counterevidence_handled/nearest_alternative_compared/all_candidates_match_body/conditions_not_reminders` 原文人工复核声明。 |
| `save` | `artifacts=[{path,sha256}]` 实际回读原件和输出；`capacity={state_root,hard_limit_bytes,reserve_bytes}` 调用现有存储估算器，区分原件逻辑字节、保守writer成本、实测传输树。可提供完整 `state_tree` 让原生校验器验证实际压缩状态。离线本地回读不认证远端保存。 |
| `publication/execution` | 无原始追加事件凭证时继续 `status="unknown"`。本工具不将发表/成交声明升级为事实，不从文件生成或参考赔率推导收益；真实事件继续用F2追加事件入口处理。 |
| `comparison_plan` | `{path,manifest_sha256}` 指向 `pretest_protocols.freeze_preparation` 的完整冻结目录。单场白名单另包含其 `preparation.json` 原字节。实际左右政策从 `read_preparation` 全包复核，不接受手写两个名称代替全文。 |
| `confirmation_protocol` | 同样引用完整准备包；可另提供 `activation_request={path,sha256}` 给 `check_activation` 回核上游真实证据。`ready_for_separate_activation` 也不是已激活，不改变现有试运行资格。 |

`model.execution_receipt` 保存真实 `argv/exit_code/started_at/completed_at/packet_sha256/source_commit/state_commit`。其中 `source_commit` 声明**本次复核代码**，包内 `provenance.source_commit` 和 `model.prediction_source_commit` 指向**原生产预测代码**，可以不同。离线门禁只核声明对应与原文件字节，不认证该SHA的Git归属；输出使用 `declared_recomputation_source_commit`、`actual_runtime_source_commit_verified=false`，另记录当前 `integration.py/model.py/pretest.py` 的实际SHA-256。包内 `read_and_recomputed=true` 单独不能通过；工具会核原件白名单和对应原字节，再调用原 `integration.match_packet`，比较完整原预测、身份和来源。不训练、不改λ。

当前指数训练的实际入模ID不从“最近更新成功”推断。工具把固定 `runtime/data/<league>.json` 的比赛按原模型规则规范化，以模型实际训练截止及原四小时延迟筛选，重算与 `training_data_sha256` 完全相同的指纹，然后逐队与应有ID比较。独立赛程完整性未知、原件不匹配或ID遗漏时保持未知/缺失，限制本场当前量化和价值声明；不否决定性适合，也不回填历史训练数据。

## 报价、正文及容量边界

报价复用 `select_quotes` 与 `adapt_quote_row`。顶部封盘、裁切、未知报价时刻或分钟跨截点不跳过取旧数字；客队让球按主队线反号。报价取得晚于截点不使用。所有未认证API亚洲盘行继续隔离；即使人工把 `source_kind` 写成截图或声明符号已核，也不能解除采集原始标记触发的隔离。公开参考与当前开放、成交分开。

正文直接 `match-archive` 仅能查给出的声明。流程及比较模式要核统一生成包，防止主卡、正文和全候选清单来自不同字段副本。自动检查仍不能判定“最强反证是否遗漏”或人员风险幅度是否充分，人工复核必须指出实际原文依据。

容量调用原生 `validate_runtime/estimate_runtime`，不把约900MB恢复原件与500MiB传输硬限混比。`persisted_bytes` 是压缩及清单开销的保守writer估计，不是实际上传大小；提供完整保存树时另由 `validate_storage_tree` 核实际传输。F5另保留影子400MiB写入阈值。声明每日增长、某一日速度或预计线性容量仅作诊断，`long_cycle_supported` 保持未知，不能证明548天确认期已有经费和恢复能力。

本工具没有独立认证人工取得时刻、代码提交归属、认知隔离或未观察历史；同一冻结包内自洽也不证明这些事实。固定提交授权读取、真实图片第二遍核对、真实独立分析运行、实际云端保存和未来赛果仍各需原始证据。工程验收、部署、单场真实流程及准确率/收益验证分开登记。
