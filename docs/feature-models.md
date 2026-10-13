# 特征修正与goalmodel独立对照：实施入口

当前框架：v1.7修订批次十。新工程由用户2026-10-12明确授权自主实施、测试和发布，无阶段审批要求。panna暂缓。本期数据、程序与影子资格按各自原件报告，不承诺准确率或盈利。

## 实际命令

```bash
python -m football_model.feature_schedule --runtime runtime --config config.json --league serie_a --source-commit ACTUAL_SHA
python -m football_model.feature_research study --runtime runtime --league serie_a --source-commit ACTUAL_SHA --state-commit ACTUAL_STATE_SHA --protocol feature-models-v2.json --run-goalmodel
python -m football_model.feature_snapshots capture --runtime runtime --config config.json --league serie_a --source-commit ACTUAL_SHA --state-commit ACTUAL_STATE_SHA --protocol feature-models-v2.json
python -m football_model.feature_scoring score --runtime runtime --config config.json --league serie_a --source-commit ACTUAL_SHA --protocol feature-models-v2.json
```

SHA为真实完整提交，不是字面占位。研究可无R运行M1，但未运行R时不得称G0/G1对照已完成；正式云端研究启用R并先执行真实R测试。原生产180天与F5-C继续原身份，候选不默认串联、不自动晋级。

## 第一批v1工程与历史身份

- 历史数据覆盖表只描述已读取runtime与端点原件范围；已有/fixtures缓存先盘点再补采。
- 俱乐部全正式赛事赛程：逐队逐供应商赛季查询，核league metadata及友谊赛、主联赛原比分/身份，保存未尝试/失败/延期及预算；全部供应商查询返回也不等于独立赛事清单完备认证。国家队个人负荷仍未纳入。
- OOF回放：180天、原ridge=3基线，按30天块过去数据重拟；EARLY=开球前10小时。目标延迟沿用开球+4小时历史假设，未来/未成熟目标不评分；历史现在取得的资料不是当时PIT快照。
- M1：四赛程特征、共享主客系数、固定logλ offset、无额外截距、训练内加权标准化、L2；alpha网格0.01/0.1/1.0仅内层时间验证选择，样本不足回退最强惩罚。参数与权重口径写入JSON，alpha不等于原ridge或F5α。
- G0/G1：官方固定goalmodel提交84ecd6c2bbad3ccb967abf88ef49e5bcd074e545，真实R函数独立估计；G0无用户协变量，G1与M1同定义协变量，两者共同训练样本。R只用官方核心源文件与jsonlite，不安装其整套示例数据依赖。GPL-3源码/许可/指纹在third_party。其无ridge，与M0训练器不声称数学等价。
- EARLY 9–11小时窗口；LINEUP以开赛前90分钟内真实两队11人确认为门槛。记录真实时刻、原件闭包、首失败与第一合法版本；当前数值候选只修正赛程，首发被取得也不称已量化人员。

本期未将xG、射正或球员评分接入数值；它们待完整覆盖与独立协议加入。换帅/战术仍结构化定性，不手调λ。

## 自动执行

`.github/workflows/feature-models.yml`：正式main发布和每日北京时间09:47做历史研究，试点快照每30分钟检查一次，遵循现有ENABLE_SCHEDULE变量；原09:17生产日程不改。GitHub可能延迟，晚到如实记录，不补造按时。原model-state并发锁、API真实5000日额度、分钟限速与状态容量门保留。

工程分支push只做全量回归、真实R接口训练、固定状态历史开发，不请求付费API、不保存model-state；main执行俱乐部赛程/当前事实采集、研究和新原件保存。任务必须按每步骤真实outcome及最终状态提交核验，不能用绿色continue-on-error或旧latest冒充成功。新的配置/源码不使旧快照获确认资格。

## 工程产物

`runtime/feature_models/`下保存schedules、models、reports以及双时点队列/原件。模型卡记录source/state父提交、数据hash、protocolhash、β、标准化与贡献、训练截止、用途；不可变内容寻址原件保留，latest仅为定位。新快照的完整原始输入和模型卡按hash冻结，评分不得再读取晚来的latest。

研究latest保存带完整报告path/hash的摘要，完整比分矩阵、OOF/训练输入和R原件仍在不可变报告。新特征命名空间的64位内容寻址JSON使用既有无损gzip传输/原字节恢复，模型卡与latest入口直读；不删历史、不放宽原物理/逻辑容量上限。远端历史读取按固定状态的state_storage清单恢复，正常工作流先完整load再评分。

开发主指标为完整比分负对数概率，辅以1X2对数/Brier、平局、总球、净胜2+/3+校准。v2诊断另列完整总球／净胜投影的NLL，以及客队净胜2+/3+事件；主指标不变，各事件保存实际分布与对应分母，不凭新增诊断择优宣布通过。对照报共同成功样本及含回退的全队列；G1-G0直接比较识别协变量增量。失败/未知球队/暖期/资料缺失不删除。

五态计算继续由既有settlement执行；合法报价和费用不充分时EV未知/条件算术，参考不当开放或成交，不能以新点EV越过原诊断用途。真实收益暂无新证明。

未来评分读取首次真正候选捕获的完整冻结闭包，达到开球+4小时后独立查询API常规比分，主客/比赛/赛事/时间严格核验。两时距分开评分、独立比赛去重；首评分不可变，7日内每24小时有限复核，赛果修订追加。空队列/未成熟不初始化API；失败和未知不记0，不算无报价收益。

## 后续效果门

旧`feature-models.json`仍是v1开发及未来工程影子协议；本轮另立`feature-models-v2.json`，显式声明`research_design_version=feature-research-v2`，两者均保持confirmation_active=false、production_promotion=false。确认期需在首场前另定有意义δ、允许退化ε、成组样本量、主检验/多候选及停止规则，并不可变持久保存；不得套F5的40场或流程30请求门证明准确率。三场已见赛果继续开发案例。持续未来样本积累不等于效果已获确认，未满足效果门时保留原基线及候选研究身份。


## 修订批次十：独立v2的增补边界

本节规定本轮新工程的验收和使用要求。上面的v1实现说明保留历史属性，不由新文档推定v2已实现、已部署或真实运行成功。实际状态、源码／状态提交、测试范围及保存回读由真实回执另列。

| 分支／比较 | 训练与预测池 | 可解释的差异 |
|---|---|---|
| M0 | 原180天／ridge=3过去训练的比分基线 | 参考基线，历史回放与每日生产身份分别保存 |
| M1相对M0 | 同场同截点的基础λ，过去OOF记录训练固定offset修正 | 首批四赛程特征的增量；失败按冻结规则回退基线 |
| G0_full相对M0 | 完整合格比分训练池、相同时间／目标边界，无用户协变量 | goalmodel基础结构与参数约束差异；不是纯特征作用 |
| G0相对G0_full | G0使用特征完整训练池，G0_full使用完整比分池；均无用户协变量 | 因特征完整性筛样造成的训练池差异 |
| G1相对G0 | 两者使用同一特征完整训练池和可比较预测目标，G1加入同定义特征 | 联合模型协变量的增量 |

每次比较保存双方真实训练ID、特征合格／排除ID、同截点预测ID及共同评分分母；训练池／时间边界不能对齐时仅描述总差异，不作单因素归因。goalmodel各分支分别记录requested、未运行、拟合失败、未收敛、逐场未知球队／预测失败、已预测及回退。保留失败R原件和真实退出状态，不只汇总成功样本；未运行R不能称三分支对照完成。

历史研究报告保留完整目标队列，所有分支先记录请求再过滤。共同成功概率评分与包含失败回退的政策评分分别报告；基线也失败的目标仍在全请求分母，数值未知，不填0。G0_full工程入口是独立基础对照，不改变生产攻防模型或原固定留出验证。

未来评分须结合预定机会台账、首尝试／首失败、首合法冻结基线及首候选捕获，而非只以first_candidate成功文件决定分母。在有合法基线和独立赛果、且预定回退规则适用时，候选缺失／失败按同一冻结基线分布评价回退政策；没有合法基线、赛果或时点证据时保持未评分及原因。候选成功共同子样本、基线单侧可用、双侧失败与完整回退队列各有分母。两个时距分开，跨时距和重复版本按比赛去重；后续首合法成功不删除首失败。

各goalmodel分支的历史与未来能力分别登记。只有已实际接入捕获并保存完整合法原件的分支才作未来比较，未接入的分支写未执行；历史R回执不能代替未来候选捕获。当前数值研究仍限赛程特征，xG、射正、轻量阵容待另外的数据门禁和候选版本。

v2在研究、快照与评分命令中显式选择协议。协议hash及设计版本写入模型卡、队列、载荷和评分；`research_artifact_namespace=protocol_hash_v2`时模型入口为`runtime/feature_models/models/<league>/<protocolhash>/latest.json`，研究摘要入口为`runtime/feature_models/reports/<league>/<protocolhash>/latest.json`，完整原件仍内容寻址；旧v1默认路径不变。不得给旧快照换协议hash、改ID或复制成为v2成功／确认样本。工作流继续显式传feature-models.json执行旧v1研究、捕获及评分，同时新增v2；同一model-state锁内共用一轮事实／赛程采集，不为两份协议重复付费API采集。v1队列不中断，其结果保留旧版本，不合并为v2主样本。

工程回归、历史开发、真实未来流程、远端持久保存、概率改善和收益改善分别报告。完整队列概率评分不等于完整选票策略ROI；缺合法报价、费用或冻结行动仍不报策略收益。新增v2不激活正式确认，不继承F5-C样本、α或成熟门槛，不改变原09:17生产日程、原180天参数和F5-C数学政策。
