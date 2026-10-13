# 高级特征、未来对照与同价决定：批次十一实施入口

配套完整框架为v1.7修订批次十一。本页说明实现入口、数据准入和验收口径，不是部署或效果回执。每轮实际源码／状态提交、命令、测试、真实数据运行、远端保存及回读另以运行原件核验。用户2026年10月13日已要求自主继续完成规划中的可执行工作，无逐阶段审批要求；不能以此替代未发生的未来样本或未取得的数据。

生产基线仍是原攻防泊松模型，180天表示历史权重的半衰期：样本早180天权重约减半，更早合格比赛仍可参与；不是只训练最近180天。F5另外的过去180天选参窗口保留其自身含义。旧`feature-models.json`、`feature-models-v2.json`与F5-C保持原身份、原首输出、失败和队列；本批不把旧样本升级为高级模型或正式确认样本。panna按用户先前决定暂缓。

## 范围与分支

新工程协议为`advanced-models-v1.json`，当前用途为历史开发与未来工程影子。各非仅审计配置赛事独立拟合，国内与国家队不混参数；`--league all`表示请求该配置范围，不意味着每个联赛已取得高级数据、完成拟合或具备未来有效预测。欧冠原仅审计身份不改变。

| 分支 | 使用的特征组 | 截点 |
|---|---|---|
| M_schedule | 原四项赛程特征 | EARLY |
| M_shots | 赛程＋射门／射正代理 | EARLY |
| M_xg | 赛程＋同源历史xG | EARLY |
| M_lineup_early | 赛程＋历史首发延续 | EARLY |
| M_shots_xg | 赛程＋射门／射正＋xG | EARLY |
| M_shots_lineup | 赛程＋射门／射正＋历史首发延续 | EARLY |
| M_full_early | 赛程＋射门／射正＋xG＋历史首发延续 | EARLY |
| M_lineup_confirmed | 赛程＋本场确认首发相对上场XI的延续 | LINEUP |
| M_lineup_minutes | M_lineup_confirmed＋本场首发的历史分钟留存 | LINEUP |
| M_lineup_rating | M_lineup_confirmed＋本场首发的历史评分代理；不要求分钟留存分支可用 | LINEUP |
| M_full_lineup | 赛程＋射门／射正＋xG＋确认阵容＋历史分钟留存＋历史评分代理 | LINEUP |
| M_reported_absence_early | 赛程＋本场真实已报告缺阵者的近期分钟份额／上场XI份额 | EARLY |
| M_national_load_early | 赛程＋历史首发延续＋限定欧国联已观察出场分钟／XI匹配覆盖 | EARLY |
| M_national_load_lineup | 赛程＋确认XI延续＋限定欧国联已观察出场分钟／XI匹配覆盖 | LINEUP |
| G0_full | 完整合格比分训练池，无用户协变量 | EARLY／LINEUP分别记录 |
| G0 | M_shots_lineup特征合格训练池，无用户协变量 | EARLY／LINEUP分别记录 |
| G1 | 与G0同训练池及预测目标，使用M_shots_lineup同定义协变量 | EARLY／LINEUP分别记录 |

M系列共14个独立offset候选，每个都有固定时距；其中11个为原高级候选，另3个为本轮接续加入的已报告伤停和限定国家队负荷候选。各分支使用过去样本外M0的logλ作固定offset，主客共享系数、不另加截距，以训练内预处理及L2拟合。当前协议至少40个完整历史OOF比赛才拟合，0.1／1／10惩罚网格仅在过去训练数据内按时间选取；它们不是原模型ridge或F5的α。每个组合有独立可用性、训练池、卡及失败状态，不能用M_schedule成功代表M_full或新增人员候选成功。

G0_full与M0完整训练池的精确对齐仅适用于EARLY，该比较才可在其时间边界内解释结构差异。G0_full／G0／G1的LINEUP预测复用真实EARLY拟合参数，并在LINEUP截点重建预测特征，与未来运行保持同一政策；LINEUP的M0训练年龄／训练池可不同，不能宣称这一时距也是同池纯结构比较。G0相对G0_full包含特征完整性筛样影响；G1相对G0才是相同池下的协变量增量。M系列另按协议固定的父子组合比较新增特征，并在子分支实际完整训练池上重拟父分支作同池消融；只有相同预测目标／时间边界与可比训练池时才作限定归因。不把同时换库、换训练池、换特征的差异称为单项效果。

## 特征实际含义与数据边界

射门／xG／常规人员统计使用目标截点前本赛事最近5场历史，最低历史数由协议固定为3；不同字段各有覆盖与缺失，不能跳过最近缺失比赛去凑旧样本。已报告伤停另要求该近期窗口内每场球员分钟完整，并核上场真实XI；限定国家队负荷另用协议冻结的14天欧国联窗口。特征列顺序以代码`FEATURE_GROUPS`及候选卡为准。

- 射门代理包含本队场均射门、射正，以及对手历史被射门、被射正。它是可训练的机会代理，不是完整战术或机会质量模型。
- xG只取合法同源的本队历史xG与对手历史被创造xG。null、未提供、非法、明确0分开；缺xG不以射门乘任意系数或0补填。
- `M_lineup_early`只描述已知历史首发延续；独立的`M_reported_absence_early`另消费本场真实已报告缺阵，`M_national_load_early`以本赛事上场真实XI作为人员代理。三者均不读取本场最终XI，也不把历史XI代理称为本场预计首发概率。
- LINEUP将XI延续、分钟留存和历史评分拆成独立数据组。XI延续不以球员分钟资料齐全为前提；分钟留存要求完整分钟资料，供应商替补null不能擅自填0。评分扩展以同一过去评分表计算“本场XI平均历史评分−上一场XI平均历史评分”：球员历史评分按实际已观测出场分钟加权，两套XI每名球员都须至少90分钟可用评分，本队至少3场存在已观测评分及分钟的历史比赛；缺失评分／分钟不参与计算、不填0，覆盖另列。评分是代理，不是已证明的因果球员能力值。目标比赛自己的赛后分钟和评分不能进入预测。
- 已报告伤停已接入独立可学习特征组，计算其相对于近期实际用人的分钟份额及上场XI份额；数值对λ的作用仍须由过去合格样本拟合，不使用“缺一个人扣多少”的人工幅度。覆盖未知、空返回、疑似状态、同刻冲突及近期分钟缺失均保留缺失，不补0。近期完整原件中已无出场的长期缺阵者，其近期使用份额为0，不再另作人工扣减。此组只用于EARLY，LINEUP由确认XI反映实际替换，避免重复加入同次人员变化。伤病严重程度、真实替代质量和EARLY预计阵容权重仍未因此解决。
- 历史射门／xG、过去球员分钟及LINEUP研究使用后来下载资料时明确历史可用性假设；历史确认XI不能追认为当时提前10小时已知。**本场伤停没有此回填例外**：历史训练也必须具有当时截点前真实取得的伤停及覆盖上下文，否则该目标没有合格伤停特征。真正未来的所有使用来源均须有截点前真实取得与保存链。
- 赛程特征继续按俱乐部跨赛事来源准入，供应商全量分母尚未独立核定的限制保留。新增个人负荷只覆盖已核欧国联赛程、球员ID及出场分钟，并显示XI匹配人数和比例；其他国家队赛事仍不在范围内。未匹配者不填0，不称全队完整负荷或全球国家队覆盖；`all`只是请求配置赛事范围。

采集优先复用已获准的`/fixtures?id=比赛ID`详情中实际存在的statistics／lineups／players，紧凑投影包装只指向原始响应及其hash，明确字段路径，不伪装额外网络响应；已有合格原件复用不耗新请求。缺球员字段保持不支持／延期，不调用另一个`/fixtures/players`接口；其他缺字段只按客户端原白名单处理。FT球员分钟允许供应商实际记录的伤停补时，未识别球员隔离保留原行、不编造ID，球队／比赛身份冲突继续拒绝。这样解析通过也不等于分钟完整、所有球员有评分或历史资料具备当时可得资格。

采集器按真实5000次日额、当日已用量、分钟限速、缓存、重试、软截止和生产请求预留运行，逐比赛逐资料类保存已有、缺失、受限、延期和错误。纯额度、软截止或容量延期返回`execution_status=deferred`、`status=partial`及`full_collection_completed=false`，CLI可正常退出；容量原因明确为`storage_capacity_deferred`。真实数据／身份／schema／保存等错误保持非零退出。意甲与all分别保存`advanced_features/collection/by_selection/<selection>/latest.json`，不能拿阶段成功覆盖另一阶段结果。它可续跑，不为赶一次运行写成功；全部历史逐场三类资料可能需要多日配额。供应商覆盖标志不等于逐场实际字段齐全。

采集缓存及研究新文件在写入前核实际字节增量与压缩传输成本，保留128 MiB逻辑／32 MiB物理余量供报告及后处理；原4 GiB逻辑、1 GiB物理及60 MiB单文件硬上限不提高。研究原件使用`advanced-compact-json-v1`无损内容寻址、共享子树去重和至多4 MiB的分块，按完整引用及hash重建原JSON值；不能只读外层包装或少量成功行，也不删除失败／来源来减体积。空间耗尽保持延期及完整待办，不把分块工程当成长期容量已验收。

## 实际入口

以下`ACTUAL_SOURCE_SHA`、`ACTUAL_STATE_SHA`和比赛ID须换成本轮真实值，命令示意不构成运行回执；使用当次固定源码的`--help`核参数。

```sh
python -m football_model.advanced_collection --runtime runtime --config config.json --league all
python -m football_model.advanced_player_load --runtime runtime --config config.json --national-league unl --max-seconds 300 --reserve-requests 400
python -m football_model.advanced_research study --runtime runtime --config config.json --league all --protocol advanced-models-v1.json --source-commit ACTUAL_SOURCE_SHA --state-commit ACTUAL_STATE_SHA --run-goalmodel
python -m football_model.collect_fundamentals --runtime runtime --config config.json --league all --horizon-hours 11 --recent-matches 5
python -m football_model.advanced_snapshots capture --runtime runtime --config config.json --league all --protocol advanced-models-v1.json --source-commit ACTUAL_SOURCE_SHA --state-commit ACTUAL_STATE_SHA
python -m football_model.advanced_scoring score --runtime runtime --config config.json --league all --protocol advanced-models-v1.json --source-commit ACTUAL_SOURCE_SHA --state-commit ACTUAL_STATE_SHA
python -m football_model.advanced_confirmation --runtime runtime --league serie_a --advanced-protocol advanced-models-v1.json --protocol advanced-confirmation-v1.json --auto-activate
```

高级采集器保存statistics／lineups／players的原始来源引用与明确投影，与旧事实采集共用实际已取得来源。当前伤停由`collect_fundamentals`取得并冻结，高级人员模块消费这些原件；负荷由独立`advanced_player_load`采集。研究仅能使用届时已有且合格的过去原件，刚取得的新未来伤停不会补成旧比赛的训练样本。研究命令不带`--run-goalmodel`时不能称R分支已执行。正式R训练固定`third_party/goalmodel`源码及依赖；未来捕获按已保存实际R参数确定性计算并核工程探针，不把探针合成对阵算真实比赛。

普通单场的报价计算接入为：

```sh
python -m football_model.integration match --runtime runtime --config config.json --league serie_a --match-id ACTUAL_MATCH_ID --as-of ACTUAL_ANALYSIS_ISO --state-commit ACTUAL_STATE_SHA --decision-input reviewed/current-decision.json --output runtime/match_calls/actual.json --html runtime/match_calls/actual.html --zip runtime/match_calls/actual.zip
```

`ACTUAL_ANALYSIS_ISO`必须是本次真实分析截点，并与输入的`analysis_as_of`一致；不能拿后来资料倒填旧赛前资格。`--decision-input`是runtime内实际保存、已审核的`feature-decision-input-v1`对象，包含绑定本场及实际分析截点的整行报价、冻结来源引用与hash、审核和取得时间、可选费用及明确选择。不是把任意JSON或旧审计模板改名后提交；实际字段和报价schema按`feature_decisions.py`、`decision_quotes.py`及固定源码检查。缺输入仍输出候选读取状态，价格计算保留`no_quote_supplied`；未知费用不默认为0。

需要把实际判断一并核验时，在此对象内增加`decision_record`（完整原生`s2-decision-1.0`对象，沿用[单一决定入口](s2-decision-record.md)）及`decision_calculation_links`（每项为`{"candidate_id":"实际候选ID","model_key":"本次model_records中的实际键"}`）。该字段把分析者已声明的候选绑定到真实计算，不替分析者生成候选、四态或主票。`decision_record`的比赛、`analysis_cutoff`、`kickoff_at`和完整报价原行必须与本次接入一致；其`inputs`和`derived_artifacts`按原schema保留真实路径、摘要、时间和依赖。未提交原生决定时仍可只计算，不能将旧`selection`单票声明称为完整决定审计。

原截图或原文位于本次显式授权的外部目录时，可在上述命令追加`--decision-source-root ACTUAL_SOURCE_DIRECTORY`，并让`--decision-input`指向该目录内的真实输入文件。程序只复制该输入及其明确列出的来源，保存到`feature_decisions/sources/<原字节SHA256>.<扩展名>`与不可变输入目录，记录路径迁移及真实归档时间；原始输入另存。图片仅在这个专用目录准入PNG、JPG／JPEG、WebP，仍受原单件／整体大小及内容检查，不扩大整个runtime的任意文件上传范围。保存成功不表示已经实际看图、审核转录正确或恢复历史盲测资格；`reviewed=true`仍须对应真实执行者的回图证据。

外部归档也处理嵌入`decision_record`显式列出的输入和派生原件，按原字节复制并只迁移路径，保存可逆映射；未列出的目录内容不扫描。决定中的证据时间、事实、主票及等级不因迁移改写。

## 未来捕获与评分

EARLY窗口为开球前9—11小时；LINEUP为开球前至多90分钟并实际取得合法两队XI。历史LINEUP研究的45分钟截点假设不保证真实接口该时刻有首发。只接受真正未赛且来源／输出时序合格的记录；30分钟调度不能保证任何指定分钟必然获得资料。

先从冻结完整生产父行及原函数重建M0，再重建高级原件、特征、实际卡／系数和候选矩阵。goalmodel保持其已拟合分布。候选绑定同一首合法基线捕获，后来的候选成功不得覆盖首次失败；真实错过窗口、不合格、未到期、缺预测和待赛果各自计量。两个时距、多个候选与多次尝试不是多场独立比赛。

全机会台账先登记再判资格，评分不只扫描成功预测。合法M0存在、候选失败且协议准许时，政策评分回退该M0，并同时报告候选原生成功子样本、基线单侧、双侧失败和全部请求分母；没有合格M0或结果时保持未知。不把预测在后来损坏或赛程修订后已不合格的记录偷偷换成新的成功，保留原评分版本和当前验证状态。继承账本中的同比赛／同两队若仅开球时间改变，赛果同步追加`unscorable_schedule_revision`及新旧来源引用，原预测字节不改、不为不匹配的旧预测追加结果或分数；其他比赛／球队身份冲突仍按真实错误处理，不能借赛程修订放行。

| 原件位置 | 内容与核验 |
|---|---|
| `runtime/advanced_features/evidence/` | 内容寻址的实际历史统计、阵容、球员原件 |
| `runtime/advanced_features/collection/` | 完整采集请求、已有来源、错误和预算延期 |
| `runtime/fundamentals/snapshots/injuries/`、`runtime/fundamentals/fixtures/` | 本场伤停原响应、比赛身份和覆盖上下文；伤停必须真实在截点前取得 |
| `runtime/advanced_features/player_load/` | 限定欧国联赛程分母、球员分钟、采集失败及待续请求 |
| `runtime/advanced_models/<league>/<protocol_hash>/` | 各分支卡、训练输入、报告、失败和确认记录 |
| `runtime/advanced_snapshots/<protocol_hash>/` | 独立机会队列、捕获、冻结输入和首记录 |
| `runtime/advanced_scoring/<protocol_hash>/` | 实际结果绑定与独立评分版本 |
| 单场包`feature_decision_comparison` | 同父候选状态、五态／EV计算、明确选择和证据闭包 |

所有目录只作定位，实际可用记录必须沿引用逐hash恢复；状态归档按固定提交真实`state_storage`清单核原字节。latest只是入口，最近失败与最后成功分别报告。

## 调度及执行回执

独立工作流为`.github/workflows/advanced-models.yml`。当前配置每小时第10／40分钟尝试未来处理，每天UTC 02:07（北京时间10:07）尝试补采及完整研究；原生产北京时间09:17日程保留。定时任务受`ENABLE_SCHEDULE`及私有仓库条件控制；main使用与生产／旧影子相同的`football-model-state`互斥锁，不并发改状态。工程分支只读恢复状态作研究验证，使用分支独立并发组，不占生产状态锁，也不保存生产状态；该分支的新运行可取消其旧运行。调度延迟、长研究、API配额及排队都可能错过捕获窗口，每30分钟只是尝试频率，不保证30分钟内完成或有有效首发。

完整研究的main运行先执行独立短任务`pre_study_capture`：恢复固定状态、验证已有持久证明、同步当前资料、检查确认、捕获已有合法候选并立即保存后结束该job。后续`advanced`任务重新恢复这个真实状态，绑定保存提交与该短任务的服务端完成时间，再进行耗时补采和研究；研究后再同步／捕获。持久证明必须核固定run attempt、实际已完成job日志中的精确状态SHA、服务端`completed_at`和完整原件闭包，不能只信本地提交时间或metadata自报。短任务失败按真实状态阻断后续，已保存部分成果与失败不抹去。

这种拆分避免已经捕获的资料等待整段研究结束才首次持久保存，但工作流仍占用共享状态锁；长研究期间后续LINEUP窗口仍可能错过。不能由短任务存在宣称不再丢窗口，实际错过照常进入机会分母。

main发布及手动运行按工作流实际分支执行，工程分支研究不能冒称正式API补采／远端状态保存。每项读取其真实step outcome和对应报告：预算／软截止／容量的正常延期保留未采集分母，不能把正常退出等同采集完成；真实错误、研究失败、未来捕获、独立评分、确认阻断与持久保存分别展示。最终发布及未来评分以本轮固定回执为准，不由文档预填。

2026-10-13首次云端工程分支运行[38340613164](https://github.com/wuyan0231-hub/football-model/actions/runs/38340613164)，源码`8752866838df22a86d1a0d2dd8c8e10910b922da`，已执行19联赛R研究及当次测试，但随后状态检查因逻辑容量超限失败；没有据此保存新生产状态。这次失败是后续紧凑原件与写入前容量保护的修复依据，不是main发布或修复后云端验收回执。

## 同价计算、选择与报告

普通接入的新增计算链检查赛程v2、高级EARLY／LINEUP及已核同父F5-C；旧v1仍用原读取及评分入口。本链拒绝不同生产父行的混配。每个合法候选从完整矩阵计算相同合约与参考价下的五态、盈利／不亏概率、费用前EV、有明确费用后的净EV、与M0差值及正负分歧。报价使用真实截点选行；未认证API亚洲盘不因进入高级模块就解禁。

旧可选`selection`声明仍绑定一个实际计算ID，记录模型、报价及方向；它只是一张票的声明。有`decision_record`时，程序实际复用`decision_record`原件读取、一次编译、单关／串腿各自的`decision_audit`及影子等级校验，再把每个声明候选绑定到同一合约、同一报价下的真实计算。保存的EV或保本价必须与该计算一致，并绑定实际预测ID；研究候选不能声明确认正／负价值。若同时提交旧`selection`，它必须与原生决定的主票计算一致，不能保留两个冲突主选。

最近替代沿用原生`decision_chain.strongest_alternative_candidate_id`和`alternative_reason`，没有替代须保留`no_alternative_reason`。同一声明票上所有候选相对M0的净EV正负分歧都列出，即使最终选的是M0也不隐藏。分析者在`decision_chain.model_disagreements`逐条声明`candidate_id`、`model_key`、`treatment`、`reason`和`information_basis`；`treatment`为`retain/reduce/switch/condition/reject`之一。缺少分歧处置或用途判断标`incomplete`，引用、时间、报价、数值或主选冲突标`blocked`；合法同价计算仍保留，计算成功不等于决定核验通过。未知费用不能生成净EV正负分歧，价格及费用限制继续显示。

HTML可见全部候选各自的单关／串腿四态、角色、主选、最近替代与理由、实际分歧处置、条件、影子等级和数值绑定，ZIP保存原件与核验结果。这是本次单场包中的**只读S2核验派生视图**，不另建或发表正式F2决定包，不改旧包，也不证明独立GPT运行、足球语义正确或成交。需要原生F2包及追加事件时仍使用`decision_record build`等入口。程序不按最高EV擅自改主票；四态由分析者依完整框架判断，A/B/C/D/U仍是影子用途。没有两份真实独立GPT输出，不声称已经测得框架政策效果；没有联合分布和整串票据，不报告整串预测或实盘ROI。

## 确认准备与自动执行边界

`advanced-confirmation-v1.json`预先固定主比分NLL、最小有用改善0.01、1X2／总球／双方净胜尾部不退化容差、覆盖和样本量设计。这些是研究设计假设，不是已证明的经济门槛。每个登记联赛×候选×时距为一个假设，按整个登记家族及指标校正，不能逐日挑最好的一项。

当前配置14个offset分支各有其固定时距，3个goalmodel分支各对应EARLY／LINEUP两个时距，因此每联赛为`14＋3×2＝20`个确认假设；19个登记联赛共380个假设，另对5项指标作家族校正。这个登记总数包括数据不足、未拟合、被开发门阻断的候选，不表示380项已激活，也不因只挑成功候选而缩小家族。冷启动诊断不进入这组确认。协议hash和候选集合变更需另立实际身份，不能借文件名相同延续旧样本。

当前设计要求至少60个有效开发配对及规定覆盖，按开发差分方差上界作单项检验的80%正态近似功效规划；对主指标，这只针对假设真实改善为0.01时拒绝“零改善”。终局还要求实际样本平均改善至少0.01、其他指标与覆盖全部过门，因此整套终局通过率不保证80%；真实改善恰为0.01时，仅样本均值门在规划正态模型下就使主指标联合通过率至多约50%。该规划与终局配对t区间仍依赖比赛差分的独立性假设，同队／时间相关性没有因此消失；初始基线暖期按明确规则单列，未知球队和其他失败保留。只有通过实际重算开发门、从已保存训练块参数／特征重建M0及候选预测、模型卡与协议绑定等检查，`--auto-activate`才保存不可变激活记录。未通过留下阻断回执，不能把运行退出正常称为已开测。开发报告、模型卡和激活记录另绑定实际算法／特征／R桥接等文件的`implementation_fingerprint`：仅文档提交不使同一算法失效；尚未激活而实现已变时必须重新研究，已激活后实现变更则要求新协议，不能沿旧正式样本继续计数。源码提交身份仍独立保存，文件指纹不代替远端持久证明。

真实激活后起点延后24小时，按预定数量的首批唯一机会、最长730天和7天结果成熟等待运行；监控错过的应入组机会仍消耗首N名额并保持无资格，不能用后来的比赛补招；每日监控在终局前不读取正式确认评分。停止、有效配对不足及失效按协议给结论不充分，不能不断延长至显著。源码中的准备协议不等于已激活，工程影子仍不是正式确认。即便概率确认通过，也不自动改生产、更不证明收益；采用须有独立范围适用和实际实施记录。

独立冷启动函数为`football_model.cold_start.score_cold_start(model, fixture)`，版本`unseen_team_league_centred_prior_v1`；高级研究在M0因未知球队失败时另存`COLD_START`诊断分支。它保留已知对手参数，只给未见球队零中心攻防先验，明确未知ID、训练场数0、无已校准不确定性及不与M0配对。未来捕获遇到原M0真实未知球队unsupported时，也可冻结同一原模型、赛程和来源生成独立候选，并对其单侧预测评分；原M0的unsupported保留，新增覆盖与共同可评分集分开。未见球队没有真实球队参数，不包装成问题已在预测效果上解决。此分支不是正式确认协议中的主候选，也不自动成为生产预测。

## 如何判定本轮完成度

必须分别回答：代码是否存在且针对性检查通过；数据请求是否实际发出且报告完整；各分支是否真正拟合／产生预测；本轮保存及回读是否完成；多少数据受限／未到窗口／未来待赛果；主要指标是否改善；正式确认是否激活／完成；生产是否采用。测试数量、Action绿色、全部命令运行完和文档升版均不能代替这些问题。

本轮已有可执行的伤停特征和限定欧国联个人负荷采集／特征程序，不能再把这两项笼统写成“未接入”；其实际数据、拟合及效果仍须分别验收。尚未发生的未来比赛、API实际缺失字段、预计阵容的可靠权重、完整球员能力、其他国家队赛事负荷及真实投注价格和执行仍有各自未解决范围。已有调度只能按已部署流程继续采集、拟合、捕获及评分，不能制造缺失资料或尚未发生的结果。逐项数据阻断应披露真实数量与原因；未验证效果明确写未验证，不能总括为“全面准确率优化成功”。


## 本轮接续新增的真实人员与限定国家队负荷

同一批次十一新增三个独立候选，原候选数学定义不静默改名：

| 候选 | 特征与时距 | 增量父对照 |
|---|---|---|
| M_reported_absence_early | EARLY：赛程＋真实当前已报告缺阵者的过去分钟和上场XI份额 | M_schedule |
| M_national_load_early | EARLY：赛程＋历史阵容连续性＋限定国家队已观察分钟及XI覆盖 | M_lineup_early |
| M_national_load_lineup | LINEUP：赛程＋确认XI延续＋限定国家队已观察分钟及XI覆盖 | M_lineup_confirmed |

`advanced_personnel.py`只接受真实截点前取得并有冻结原件的伤停与覆盖上下文，伤停新鲜度沿用4小时规则。空响应不代表健康，疑似状态不当作确认缺阵，同刻冲突全部保留并拒绝；本场损坏原件也保留实际指纹并纳入失败重建闭包，不能默默选择旧版。历史缺阵快照不足时，这条候选不能拟合，不使用后来下载的伤停补训练。份额描述当前缺阵相对于过去实际用人的变化；长期已无近期出场者不再次获得人工扣减，LINEUP不叠加此组。程序仍没有可靠的本场预计首发概率，也没有完整因果球员能力模型。

`advanced_player_load.py`通过原获准接口采集欧国联完整赛程及逐场球员分钟；限定范围明确为`uefa_nations_league_only`，不包含其他国家队赛事。14天窗口由高级协议冻结，EARLY绑定上场真实XI，LINEUP绑定当前已确认XI。值为有明确出场记录者的平均分钟，并同时输入匹配人数／XI比例；未匹配不表示零负荷，空匹配、缺窗口详情、身份或分钟未知均缺失。体能输入可以含真实加时出场分钟，但预测目标始终仅90分钟加伤停补时。

真实采集入口：`python -m football_model.advanced_player_load --runtime runtime --config config.json --national-league unl --max-seconds 300 --reserve-requests 400`。完整研究任务在训练前执行，使用真实API日余额、限速、缓存、容量和软截止；每次原件及待续请求保存在`runtime/advanced_features/player_load/`。真实错误保持非零，预算／时间／容量延期完整记录。使用者必须查看具体报告，命令退出0不代表全部资料已齐。

可选负荷原件损坏仅阻断该组。拒绝原件路径与实际指纹单列诊断，不混入已验证数值来源；若诊断字节未随预测冻结，字段`optional_diagnostic_bytes_frozen=false`明确保留此保存缺口。合法基线和其他合法特征仍可运行。新增分支按同一真实训练、未来冻结和评分程序运行；没有足够合格数据时保留未拟合／未捕获，不人为给出系数或提高λ。

本轮同时补上采集相对路径、快照逐文件容量准入和完整实现依赖指纹。国家队资料采集与自动续采已实现不等于全球国家队负荷已覆盖；真实缺阵训练样本积累也不等于人员修正效果已成立。


### 正式发布验收中的排队修复

2026-10-13 正式合并后，多个工作流进入同一 `football-model-state` 互斥组，平台默认只保留一个待执行任务，导致高级研究尚未创建 job 就被后来的任务替换取消。`cancel-in-progress: false` 只保护正在运行的任务，不能单独保护待执行队列。

本次同一批次十一的部署补修将六个相关工作流统一为 `queue: max`、`cancel-in-progress: false`。主分支仍串行恢复、修改和保存同一状态；高级研究分支仍使用独立分支组。按 GitHub 官方规则每组最多100个等待任务，超额仍可能取消；已有取消记录保留，不能按成功计。队列按实际开始等待的次序处理，不保证触发时间顺序；真实抓取和保存时间继续决定赛前资格，排队延迟不允许补写为及时预测。日程、API额度和生产算法未改变。

依据：[GitHub 并发与排队说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)。本修复必须另核新任务真实排队、启动和状态保存，不能仅凭YAML存在声称已执行。
