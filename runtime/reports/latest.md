# 足球模型运行报告

生成时间：2026-10-11T01:33:43.870500Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|
|---|---|---|---|---:|---|---|
|英超|探索性基线|available|9/0/9|816|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|英冠|探索性基线|available|25/0/25|1220|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西甲|探索性基线|available|10/0/10|833|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德甲|探索性基线|available|9/0/9|659|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|意甲|探索性基线|available|11/0/11|813|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法甲|探索性基线|available|7/0/7|666|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德乙|探索性基线|available|9/0/9|676|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法乙|探索性基线|available|9/0/9|690|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧冠|仅审计|audit_only|None/None/None|668|未训练|欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。|
|瑞典超|探索性基线|available|6/0/6|664|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|荷甲|探索性基线|available|8/0/8|698|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|葡超|探索性基线|available|4/0/4|683|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|巴西甲|探索性基线|available|13/0/13|1050|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|日职联J1|探索性基线|available|12/0/12|666|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|韩国K1|探索性基线|available|2/0/2|649|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|美职联MLS|探索性基线|available|27/0/27|1469|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧洲国家联赛|探索性基线|no_upcoming|0/0/0|434|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。 场地筛选：454场常规赛果中纳入434场、排除20场；未来7天可用预测0场，不支持0场。|
|墨西超Liga MX|探索性基线|available|11/0/11|767|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西乙|探索性基线|available|13/0/13|1027|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|阿根廷甲级联赛|探索性基线|available|8/0/8|1313|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|

预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。

本次接口网络尝试次数：25（含状态查询与重试，不等于当日计费已用量）。

## API额度与限速

|项目|本次记录|
|---|---|
|配置的每日上限|5000|
|供应商日上限／当日已用（最近观测）|7500／25|
|供应商最近回报余额|7475|
|当前有效日余额（保守估计）|4975|
|日额证据状态|known|
|每轮网络次数上限|不设额外次数上限|
|供应商分钟上限／最近剩余|300／275|
|当前请求最小间隔（秒）|0.21000000000000002|
|网络请求软截止／已运行（秒）|3600／37.43311106399999|
|额度最近观测时间／来源|2026-10-11T01:33:42.080266Z／response_headers|

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

## 英超

- 2026：返回380场，抓取时间2026-10-11T01:33:06.891833Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-07T01:32:57.800752Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-07T01:32:58.064015Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-02-28T00:00:00Z",
    "train_last_kickoff": "2026-02-27T20:00:00Z",
    "test_start": "2026-02-28T12:30:00Z",
    "test_end": "2026-10-10T16:30:00Z",
    "training_count": 652,
    "testing_count": 164,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 164,
    "scored": 155,
    "excluded": 9,
    "ratio": 0.9451219512195121,
    "exclusions": [
      {
        "match_id": 1557367,
        "reason": "未知球队 ID：[1346]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557368,
        "reason": "未知球队 ID：[64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557380,
        "reason": "未知球队 ID：[1346, 64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557394,
        "reason": "未知球队 ID：[1346]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557392,
        "reason": "未知球队 ID：[64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557399,
        "reason": "未知球队 ID：[64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557400,
        "reason": "未知球队 ID：[1346]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557414,
        "reason": "未知球队 ID：[64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557415,
        "reason": "未知球队 ID：[1346]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 155,
    "log_loss": 1.0391472592839335,
    "brier_score": 0.6217690054510547,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 155,
    "log_loss": 1.0946214885399466,
    "brier_score": 0.6628429739165164,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5134099616858236,
    "away_rate": 1.3448275862068966,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.055474229256013086,
    "model_minus_baseline_brier_score": -0.04107396846546163,
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

## 英冠

- 2026：返回552场，抓取时间2026-10-11T01:33:09.871947Z；接口返回成功；完整性尚需核验。
- 2025：返回558场，抓取时间2026-10-09T16:25:11.914590Z；接口返回成功；完整性尚需核验。
- 2024：返回557场，抓取时间2026-10-09T16:25:12.243082Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-06T00:00:00Z",
    "train_last_kickoff": "2026-03-03T19:45:00Z",
    "test_start": "2026-03-06T20:00:00Z",
    "test_end": "2026-10-10T14:00:00Z",
    "training_count": 976,
    "testing_count": 244,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 244,
    "scored": 212,
    "excluded": 32,
    "ratio": 0.8688524590163934,
    "exclusions": [
      {
        "match_id": 1563083,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563084,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563087,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563093,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563098,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563100,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563101,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563105,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563117,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563108,
        "reason": "未知球队 ID：[68, 1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563116,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563120,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563123,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563126,
        "reason": "未知球队 ID：[48, 39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563133,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563134,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563142,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563131,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563144,
        "reason": "未知球队 ID：[68, 48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563166,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563156,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563160,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563161,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563145,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563172,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563171,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563177,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563173,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563190,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563180,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563183,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563185,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 212,
    "log_loss": 1.0959832381821517,
    "brier_score": 0.6635508221831996,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 212,
    "log_loss": 1.0959006795038302,
    "brier_score": 0.6647650311904574,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4137224782386073,
    "away_rate": 1.0993343573988734,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 8.255867832152042e-05,
    "model_minus_baseline_brier_score": -0.0012142090072577716,
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

## 西甲

- 2026：返回380场，抓取时间2026-10-11T01:33:11.852897Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-07T01:32:59.103676Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-07T01:32:59.378713Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-22T00:00:00Z",
    "train_last_kickoff": "2026-03-21T20:00:00Z",
    "test_start": "2026-03-22T13:00:00Z",
    "test_end": "2026-10-10T19:00:00Z",
    "training_count": 666,
    "testing_count": 167,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 167,
    "scored": 146,
    "excluded": 21,
    "ratio": 0.874251497005988,
    "exclusions": [
      {
        "match_id": 1570339,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570337,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570334,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570348,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570349,
        "reason": "未知球队 ID：[535, 544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570359,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570360,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570356,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570370,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570372,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570369,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570378,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570374,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570375,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570387,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570385,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570390,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570395,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570398,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570396,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570408,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 146,
    "log_loss": 0.9772044741358286,
    "brier_score": 0.57864448462419,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 146,
    "log_loss": 1.0425424791260292,
    "brier_score": 0.6280128303407866,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5011252813203302,
    "away_rate": 1.145536384096024,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.0653380049902006,
    "model_minus_baseline_brier_score": -0.04936834571659654,
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

## 德甲

- 2026：返回306场，抓取时间2026-10-11T01:33:13.507914Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-07T01:33:00.352448Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-07T01:33:00.619388Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-07T00:00:00Z",
    "train_last_kickoff": "2026-03-06T19:30:00Z",
    "test_start": "2026-03-07T14:30:00Z",
    "test_end": "2026-10-10T16:30:00Z",
    "training_count": 525,
    "testing_count": 134,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 134,
    "scored": 123,
    "excluded": 11,
    "ratio": 0.917910447761194,
    "exclusions": [
      {
        "match_id": 1545417,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1545418,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575146,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575141,
        "reason": "未知球队 ID：[174]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575157,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575156,
        "reason": "未知球队 ID：[174]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575164,
        "reason": "未知球队 ID：[174]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575158,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575173,
        "reason": "未知球队 ID：[174]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575175,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575184,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 123,
    "log_loss": 0.9977785378079311,
    "brier_score": 0.5945541880056883,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 123,
    "log_loss": 1.076610704846054,
    "brier_score": 0.6523692144097479,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.686964795432921,
    "away_rate": 1.4643196955280684,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.0788321670381229,
    "model_minus_baseline_brier_score": -0.057815026404059666,
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

## 意甲

- 2026：返回380场，抓取时间2026-10-11T01:33:15.097176Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-07T01:33:01.582168Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-07T01:33:01.859769Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-06T00:00:00Z",
    "train_last_kickoff": "2026-03-02T19:45:00Z",
    "test_start": "2026-03-06T19:45:00Z",
    "test_end": "2026-10-10T18:45:00Z",
    "training_count": 650,
    "testing_count": 163,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 163,
    "scored": 157,
    "excluded": 6,
    "ratio": 0.9631901840490797,
    "exclusions": [
      {
        "match_id": 1550090,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550100,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550111,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550119,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550131,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550144,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 157,
    "log_loss": 0.9717073924340653,
    "brier_score": 0.5772900412461469,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 157,
    "log_loss": 1.0694008080536679,
    "brier_score": 0.6478521515145261,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.315142198308993,
    "away_rate": 1.1906225980015372,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.09769341561960254,
    "model_minus_baseline_brier_score": -0.07056211026837922,
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

## 法甲

- 2026：返回306场，抓取时间2026-10-11T01:33:16.738035Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-07T01:33:02.739357Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-08T01:33:48.663837Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-13T00:00:00Z",
    "train_last_kickoff": "2026-03-08T19:45:00Z",
    "test_start": "2026-03-13T19:45:00Z",
    "test_end": "2026-10-10T18:45:00Z",
    "training_count": 533,
    "testing_count": 133,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 133,
    "scored": 122,
    "excluded": 11,
    "ratio": 0.9172932330827067,
    "exclusions": [
      {
        "match_id": 1552731,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552737,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552740,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552744,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552752,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552755,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552760,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552759,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552765,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552767,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552780,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 1.0406772056878346,
    "brier_score": 0.6283681291644914,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0857347277668767,
    "brier_score": 0.6576070496474614,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5998125585754452,
    "away_rate": 1.2867853795688846,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04505752207904212,
    "model_minus_baseline_brier_score": -0.029238920482970054,
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

## 德乙

- 2026：返回306场，抓取时间2026-10-11T01:33:18.274415Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-08T01:33:49.804610Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-08T01:33:49.994472Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-20T00:00:00Z",
    "train_last_kickoff": "2026-03-15T12:30:00Z",
    "test_start": "2026-03-20T17:30:00Z",
    "test_end": "2026-10-10T18:30:00Z",
    "training_count": 542,
    "testing_count": 134,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 134,
    "scored": 102,
    "excluded": 32,
    "ratio": 0.7611940298507462,
    "exclusions": [
      {
        "match_id": 1545405,
        "reason": "未知球队 ID：[1621]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1545406,
        "reason": "未知球队 ID：[1621]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576137,
        "reason": "未知球队 ID：[180, 1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576142,
        "reason": "未知球队 ID：[161]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576135,
        "reason": "未知球队 ID：[1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576141,
        "reason": "未知球队 ID：[186]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576148,
        "reason": "未知球队 ID：[186]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576147,
        "reason": "未知球队 ID：[180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576151,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576143,
        "reason": "未知球队 ID：[1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576146,
        "reason": "未知球队 ID：[161]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576152,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576154,
        "reason": "未知球队 ID：[1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576156,
        "reason": "未知球队 ID：[180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576157,
        "reason": "未知球队 ID：[161]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576160,
        "reason": "未知球队 ID：[186]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576161,
        "reason": "未知球队 ID：[186]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576169,
        "reason": "未知球队 ID：[161, 1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576162,
        "reason": "未知球队 ID：[180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576168,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576177,
        "reason": "未知球队 ID：[186, 161]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576173,
        "reason": "未知球队 ID：[180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576174,
        "reason": "未知球队 ID：[1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576178,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576187,
        "reason": "未知球队 ID：[161]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576183,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576179,
        "reason": "未知球队 ID：[180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576180,
        "reason": "未知球队 ID：[1320, 186]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576191,
        "reason": "未知球队 ID：[180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576190,
        "reason": "未知球队 ID：[1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576196,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576194,
        "reason": "未知球队 ID：[161]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 102,
    "log_loss": 1.0349292031836574,
    "brier_score": 0.6234522723669289,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 102,
    "log_loss": 1.0475051655753416,
    "brier_score": 0.6315149736822407,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6027649769585253,
    "away_rate": 1.3649769585253457,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.012575962391684126,
    "model_minus_baseline_brier_score": -0.008062701315311838,
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

## 法乙

- 2026：返回306场，抓取时间2026-10-11T01:33:19.875660Z；接口返回成功；完整性尚需核验。
- 2025：返回310场，抓取时间2026-10-08T01:33:51.120521Z；接口返回成功；完整性尚需核验。
- 2024：返回310场，抓取时间2026-10-08T01:33:51.369588Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-20T00:00:00Z",
    "train_last_kickoff": "2026-03-16T19:45:00Z",
    "test_start": "2026-03-20T19:00:00Z",
    "test_end": "2026-10-10T18:00:00Z",
    "training_count": 552,
    "testing_count": 138,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 138,
    "scored": 114,
    "excluded": 24,
    "ratio": 0.8260869565217391,
    "exclusions": [
      {
        "match_id": 1545224,
        "reason": "未知球队 ID：[3221]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1545225,
        "reason": "未知球队 ID：[3221]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552427,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552428,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552431,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552433,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552436,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552438,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552442,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552448,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552445,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552451,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552453,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552452,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552459,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552465,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552464,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552469,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552476,
        "reason": "未知球队 ID：[115, 83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552477,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552481,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552478,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552493,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552491,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 114,
    "log_loss": 1.0679606904533812,
    "brier_score": 0.6466854408657767,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 114,
    "log_loss": 1.0971620748852677,
    "brier_score": 0.6652409850213293,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3909502262443438,
    "away_rate": 1.1266968325791855,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.02920138443188658,
    "model_minus_baseline_brier_score": -0.018555544155552606,
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

## 欧冠

- 2026：返回234场，抓取时间2026-10-11T01:33:21.681129Z；接口返回成功；完整性尚需核验。
- 2025：返回281场，抓取时间2026-10-08T01:33:52.723625Z；接口返回成功；完整性尚需核验。
- 2024：返回279场，抓取时间2026-10-08T01:33:52.944926Z；接口返回成功；完整性尚需核验。
## 瑞典超

- 2026：返回240场，抓取时间2026-10-11T01:33:23.042200Z；接口返回成功；完整性尚需核验。
- 2025：返回242场，抓取时间2026-10-08T01:33:53.985628Z；接口返回成功；完整性尚需核验。
- 2024：返回242场，抓取时间2026-10-08T01:33:54.193832Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-08T00:00:00Z",
    "train_last_kickoff": "2026-05-04T17:00:00Z",
    "test_start": "2026-05-08T17:00:00Z",
    "test_end": "2026-10-10T15:30:00Z",
    "training_count": 532,
    "testing_count": 132,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 132,
    "scored": 132,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 132,
    "log_loss": 1.028998444963802,
    "brier_score": 0.6166418989415495,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 132,
    "log_loss": 1.0659050163411083,
    "brier_score": 0.6462177430423486,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5427230046948357,
    "away_rate": 1.307981220657277,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.03690657137730624,
    "model_minus_baseline_brier_score": -0.029575844100799054,
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

## 荷甲

- 2026：返回306场，抓取时间2026-10-11T01:33:24.638564Z；接口返回成功；完整性尚需核验。
- 2025：返回309场，抓取时间2026-10-08T01:33:55.316254Z；接口返回成功；完整性尚需核验。
- 2024：返回321场，抓取时间2026-10-08T01:33:55.521337Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-15T00:00:00Z",
    "train_last_kickoff": "2026-03-14T20:00:00Z",
    "test_start": "2026-03-15T11:15:00Z",
    "test_end": "2026-10-10T19:00:00Z",
    "training_count": 560,
    "testing_count": 138,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 138,
    "scored": 138,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 138,
    "log_loss": 0.9909217573117495,
    "brier_score": 0.5892722975521671,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 138,
    "log_loss": 1.1067412855632923,
    "brier_score": 0.6726485458624758,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.727921498661909,
    "away_rate": 1.3407671721677075,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.11581952825154285,
    "model_minus_baseline_brier_score": -0.08337624831030865,
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

## 葡超

- 2026：返回306场，抓取时间2026-10-11T01:33:26.302279Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-08T01:33:56.741597Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-08T01:33:56.939164Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-22T00:00:00Z",
    "train_last_kickoff": "2026-03-21T18:00:00Z",
    "test_start": "2026-03-22T15:30:00Z",
    "test_end": "2026-10-10T19:30:00Z",
    "training_count": 545,
    "testing_count": 137,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1396479
    ]
  },
  "coverage": {
    "total": 137,
    "scored": 120,
    "excluded": 17,
    "ratio": 0.8759124087591241,
    "exclusions": [
      {
        "match_id": 1545403,
        "reason": "未知球队 ID：[4799]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1545404,
        "reason": "未知球队 ID：[4799]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575451,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575446,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575455,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575460,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575468,
        "reason": "未知球队 ID：[214, 238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575475,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575473,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575487,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575486,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575491,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575497,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575504,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575502,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575515,
        "reason": "未知球队 ID：[214]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575509,
        "reason": "未知球队 ID：[238]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 120,
    "log_loss": 0.9896811440298385,
    "brier_score": 0.5926576325827259,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 120,
    "log_loss": 1.0918810828163124,
    "brier_score": 0.6619860890714853,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.449129239230064,
    "away_rate": 1.1796516956920258,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.10219993878647382,
    "model_minus_baseline_brier_score": -0.0693284564887594,
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

## 巴西甲

- 2026：返回380场，抓取时间2026-10-11T01:33:27.952862Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-08T01:33:58.310978Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-08T01:33:58.519369Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-02T00:00:00Z",
    "train_last_kickoff": "2026-03-29T22:30:00Z",
    "test_start": "2026-04-02T00:30:00Z",
    "test_end": "2026-10-10T21:00:00Z",
    "training_count": 837,
    "testing_count": 208,
    "result_availability_delay_hours": 4,
    "embargoed_count": 5,
    "embargoed_match_ids": [
      1492191,
      1492197,
      1492190,
      1492195,
      1492194
    ]
  },
  "coverage": {
    "total": 208,
    "scored": 208,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 208,
    "log_loss": 1.029198428229398,
    "brier_score": 0.6201665002791826,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 208,
    "log_loss": 1.0667854571832105,
    "brier_score": 0.6444452431755148,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4776119402985075,
    "away_rate": 1.0202985074626865,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.037587028953812496,
    "model_minus_baseline_brier_score": -0.02427874289633214,
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

## 日职联J1

- 2027：返回380场，抓取时间2026-10-11T01:33:29.833413Z；接口返回成功；完整性尚需核验。
- 2026：返回200场，抓取时间2026-10-08T01:33:59.831403Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-08T01:34:00.054450Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-10T00:00:00Z",
    "train_last_kickoff": "2026-05-09T07:00:00Z",
    "test_start": "2026-05-10T05:00:00Z",
    "test_end": "2026-10-10T10:00:00Z",
    "training_count": 531,
    "testing_count": 135,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 135,
    "scored": 135,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 135,
    "log_loss": 1.0225257846082865,
    "brier_score": 0.6109307574126616,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 135,
    "log_loss": 1.074393848421358,
    "brier_score": 0.65070160609274,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3367826904985889,
    "away_rate": 1.1241768579492004,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.05186806381307152,
    "model_minus_baseline_brier_score": -0.03977084868007841,
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

## 韩国K1

- 2026：返回198场，抓取时间2026-10-11T01:33:31.523204Z；接口返回成功；完整性尚需核验。
- 2025：返回232场，抓取时间2026-10-07T07:08:08.172378Z；接口返回成功；完整性尚需核验。
- 2024：返回232场，抓取时间2026-10-07T07:08:08.400145Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-25T00:00:00Z",
    "train_last_kickoff": "2026-04-22T10:30:00Z",
    "test_start": "2026-04-25T05:00:00Z",
    "test_end": "2026-10-10T07:30:00Z",
    "training_count": 518,
    "testing_count": 131,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 131,
    "scored": 131,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 131,
    "log_loss": 1.0702629261255703,
    "brier_score": 0.647401885431299,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 131,
    "log_loss": 1.1142211609079673,
    "brier_score": 0.6777507099866455,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3799421407907426,
    "away_rate": 1.1639344262295082,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04395823478239702,
    "model_minus_baseline_brier_score": -0.0303488245553466,
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

## 美职联MLS

- 2026：返回510场，抓取时间2026-10-11T01:33:33.178964Z；接口返回成功；完整性尚需核验。
- 2025：返回540场，抓取时间2026-10-07T07:09:05.938559Z；接口返回成功；完整性尚需核验。
- 2024：返回526场，抓取时间2026-10-07T07:09:06.145918Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-19T00:00:00Z",
    "train_last_kickoff": "2026-04-18T18:30:00Z",
    "test_start": "2026-04-19T00:30:00Z",
    "test_end": "2026-10-10T18:30:00Z",
    "training_count": 1168,
    "testing_count": 294,
    "result_availability_delay_hours": 4,
    "embargoed_count": 7,
    "embargoed_match_ids": [
      1490213,
      1490214,
      1490215,
      1490216,
      1490217,
      1490218,
      1490219
    ]
  },
  "coverage": {
    "total": 294,
    "scored": 294,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 294,
    "log_loss": 1.1031375183714192,
    "brier_score": 0.6678831735610558,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 294,
    "log_loss": 1.0736932994303794,
    "brier_score": 0.6486583159620838,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.7171587505348738,
    "away_rate": 1.3534445870774496,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.029444218941039768,
    "model_minus_baseline_brier_score": 0.019224857598971967,
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

## 欧洲国家联赛

- 2026：返回156场，抓取时间2026-10-11T01:33:35.710744Z；接口返回成功；完整性尚需核验。
- 2024：返回188场，抓取时间2026-10-09T01:35:59.165015Z；接口返回成功；完整性尚需核验。
- 2022：返回168场，抓取时间2026-10-09T01:35:59.377946Z；接口返回成功；完整性尚需核验。

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

## 墨西超Liga MX

- 2026：返回153场，抓取时间2026-10-11T01:33:38.388299Z；接口返回成功；完整性尚需核验。
- 2025：返回337场，抓取时间2026-10-04T04:05:31.314349Z；接口返回成功；完整性尚需核验。
- 2024：返回340场，抓取时间2026-10-04T04:05:37.812442Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-23T00:00:00Z",
    "train_last_kickoff": "2026-03-22T03:10:00Z",
    "test_start": "2026-03-23T01:06:00Z",
    "test_end": "2026-10-10T03:00:00Z",
    "training_count": 614,
    "testing_count": 151,
    "result_availability_delay_hours": 4,
    "embargoed_count": 2,
    "embargoed_match_ids": [
      1492565,
      1492569
    ]
  },
  "coverage": {
    "total": 151,
    "scored": 141,
    "excluded": 10,
    "ratio": 0.9337748344370861,
    "exclusions": [
      {
        "match_id": 1550894,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550907,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550918,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550921,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550936,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550939,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550954,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550959,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550966,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1550975,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 141,
    "log_loss": 1.1004649009530123,
    "brier_score": 0.663149395003006,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 141,
    "log_loss": 1.0733730671028447,
    "brier_score": 0.6498717907641299,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6346623270951994,
    "away_rate": 1.1871440195280716,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.027091833850167557,
    "model_minus_baseline_brier_score": 0.013277604238876028,
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

## 西乙

- 2026：返回462场，抓取时间2026-10-11T01:33:40.161266Z；接口返回成功；完整性尚需核验。
- 2025：返回468场，抓取时间2026-10-04T15:32:43.349074Z；接口返回成功；完整性尚需核验。
- 2024：返回468场，抓取时间2026-10-04T15:32:43.588357Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-01T00:00:00Z",
    "train_last_kickoff": "2026-03-31T19:30:00Z",
    "test_start": "2026-04-01T17:00:00Z",
    "test_end": "2026-10-10T19:00:00Z",
    "training_count": 823,
    "testing_count": 204,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 204,
    "scored": 173,
    "excluded": 31,
    "ratio": 0.8480392156862745,
    "exclusions": [
      {
        "match_id": 1569874,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569879,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569877,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569876,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569886,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569883,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569884,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569890,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569902,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569899,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569901,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569897,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569909,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569906,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569908,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569913,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569921,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569923,
        "reason": "未知球队 ID：[798, 9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569918,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569935,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569929,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569936,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569927,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569941,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569938,
        "reason": "未知球队 ID：[9571, 9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569944,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569954,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569955,
        "reason": "未知球队 ID：[547, 798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569958,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569964,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569963,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 173,
    "log_loss": 1.0156363360952032,
    "brier_score": 0.6084584606022466,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 173,
    "log_loss": 1.0636839892636687,
    "brier_score": 0.6430489882386493,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4408014571948997,
    "away_rate": 1.1202185792349726,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04804765316846549,
    "model_minus_baseline_brier_score": -0.03459052763640269,
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

## 阿根廷甲级联赛

- 2026：返回495场，抓取时间2026-10-11T01:33:42.146983Z；接口返回成功；完整性尚需核验。
- 2025：返回510场，抓取时间2026-10-04T17:46:21.210260Z；接口返回成功；完整性尚需核验。
- 2024：返回378场，抓取时间2026-10-04T17:46:21.498376Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-11T01:33:06.415389Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-25T00:00:00Z",
    "train_last_kickoff": "2026-03-24T00:15:00Z",
    "test_start": "2026-03-25T22:00:00Z",
    "test_end": "2026-10-10T20:00:00Z",
    "training_count": 1050,
    "testing_count": 263,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 263,
    "scored": 263,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 263,
    "log_loss": 1.0748716753959038,
    "brier_score": 0.6506101599992661,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 263,
    "log_loss": 1.0848024032359609,
    "brier_score": 0.6569282581194985,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.1504045692527367,
    "away_rate": 0.8372203712517848,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.009930727840057108,
    "model_minus_baseline_brier_score": -0.006318098120232429,
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
