# 足球模型运行报告

生成时间：2026-09-24T16:07:24.628668Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|状态|可用90分钟赛果|训练参数变化|说明|
|---|---|---:|---|---|
|欧洲国家联赛|探索性基线|349|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。 场地筛选：350场常规赛果中纳入349场、排除1场；未来7天可用预测0场，不支持52场。|

本次接口请求次数：2。

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

- 2026：返回156场，抓取时间2026-09-24T16:07:31.474820Z；接口返回成功；完整性尚需核验。
- 2024：返回188场，抓取时间2026-09-24T15:54:24.558896Z；接口返回成功；完整性尚需核验。
- 2022：返回168场，抓取时间2026-09-24T15:54:31.022018Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-24T16:07:24.628668Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2024-11-16T00:00:00Z",
    "train_last_kickoff": "2024-11-15T19:45:00Z",
    "test_start": "2024-11-16T14:00:00Z",
    "test_end": "2026-03-31T16:00:00Z",
    "training_count": 283,
    "testing_count": 66,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 66,
    "scored": 66,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 66,
    "log_loss": 1.0193969221551993,
    "brier_score": 0.6157497191735957,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 66,
    "log_loss": 1.0824162804716364,
    "brier_score": 0.6551804187292826,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4162257495590829,
    "away_rate": 1.17989417989418,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.06301935831643712,
    "model_minus_baseline_brier_score": -0.0394306995556869,
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
