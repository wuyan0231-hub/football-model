# 当前官网参考观察独立入口 v1

本入口解决一个明确的接入边界：官网公开 DOM 已实际读取完整报价行，但页面没有显示单笔报价变动时间。旧 `feature_decisions._load_quotes` 只消费历史 `as_of_latest`，因此拒绝 `unknown_quote_time` 是正确行为。新增入口显式消费当前 `snapshot_latest`，不修改旧选择器、模型数学、50 文件实现指纹或研究协议。

## 第一步：真实当前调用与参考价计算

```sh
python scripts/current_observation_decision.py \
  --runtime /path/to/runtime --config config.json \
  --league epl --match-id 1557430 --state-commit FIXED_STATE_SHA \
  --input reviewed/current-input.json --output /path/to/new-current-runs
```

程序真实调用 `integration.match_packet(as_of=None, decision_input=None)`，重新读取并复算原 M0、同生产父行 F5-C 与全部候选。没有 `--as-of` 或人工时钟参数；冻结输入的 `analysis_as_of` 只保留为原件声明，不替代本次实际时钟。原生包无报价状态与报价结构校验的历史拒绝视图分别保存。

输入显式声明 `current-observation-decision-input-v1`、`observation_mode=current_observation`、`usage=reference_only`。旧 `feature-decision-input-v1` 仅接受显式 `diagnostic_reference_only`；同样必须补齐下列来源身份，不能把原 r2 缺少的字段假称已经存在。

- 所有来源经原 `_read_ref` 校验相对路径、符号链接、原字节 SHA-256、长度及有限大小；`reviewed=true`，真实 `observed_at ≤ reviewed_at ≤ 本次实际时间`。
- 报价来源为明确复核的官方 Pinnacle 官网 DOM，URL、主客队、市场、整行价格及让球双方相反坐标绑定；只支持本入口声明的英文页面整行语法。它核对已保存原件和人工复核声明，不提供网络真实性或足球事实真实性认证。
- `fixture_identity_source_id` 另指向同次完整 DOM 原件，类型 `official_dom_identity_snapshot`。程序读取原文中的完整比赛日期、开球显示及页面 `GMT ±HH:MM`，转换后必须精确等于原模型比赛开球时间。页面时钟只证明显示时区，绝不写成报价变动时间。
- 各行比赛 ID、来源 ID、URL、真实观察时间绑定；固定市场 sequence 防止把旧行另分组为顶部。原 `select_quotes` 做结构/顺序检查，并保留 `unknown_quote_time` 与空 `as_of_latest`。当前只取每组 `row_order=0`；顶部 closed、unknown、不完整或缺少第0行时不回退较旧数字。
- 原始 `quote_time`、`time_precision` 继续为 null；原 API AH 来源标记仍由 `adapt_quote_row` 隔离，不能通过改写为官网标签认证。只允许 reference_only、开放状态未知、成交未知。
- 原 `adapt_quote_row` 与 `project_offer` 逐票计算。费用未知时费用后 EV、净盈利/不亏概率及净 EV 分歧均为 null；明确费用才计算净值。

每个可用报价保留全部模型记录与计算槽。未捕获/不可用候选保留其原因，不拼接其他时距或数学调整。输入不提供人工选择时不生成主票。提供选择时，必须有已复核人工声明原件，显式 `selection_binding` 绑定 candidate、model、原 row、side 与报价原件引用；程序不从自由文字猜合约，也不按最高 EV 选择。

输出为独立哈希目录，使用 exclusive/x 写入，拒绝覆盖。保存实际原生包、当前结果、输入及全部报价/模型来源原字节、配置、runner 原字节、50 文件代码闭包及指纹。`native_packet_sha256` 是实际包对象的规范 JSON SHA；文件原字节 SHA 另由 receipt 逐文件记录。

## 第二步：随后冻结完整决定，绑定同一实际包

完整 S2 决定可以在上述真实调用后生成。其信息边界使用已经保存的实际 `packet.analysis_as_of`；外部输入真实观察/发布时间不能晚于该边界。新决定的冻结、报告生成时间仍由原生 builder 记录实际后来时刻，不能把已生成时间倒填为信息边界。

```sh
python scripts/finalize_current_observation_decision.py \
  --run-dir /path/to/the/existing/current-run \
  --run-receipt-sha256 EXACT_ORIGINAL_RECEIPT_SHA256 \
  --source-root /path/to/reviewed-source-root \
  --link-input subsequent-s2-link-input.json \
  --output /path/to/new-finalizations
```

link input 包含 `decision_record` 与 `decision_calculation_links`。此步骤不再次调用模型入口，不改原 run、原 S2 或原截点。它先校验外部指定的原 receipt SHA、全部原文件和来源闭包、run/comparison/packet/code 身份，再对固定原 packet/current view 调用未修改的 `_decision_record_link`，随后真实执行 `build_decision_bundle` 与 `verify_decision_bundle`。三道状态均通过才报告结构通过；任一阻断保留在新输出和返回状态中。

后继独立目录包含整个原调用原字节、S2 所有来源原字节、原链接输入、原生链接结果、完整 S2 HTML/JSON bundle 及 build/verify 回执。原调用、来源和代码在执行前后重核。重复相同后继身份拒绝覆盖。

所有阶段固定 `historical_as_of_eligible=false`、`confirmation_eligible=false`。结构通过不证明人工分析独立、足球语义充分、预测改善、正式适合等级、经济优势、当前开放或真实成交。旧 native 历史流水线保持原拒绝规则；此工作只新增明确当前参考扩展。

## 2026-10-16 本地真实验收

比赛为 Everton–Chelsea、API fixture 1557430，开球 2026-10-17 11:30Z。原官网 DOM 观察 2026-10-16 14:06:38.505Z，页面显示 2026-10-17 19:30、GMT+08:00。真实当前调用信息时刻为 14:21:48.057206Z。

M0 与同父 F5-C 共14次实际结算计算；其余37模型、259报价计算槽保留未捕获/不可用。三组历史视图仍为 unknown_quote_time。费用全部未知，净 EV 全部 null。人工声明只显式链接 Everton +0.25@2.030、M0；足球机制与最近替代由人工原文保留，代码不认证其结论。原 r2 一处“从领先到2比2”的无来源过程叙述已在后续声明明确撤回，原件仍保存。

后继入口另加原 run/source/output 符号链接拒绝、原配置及50文件闭包逐hash复核，并在写前拒绝将输出放入原 run 内部。加固后的 CLI 已于北京时间22:45:21（14:45:21Z）真实执行，新身份 `4b3e5d0e8af78f92313b6365fe48157092079fc8777fb832f223487830ab3595`，旧 `d25d2724…` 执行目录原样保留。

后续完整 S2 在同一真实截点链接：7候选、原 helper 无结构问题，native build 结构通过、native verify verified；生成/冻结真实后来时间另外保存。紧凑回执见 [current-observation-decision-local-20261016.json](evidence/current-observation-decision-local-20261016.json)，大体积运行及全部原件留在独立执行目录，不纳入源码仓库。

验证命令：`python -m unittest tests.test_current_observation_decision tests.test_current_observation_finalize tests.test_feature_decisions tests.test_decision_quotes`。在仓库 venv 独立实际89项通过（8.177秒）；包括未来时间、顶部不回退、来源/数值/场次时区绑定、API AH、费用、人工选票、不可变输出、完整原件闭包、严格同实际包截点、后继原生 build/verify 阻断保留。

外部保存与固定提交恢复仍须独立验收；本地 verifier 收到 expected manifest 后回传的 `externally_anchored=true` 不能代替远端回读事件。

本轮完整内部单场审计包向公开仓库 `wuyan0231-hub/football-model` 的保存被自动审批明确拒绝，要求针对具体包与公开目的地的授权。完整远端提交和回读均未完成，部分 blob 上传不等于完整保存；R06 保持 partial。小体积请求正常执行与此次审批拒绝分别记录，不改用其他通道绕过。


## 后继完整远端保存与独立回读

在用户对具体公开归档明确授权后，[固定提交2a5c21a9](https://github.com/wuyan0231-hub/football-model/tree/2a5c21a9bbd9f3c06b085bd25a249fdf53ab837c/audit-artifacts/approved-20261016)已保存获批完整原件。37片按同一fixed ref全新读回，获批ZIP整体hash完全一致；180文件恢复、原生verify和原实际cutoff重放全通过。新事实见[公开回读验收](evidence/current-observation-public-readback-20261016.json)。

原blocked记录只描述此前真实状态，不抹去；R06现在仅按普通当前QUOTE_REF工程闭环结案。未把价值、完整人员、历史盲性或未来效果改成通过。
