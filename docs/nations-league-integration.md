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

`football_model/nations_venues.json` 是基于本次供应商场地三元组（ID、名称、城市）人工建立的协会归属登记表。英格兰、苏格兰、威尔士、北爱尔兰分别识别。未对每场历史场地独立复核；已对迁移场地和决赛举办地进行欧足联来源抽查。该分类以供应商场地资料正确为条件，不代表已完成全部赛场质量验收。

- 场地属于名义主队协会：neutral=false。
- 场地属于第三方协会：neutral=true，原模型去除主场项。球队及比分顺序不变。中立场设定未单独经过效果校准，也不量化观众、旅行和封闭场地影响。
- 场地属于名义客队协会：现有接入不支持，明确排除/阻断，避免误当中立场。
- 缺少或未登记场地、登记元组不一致：历史比赛排除并记录原因；未来比赛标为unsupported，不生成概率。新场地须核实后加入登记表，不能根据名义主队自动猜测。
- 同一比赛ID的场地冲突、球队ID与名称不符等身份异常须报错。
- `runtime/audits/unl/<run_id>.json` 保存原始记录数、90分钟合格数、场地准入数、排除清单、分届/阶段统计、未来场地覆盖和连通性审计。

场地缺失时即使已经有球队参数，也不表示有可用单场预测。每次实际分析读取最新索引，确认有效预测存在，再使用原函数和同一neutral标记复算。当前比分、预计首发、伤停、轮换、战术、天气、赔率、两回合目标仍由框架v1.6另外核查。国家队名单和长期实力变动并未自动进入此模型。

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
