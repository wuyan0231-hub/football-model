# 足球模型运行报告

生成时间：2026-10-08T03:05:15.176854Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|
|---|---|---|---|---:|---|---|
|欧洲国家联赛|探索性基线|no_upcoming|0/0/0|434|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。 场地筛选：454场常规赛果中纳入434场、排除20场；未来7天可用预测0场，不支持0场。|

预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。

本次接口网络尝试次数：2（含状态查询与重试，不等于当日计费已用量）。

## API额度与限速

|项目|本次记录|
|---|---|
|配置的每日上限|5000|
|供应商日上限／当日已用（最近观测）|7500／589|
|供应商最近回报余额|6911|
|当前有效日余额（保守估计）|4411|
|日额证据状态|known|
|每轮网络次数上限|不设额外次数上限|
|供应商分钟上限／最近剩余|300／298|
|当前请求最小间隔（秒）|0.21000000000000002|
|网络请求软截止／已运行（秒）|3600／2.6061655729999984|
|额度最近观测时间／来源|2026-10-08T03:05:13.407530Z／response_headers|

每轮不设额外次数上限时，仍遵守已核实的当日额度、后续下载预留、分钟限速及软截止；不把每日上限当作每轮可重新使用的余额。未知额度不填零，也不以配置值冒充已核实余额。


## 怎样阅读

- 已取得数据，不代表该赛事全部历史数据完整；请查看各赛季统计和请求失败项。
- 已拟合／沿用参数，不代表通过准确性或收益验收。参数和所有概率都是探索性估计。
- 验证采用按日期划分的时间留出集，未来时段不参与那次拟合；不是完整滚动回测。
- 训练窗口、半衰期、正则强度采用预先固定默认值，尚未调优；不能据反复查看同一留出集声称独立验收。
- 历史数据可能包含事后修订；无原始发布时间快照，历史检查不是严格还原当时供应商资料。
- 正式预测文件保存数据截点、模型版本和未覆盖因素；阵容影响未量化，EV未量化。
- 国内联赛仍按名义主客生成条件基线；欧国联按精确场地登记表分类，未知场地不出数值。实际场地和特殊赛制仍须核实。
- 新队或与训练队伍缺少连接的队伍不出数值；陈旧模型不自动发布新预测。
- 单项失败会保留旧模型文件，但本次不会把旧文件伪装为新训练，也不会用失败的数据生成预测。

## 欧洲国家联赛

- 2026：返回156场，抓取时间2026-10-08T03:05:13.420388Z；接口返回成功；完整性尚需核验。
- 2024：返回188场，抓取时间2026-10-02T01:35:04.381135Z；接口返回成功；完整性尚需核验。
- 2022：返回168场，抓取时间2026-10-02T01:35:10.879030Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-31T00:00:00Z",
    "train_last_kickoff": "2026-03-26T17:00:00Z",
    "test_start": "2026-03-31T16:00:00Z",
    "test_end": "2026-10-06T18:45:00Z",
    "training_count": 347,
    "testing_count": 87,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 87,
    "scored": 87,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 87,
    "log_loss": 1.100940367721053,
    "brier_score": 0.6680900174095673,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 87,
    "log_loss": 1.0784304880248146,
    "brier_score": 0.6538787081391572,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4719424460431654,
    "away_rate": 1.1812949640287769,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.02250987969623841,
    "model_minus_baseline_brier_score": 0.014211309270410144,
    "lower_is_better": true
  },
  "warnings": [
    "单次固定模型留出验证仅为探索性检查，不是滚动回测，也不构成上线或盈利证明。",
    "不含赔率、投注收益、阵容修正或提前十小时伤停历史快照验证。",
    "超参数为显式默认值，不能根据本留出期反复挑选参数后再声称独立验证。",
    "未知球队比赛单独列为未覆盖，指标只针对 model_metrics.n 场已覆盖比赛。"
  ]
}
```
