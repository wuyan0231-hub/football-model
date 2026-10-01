# 欧洲国家联赛接入说明

新增赛事键 `unl`，供应商精确赛事名 `UEFA Nations League`，国家字段 `World`，已核实 API-Football 赛事ID为5。配置模式 `national_baseline`。用户于2026-09-24授权加入并训练。原有14个国内联赛及欧冠的配置内容、模型函数、默认超参数与固定时间留出验证保持不变。

## 训练与隔离

- 欧国联独立保存 `runtime/data/unl.json`、`runtime/models/unl.json`、`runtime/model_history/unl/`、`runtime/validation/unl/` 和 `runtime/predictions/<run_id>/unl.json`；不读取任何国内联赛参数或赛果。
- 复用原时间衰减正则化独立泊松算法，半衰期180天、ridge=3、训练至少180场、被支持验证至少40场。首次不根据测试结果调参。有效样本量和验证限制必须随结果报告。
- 最近3届按供应商实际开始日期选择，不把3届误解为3个日历年。本次实际返回2026、2024、2022届。目标始终是90分钟加伤停补时，FT/AET/PEN只取score.fulltime。
- 完整训练集和留出期训练集的对阵图都须通过原函数连通性检查。不得用留出期结果补训练连接；未知队、样本不足、优化失败或参数触边时不发布模型。
- 欧国联通常有很长间歇。仅对本赛事：数据未变但出现未来赛程，且模型当前或该场开赛时将超过14天时，按真实当前截点重新拟合并验证，以重新计算时间权重。不只修改训练日期。国内联赛原行为不变。
- 每日工作流的all自动包含unl；首次可手动选择unl和train。只增加选择项，不更改原日程或密钥。

## 场地处理与适用边界

`football_model/nations_venues.json` 是人工核实的种子登记表。日常更新使用 `football_model/venue_registry.py`，把核实的新场地合并保存到 `model-state` 分支的 `runtime/registries/nations_venues.json`。英格兰、苏格兰、威尔士、北爱尔兰分别识别。自动核实依据供应商 `/venues` 的明确国家字段及场地身份，不等于每场均经过独立官方复核；最新迁场、封闭场地等仍须按分析框架核查。

- 场地属于名义主队协会：neutral=false。
- 场地属于第三方协会：neutral=true，原模型去除主场项。球队及比分顺序不变。中立场设定未单独经过效果校准，也不量化观众、旅行和封闭场地影响。
- 场地属于名义客队协会：现有接入不支持，明确排除/阻断，避免误当中立场。
- 未登记场地先自动查证并登记，再执行原场地分类；查证失败、资料不足或冲突的历史比赛排除并记录原因，未来比赛标为unsupported，不生成概率。不能根据名义主队猜测场地归属。
- 同一比赛ID的场地冲突、球队ID与名称不符等身份异常须报错。
- `runtime/audits/unl/<run_id>.json` 保存原始记录数、90分钟合格数、场地准入数、排除清单、分届/阶段统计、未来场地覆盖和连通性审计。

场地缺失时即使已经有球队参数，也不表示有可用单场预测。每次实际分析读取最新索引，确认有效预测存在，再使用原函数和同一neutral标记复算。当前比分、预计首发、伤停、轮换、战术、天气、赔率、两回合目标仍由框架v1.6另外核查。国家队名单和长期实力变动并未自动进入此模型。

## 自动核实、保存与新球场预测

每次 `unl` 的数据检查/训练先核实预测窗口内的未知场地，再在剩余预算内核实历史未知场地。每轮最多24个不同查询，并为同轮后续联赛预留普通数据请求量；未处理项留在场地审计中，下次更新继续。现有全局80次请求及重试限制仍生效。

- 正整数ID：查询 `/venues?id=ID`。要求唯一返回同一ID、城市逐字一致、明确国家可映射协会。仅球场名称变化允许登记原始赛程名称为别名，同时保存场地详情原名称；城市或既有协会冲突不自动覆盖。
- ID为0或null：保留该原始值，用 `/venues?name=名称` 查候选；只有名称与城市逐字一致且候选唯一、国家归属明确时登记。解析出的供应商ID单独留在核实证据中，不改写原始赛程。
- 名称/城市缺失、无匹配、多个候选、未知国家、笼统的United Kingdom、城市或协会冲突、接口失败：登记为待核原因，继续保持相应比赛不支持；不填默认主场或中立场。
- 保存核实方法、请求参数、供应商原字段、原抓取时点及缓存标记。重复运行复用已核实条目；当前完整表和历次内容摘要快照随原云端工作流保存/恢复。
- 每次预测包含 `registry_snapshot`，值为相对runtime的 `registries/nations_venues/<摘要>.json`。读取单场预测必须同时获取这个文件；缺失、摘要不符、内容被修改时拒绝接入，不能换用最新表蒙混通过。旧版未带快照的预测继续用其对应源码的种子表校验。

**新球场不需要在历史训练比赛里出现过。** 核实场地只是确定主场项是否适用；球队参数、模型时效、赛前状态和其他接入条件仍分别检查。本模型不学习球场专属参数。登记后在同一次正常流程中重新生成预测，不要求先积累这座球场的历史赛果。

训练算法、超参数、90分钟目标与固定时间留出验证保持原样。现有训练指纹包含场地证据，登记表扩展可能触发按原规则真实重拟合；不得只刷新训练日期或手工修改旧unsupported预测。每日全联赛日程保持原样；场地适配相关代码发布到main时先运行测试，再自动执行一次unl更新验证。

登记审计：`runtime/audits/unl/<run_id>-venues.json`。当前表在`model-state`，种子表在`main`，两者用途不同。旧预测、旧模型及旧登记快照保留原版本。

接口依据：[API-Football官方入门文档](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide)、[场地API文档](https://www.api-football.com/documentation-v3#tag/Venues)。

## 分析入口

`python -m football_model.integration match --runtime runtime --config config.json --league unl --match-id 实际ID --state-commit 实际状态提交 --output runtime/match_calls/实际文件名.json`

数字必须来自实际已保存预测；不以模型已训练替代本场可用性检查。未有有效预测时，按v1.6定性路径继续，模型概率与EV标为未量化。

## 官方赛程与场地抽查

- 2026/27赛季欧国联欧洲当地2026年9月24日起举行；前四轮至10月6日，第5—6轮11月12—17日。
- https://www.uefa.com/uefanationsleague/news/02a2-1fe8e0405d9d-cc8f1ee7068b-1000--draw-for-the-league-phase-of-the-2026-27-uefa-nations-league/
- https://www.uefa.com/uefanationsleague/match/2034548--slovakia-vs-belarus/matchinfo/
- https://www.uefa.com/uefanationsleague/match/2043048--iceland-vs-kosovo/matchinfo/
- https://www.uefa.com/uefanationsleague/match/2040057--belgium-vs-israel/matchinfo/
- https://www.uefa.com/uefanationsleague/news/0297-1d597601d9a2-f305608efca7-1000/
