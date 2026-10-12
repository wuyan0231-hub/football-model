# 特征修正与goalmodel独立对照：实施入口

当前框架：v1.7修订批次九。新工程由用户2026-10-12明确授权自主实施、测试和发布，无阶段审批要求。panna暂缓。本期数据、程序与影子资格按各自原件报告，不承诺准确率或盈利。

## 实际命令

```bash
python -m football_model.feature_schedule --runtime runtime --config config.json --league serie_a --source-commit ACTUAL_SHA
python -m football_model.feature_research study --runtime runtime --league serie_a --source-commit ACTUAL_SHA --state-commit ACTUAL_STATE_SHA --run-goalmodel
python -m football_model.feature_snapshots capture --runtime runtime --config config.json --league serie_a --source-commit ACTUAL_SHA --state-commit ACTUAL_STATE_SHA --protocol feature-models.json
python -m football_model.feature_scoring score --runtime runtime --config config.json --league serie_a --source-commit ACTUAL_SHA --protocol feature-models.json
```

SHA为真实完整提交，不是字面占位。研究可无R运行M1，但未运行R时不得称G0/G1对照已完成；正式云端研究启用R并先执行真实R测试。原生产180天与F5-C继续原身份，候选不默认串联、不自动晋级。

## 已实现的第一批

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

开发主指标为完整比分负对数概率，辅以1X2对数/Brier、平局、总球、净胜2+/3+校准。对照报共同成功样本及含回退的全队列；G1-G0直接比较识别协变量增量。失败/未知球队/暖期/资料缺失不删除。

五态计算继续由既有settlement执行；合法报价和费用不充分时EV未知/条件算术，参考不当开放或成交，不能以新点EV越过原诊断用途。真实收益暂无新证明。

未来评分读取首次真正候选捕获的完整冻结闭包，达到开球+4小时后独立查询API常规比分，主客/比赛/赛事/时间严格核验。两时距分开评分、独立比赛去重；首评分不可变，7日内每24小时有限复核，赛果修订追加。空队列/未成熟不初始化API；失败和未知不记0，不算无报价收益。

## 后续效果门

`feature-models.json`是开发及未来工程影子协议，confirmation_active=false、production_promotion=false。确认期需在首场前另定有意义δ、允许退化ε、成组样本量、主检验/多候选及停止规则，并不可变持久保存；不得套F5的40场或流程30请求门证明准确率。三场已见赛果继续开发案例。持续未来样本积累不等于效果已获确认，未满足效果门时保留原基线及候选研究身份。
