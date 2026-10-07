# F3 当前报价积累（quote-collection-v1）

本入口只追加保存从实际运行时点取得的 API-Football 赛前报价，不训练模型、不修改原预测、不认定当前可成交、不自动下注。模型失败不阻止使用已经保存的完整供应商赛程定位报价候选。已有严格300秒报价/3600秒预测模拟协议保持原样，本入口不把采集结果自动送入该协议。

## 命令与运行预算

```bash
python -m football_model.collect_quotes --runtime runtime --config config.json --league all --horizon-hours 168 --max-run-seconds 600
```

可用 `--league k1`、`--match-id 123456` 缩小已知赛程范围。必须在未来7天内；没有 `--as-of`，不能把今天取得的响应倒填历史。正式客户端时钟采用实际 UTC；报告可转换北京时间展示，但不改变原始时间。

默认600秒为新增阶段的软截止，实际取此值与 `config.api_max_run_seconds` 的较小者。它为报告和状态保存预留时间，不是每日额度或每轮请求数上限；默认继续使用 `max_requests_per_run=null`。客户端每次网络尝试前复用原有日额度、响应头校准、分钟限速、后续请求预留、有限重试和超时保护。

有候选时先调用现有 `status()` 确认真实当日已用量，不能把套餐5000当作本轮可用余额。状态响应只保存白名单预算数字；密钥及账户资料不落盘。无候选、且有明确已存赛程证明空窗时不初始化客户端或发请求。无赛程资料则记录缺口，不能称正常0场。

此模块自身不设定轮询或新日程。工作流接入、并发状态保护和最终 `cloud_state save` 由既有运行入口负责。可以在模型更新之后、基本面采集之前单次执行；前一阶段失败时仍应允许运行并保存结果。

## 候选与当前身份

候选从 `runtime/cache/api_cache` 的完整 `/fixtures?league=&season=` 响应读取，使用配置中的赛事全名和国家精确匹配。保存赛程的文件路径、原字节SHA-256及实际取得时间；只选 `NS`、开球在当前时刻之后且不超过168小时的场次。场地模型不支持或训练失败不影响报价资料采集。

每场报价请求前重新取得 `/fixtures?id=`，核比赛ID、主客队ID、赛事ID、开球时间及当前 `NS` 状态。原赛程与本次身份不一致时拒绝该候选，保留原件并记录失败；不会改旧预测。已开球、延期或其他非赛前状态单列跳过。候选源可能较旧，报告不会称其是当前模型或最新赛程；新取得的身份响应单独保存。

报告 `issues` 逐联赛显示 `saved_provider_schedule_missing` 等缺口。旧赛程中的无未来比赛只表示“已知赛程中没有候选”，不证明供应商没有后续新增场次；每日原赛程更新仍由原流程负责。

## 分页与追加原件

`OddsClient` 继承原 `APIClient`。原训练接口白名单和拒绝分页的政策不改；专用 `fetch_odds_page` 只增加固定供应商 `/odds` GET，以原预算、限速、重试和禁止重定向传输原语取得单页。

每一页响应先保存，再核验绑定的比赛ID、页码、返回数量、总页数及取得时间。按服务器报告的总页数逐页读取，不设置额外页数上限；真实预算或软截止可以中止。页数中途变化、身份冲突、响应错误均不能标完整；已取页仍保留。只有全部页核验完成才写规范化快照。分页本来不是一个原子网络响应，保存 `acquisition_atomic=false` 和各页时点，不把多页说成同一瞬间市场。

目录：

| 路径 | 内容与行为 |
|---|---|
| `runtime/quotes/fixtures/<match_id>/<sha>.json` | 本次新取得的比赛身份响应 |
| `runtime/quotes/raw/<match_id>/<sha>.json` | 每页完整响应文本、解析结构、请求参数、实际取得时间、源/保存文本SHA；意外回显密钥时先脱敏并标记 |
| `runtime/quotes/snapshots/<match_id>/<sha>.json` | 所有原页路径、完整性、候选证据、规范化整行与解析问题 |
| `runtime/quotes/reports/history/<run_id>.json` | 运行、失败、延期、空响应、缺口和预算；同ID禁止覆盖 |
| `runtime/quotes/reports/latest.json` | 可重建的最新报告指针，历史正文仍追加保留 |

内容相同的原件允许幂等复用，文件内容冲突拒绝覆盖。新运行、新取得时点、新价格均形成新产物；旧报价不会被最新价替换。HTTP连接失败只记录安全的错误分类，不打印原异常、鉴权头或账户信息。收到的可解码响应文本即使JSON结构不合法也先留存；没有成功取得的原始内容不能伪造。

## 市场、时间与缺失处理

原始响应保存所有公司和市场。规范化当前只接受ID及名称同时匹配的四类全场市场：

| API市场 | 规范化 | 选择方 |
|---|---|---|
| 1 / Match Winner | `1x2` | home / draw / away |
| 4 / Asian Handicap | `asian_handicap` | home / away |
| 5 / Goals Over/Under | `totals` | over / under |
| 50 / Goal Line | `totals` | over / under |

半场、加时、角球、三项让球、非四分之一球线以及未识别名称不强行映射。原件仍可追溯。只有严格大于1的有限十进制赔率成为参考数字；空、null、NaN、Infinity、1.00、负值或不明格式保留缺失，不能填0。双侧/三项需来自同公司、同市场、同线、同供应商记录；不得拼出最优价。

完全相同的重复报价计数后去重；同一身份的冲突报价都保留并标不完整，不能任选其中一侧。API可能同时通过市场5和50提供同一总球线，保留各自市场ID，不能当作两家独立公司或两场独立样本。

`source_updated_at` 原样保存供应商记录的 `update`。该字段未证明是每个价格自己的变化时点，明确标 `quote_time_semantics=provider_record_update_not_proven_individual_price_change`。`observed_at` 是该行所在页真正取得的时刻；整份快照保存全部页的时刻和最后取得时刻。源更新时间缺失继续未知；源时间晚于取得时间或格式不可信时标不完整，不用采集时间替换。

### 亚洲盘口映射的验证边界

S1真实响应反复出现 `Home -0.5` 与 `Away -0.5` 同号成对、主侧赔率随主盘口单调变化，支持“双方标签共享主盘口键”的解释。规范化保留原标签，并将该键放在候选 `line`；现有 `decision_quotes` 在派生客侧合约时取反。

本轮官方文档网页未取得可读的盘口语义正文，不能宣称供应商文字定义已经核实。所有亚洲盘记录明确标 `line_mapping_status=empirical_inference_pending_provider_confirmation`，不能直接作为已验证合约自动结算。反号标签不自行重配。此缺口应通过供应商定义或可回核的对应公司原页面进一步确认，不能用事后赛果或单纯价格直觉证明映射正确。

## 参考数字、当前开放与实际成交分离

API赔率响应通常没有可认证的当前开放字段。因此即便数字完整，整行 `status=unknown`，`availability_status=unknown`；明确 `blocked/suspended/closed` 则保留关闭。缺侧、坏价或冲突是 `incomplete`。**永远不因数字存在写 `open`，不生成 `open_evidence` 或 `execution_evidence`。**

`reference_values_complete` 只说明原始双侧/三项参考数字是否完整且未见显式关闭，既不是盘口语义验收、实际开放、成交，也不是价格价值判断。其与 `availability_status`、`line_mapping_status` 必须分别看。

输出整行保留 `market/row_id/source_id/sequence/row_order/quote_time/time_precision/observed_at/company/odds_format/odds/line/settlement_basis`，结构可交给 `decision_quotes.adapt_quote_row`。直接适配原 `unknown/incomplete/closed` 行会生成无价格的观察对象；不得为了通过适配器而把原状态改成 `open`。

要将完整1X2或大小球原数字用于参考价纸面结算，使用显式桥接：

```python
from football_model.collect_quotes import adapt_api_reference_row

reference = adapt_api_reference_row(selected_api_row, forecast_id=None)
# 原件为 reference["parent_api_row"]，原状态仍 unknown。
# F2 quote_rows 采用 reference["original_row"]；它明确标 reference_only。
# 正式决定仍须冻结原来源，绑定同比赛、同报价和自己的条件。
```

该桥接只接受已完整解析且未见关闭的1X2/大小球单行，不替你选最新行或最佳价格；AH映射未核实、不完整、关闭或含开放/成交声明的输入都拒绝。派生行保留父行ID、父指纹和原unknown状态，新增 `status=reference_only`；既有F2报价适配器只给予参考 `decimal_odds`，`availability_status` 保持unknown，禁止携带开放或成交声明。旧open/unknown/closed的F2输出结构保持不变，旧包复验不能因本扩展失效。

`reference_only` 可以进入明确冻结的 `QUOTE_REF` 评价，但自身不足以进入 `QUOTE_OPEN` 或 `ACTUAL`；后两类仍要各自的真实开放或成交来源。全局历史截点选行工具没有新增此状态，故必须先在原API观测中按事先声明的选择规则确定一行，再显式派生参考行。桥接只做数值/结构检查，不核外部原件字节；正式决定的来源冻结、身份和人工语义复核仍须执行。本采集器不自行产生推荐或收益样本。

## 报告与验收

稳定报告字段：`source_commit`（真实 `GITHUB_SHA` 或未知）、`run_started_at`、`collection_finished_at`、`generated_at`、`candidate_count`、`counts`、`api_requests`、`api_budget`。`counts` 分开 `collected/empty/deferred/failed/skipped/evidence_only`；每场另列实际页数、预期页数、完整性、整行数、未知/关闭/完整数字数和原件路径。

退出0只代表本轮采集流程没有已知失败；空赔率属于实测无返回，不是推荐弃权或收益0。退出1保留预算延期、资料/提供方/保存失败等部分运行；退出2为入口配置或最终保存失败。状态保存是否成功需看既有云端保存步骤，不能由本命令退出0推定。

`tests/test_quote_collection.py` 覆盖分页完整性和变化、预算/软截止、空/缺失、关闭/未知、市场解析、重复/冲突、当前身份、密钥回显、追加及禁止覆盖。历史S1四响应可用于解析回归，原始 `received_at` 与本次 `parsed_at` 分开；不得把回归产物并入当前采集目录或前瞻样本。真实上线仍须另验新响应、真实日预算和最终状态保存；采集通过不是准确率或盈利通过。
