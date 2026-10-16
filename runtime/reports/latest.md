# 足球模型运行报告

生成时间：2026-10-16T01:51:01.306953Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|
|---|---|---|---|---:|---|---|
|英超|探索性基线|available|10/0/10|820|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|英冠|探索性基线|available|13/0/13|1233|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西甲|探索性基线|available|11/0/11|838|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德甲|探索性基线|available|9/0/9|661|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|意甲|探索性基线|available|10/0/10|820|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法甲|探索性基线|available|9/0/9|669|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德乙|探索性基线|available|9/0/9|679|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法乙|探索性基线|available|9/0/9|691|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧冠|仅审计|audit_only|None/None/None|686|未训练|欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。|
|瑞典超|探索性基线|available|8/0/8|668|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|荷甲|探索性基线|available|9/0/9|702|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|葡超|探索性基线|no_upcoming|0/0/0|687|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|巴西甲|探索性基线|available|10/0/10|1059|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|日职联J1|探索性基线|available|20/0/20|670|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|韩国K1|探索性基线|available|6/0/6|650|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|美职联MLS|探索性基线|available|16/0/16|1497|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧洲国家联赛|探索性基线|no_upcoming|0/0/0|434|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。 场地筛选：454场常规赛果中纳入434场、排除20场；未来7天可用预测0场，不支持0场。|
|墨西超Liga MX|探索性基线|available|18/0/18|774|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西乙|探索性基线|available|11/0/11|1035|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|阿根廷甲级联赛|探索性基线|available|14/0/14|1323|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|

预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。

本次接口网络尝试次数：67（含状态查询与重试，不等于当日计费已用量）。

## API额度与限速

|项目|本次记录|
|---|---|
|配置的每日上限|7460|
|供应商日上限／当日已用（最近观测）|7500／67|
|供应商最近回报余额|7433|
|当前有效日余额（保守估计）|7393|
|日额证据状态|known|
|每轮网络次数上限|不设额外次数上限|
|供应商分钟上限／最近剩余|300／233|
|当前请求最小间隔（秒）|0.21000000000000002|
|网络请求软截止／已运行（秒）|3600／48.47621661599999|
|额度最近观测时间／来源|2026-10-16T01:50:59.581528Z／response_headers|

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

- 2026：返回380场，抓取时间2026-10-16T01:50:13.645549Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-16T01:50:13.889809Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-16T01:50:14.124025Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-01T00:00:00Z",
    "train_last_kickoff": "2026-02-28T17:30:00Z",
    "test_start": "2026-03-01T14:00:00Z",
    "test_end": "2026-10-12T19:00:00Z",
    "training_count": 657,
    "testing_count": 163,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 163,
    "scored": 152,
    "excluded": 11,
    "ratio": 0.9325153374233128,
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
      },
      {
        "match_id": 1557422,
        "reason": "未知球队 ID：[64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557420,
        "reason": "未知球队 ID：[1346]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 152,
    "log_loss": 1.0454239940173675,
    "brier_score": 0.6261614535548649,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 152,
    "log_loss": 1.093033577351189,
    "brier_score": 0.661645109394599,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5186311787072244,
    "away_rate": 1.3513307984790874,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04760958333382148,
    "model_minus_baseline_brier_score": -0.03548365583973412,
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

- 2026：返回552场，抓取时间2026-10-16T01:50:16.980748Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-16T01:50:12.817334Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-10T00:00:00Z",
    "train_last_kickoff": "2026-03-08T16:30:00Z",
    "test_start": "2026-03-10T19:45:00Z",
    "test_end": "2026-10-14T19:00:00Z",
    "training_count": 986,
    "testing_count": 247,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 247,
    "scored": 213,
    "excluded": 34,
    "ratio": 0.8623481781376519,
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
      },
      {
        "match_id": 1563197,
        "reason": "未知球队 ID：[39, 68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563199,
        "reason": "未知球队 ID：[1379, 48]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 213,
    "log_loss": 1.0967775464557838,
    "brier_score": 0.6643563993442472,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 213,
    "log_loss": 1.091498362937937,
    "brier_score": 0.6617318461411261,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4095286365940192,
    "away_rate": 1.104409528636594,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.005279183517846953,
    "model_minus_baseline_brier_score": 0.0026245532031210494,
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

- 2026：返回380场，抓取时间2026-10-16T01:50:19.125687Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-16T01:50:19.377081Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-16T01:50:19.638688Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-03T00:00:00Z",
    "train_last_kickoff": "2026-03-22T20:00:00Z",
    "test_start": "2026-04-03T19:00:00Z",
    "test_end": "2026-10-12T19:00:00Z",
    "training_count": 670,
    "testing_count": 168,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 168,
    "scored": 145,
    "excluded": 23,
    "ratio": 0.8630952380952381,
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
      },
      {
        "match_id": 1570412,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570409,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 145,
    "log_loss": 0.98252436654872,
    "brier_score": 0.5826853268744684,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 145,
    "log_loss": 1.0489821067893073,
    "brier_score": 0.6323885251629371,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.505592841163311,
    "away_rate": 1.1491424310216256,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.06645774024058726,
    "model_minus_baseline_brier_score": -0.0497031982884687,
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

- 2026：返回306场，抓取时间2026-10-16T01:50:21.520526Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-16T01:50:21.984599Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-16T01:50:22.242190Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-12T01:37:32.019444Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-08T00:00:00Z",
    "train_last_kickoff": "2026-03-07T17:30:00Z",
    "test_start": "2026-03-08T14:30:00Z",
    "test_end": "2026-10-11T15:30:00Z",
    "training_count": 531,
    "testing_count": 130,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 130,
    "scored": 118,
    "excluded": 12,
    "ratio": 0.9076923076923077,
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
      },
      {
        "match_id": 1575179,
        "reason": "未知球队 ID：[174]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 118,
    "log_loss": 1.002412468795541,
    "brier_score": 0.5981990202594684,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 118,
    "log_loss": 1.0698959788794602,
    "brier_score": 0.6477184172390176,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6886171213546566,
    "away_rate": 1.4741298212605833,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.06748351008391928,
    "model_minus_baseline_brier_score": -0.04951939697954921,
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

- 2026：返回380场，抓取时间2026-10-16T01:50:23.824487Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-15T15:41:17.174097Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-15T15:41:17.404874Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-08T00:00:00Z",
    "train_last_kickoff": "2026-03-07T19:45:00Z",
    "test_start": "2026-03-08T11:30:00Z",
    "test_end": "2026-10-12T18:45:00Z",
    "training_count": 654,
    "testing_count": 166,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 166,
    "scored": 160,
    "excluded": 6,
    "ratio": 0.963855421686747,
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
    "n": 160,
    "log_loss": 0.9755451658226495,
    "brier_score": 0.5799094367517833,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 160,
    "log_loss": 1.0714811907315398,
    "brier_score": 0.6491340396986387,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.320855614973262,
    "away_rate": 1.190985485103132,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.0959360249088903,
    "model_minus_baseline_brier_score": -0.0692246029468554,
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

- 2026：返回306场，抓取时间2026-10-16T01:50:25.726788Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-16T01:50:25.965138Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-16T01:50:26.219838Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-12T01:37:32.019444Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-14T00:00:00Z",
    "train_last_kickoff": "2026-03-13T19:45:00Z",
    "test_start": "2026-03-14T16:00:00Z",
    "test_end": "2026-10-11T18:45:00Z",
    "training_count": 534,
    "testing_count": 135,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 135,
    "scored": 123,
    "excluded": 12,
    "ratio": 0.9111111111111111,
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
      },
      {
        "match_id": 1552782,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 123,
    "log_loss": 1.0432488072754311,
    "brier_score": 0.6302282506904309,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 123,
    "log_loss": 1.0834368597243766,
    "brier_score": 0.6560010749059086,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.598690364826941,
    "away_rate": 1.284377923292797,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04018805244894552,
    "model_minus_baseline_brier_score": -0.025772824215477774,
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

- 2026：返回306场，抓取时间2026-10-16T01:50:28.354664Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-16T01:50:28.588802Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-16T01:50:28.835461Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-12T01:37:32.019444Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-21T00:00:00Z",
    "train_last_kickoff": "2026-03-20T17:30:00Z",
    "test_start": "2026-03-21T12:00:00Z",
    "test_end": "2026-10-11T11:30:00Z",
    "training_count": 544,
    "testing_count": 135,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 135,
    "scored": 102,
    "excluded": 33,
    "ratio": 0.7555555555555555,
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
      },
      {
        "match_id": 1576195,
        "reason": "未知球队 ID：[186]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 102,
    "log_loss": 1.0400668724152213,
    "brier_score": 0.627102820913206,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 102,
    "log_loss": 1.0499667846449177,
    "brier_score": 0.633365084979369,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6042240587695134,
    "away_rate": 1.361799816345271,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.009899912229696328,
    "model_minus_baseline_brier_score": -0.006262264066163037,
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

- 2026：返回306场，抓取时间2026-10-16T01:50:30.706355Z；接口返回成功；完整性尚需核验。
- 2025：返回310场，抓取时间2026-10-16T01:50:30.940974Z；接口返回成功；完整性尚需核验。
- 2024：返回310场，抓取时间2026-10-16T01:50:31.165472Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-20T00:00:00Z",
    "train_last_kickoff": "2026-03-16T19:45:00Z",
    "test_start": "2026-03-20T19:00:00Z",
    "test_end": "2026-10-12T18:45:00Z",
    "training_count": 552,
    "testing_count": 139,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 139,
    "scored": 114,
    "excluded": 25,
    "ratio": 0.8201438848920863,
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
      },
      {
        "match_id": 1552487,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
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

- 2026：返回234场，抓取时间2026-10-16T01:50:33.037972Z；接口返回成功；完整性尚需核验。
- 2025：返回281场，抓取时间2026-10-16T01:50:33.285212Z；接口返回成功；完整性尚需核验。
- 2024：返回279场，抓取时间2026-10-16T01:50:33.521192Z；接口返回成功；完整性尚需核验。
## 瑞典超

- 2026：返回240场，抓取时间2026-10-16T01:50:35.297155Z；接口返回成功；完整性尚需核验。
- 2025：返回242场，抓取时间2026-10-16T01:50:35.511915Z；接口返回成功；完整性尚需核验。
- 2024：返回242场，抓取时间2026-10-16T01:50:35.739417Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-09T00:00:00Z",
    "train_last_kickoff": "2026-05-08T17:00:00Z",
    "test_start": "2026-05-09T13:00:00Z",
    "test_end": "2026-10-12T17:00:00Z",
    "training_count": 533,
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
    "log_loss": 1.0211613167198021,
    "brier_score": 0.6112270136945824,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 135,
    "log_loss": 1.0632854315011127,
    "brier_score": 0.6444624275382926,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5435801312089972,
    "away_rate": 1.3055295220243675,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04212411478131051,
    "model_minus_baseline_brier_score": -0.033235413843710204,
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

- 2026：返回306场，抓取时间2026-10-16T01:50:37.670825Z；接口返回成功；完整性尚需核验。
- 2025：返回309场，抓取时间2026-10-16T01:50:37.903393Z；接口返回成功；完整性尚需核验。
- 2024：返回321场，抓取时间2026-10-16T01:50:38.157159Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-12T01:37:32.019444Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-15T00:00:00Z",
    "train_last_kickoff": "2026-03-14T20:00:00Z",
    "test_start": "2026-03-15T11:15:00Z",
    "test_end": "2026-10-11T14:45:00Z",
    "training_count": 560,
    "testing_count": 142,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 142,
    "scored": 142,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 142,
    "log_loss": 0.9903171648001583,
    "brier_score": 0.5890003978020887,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 142,
    "log_loss": 1.1081551926920081,
    "brier_score": 0.673771784098632,
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
    "model_minus_baseline_log_loss": -0.11783802789184983,
    "model_minus_baseline_brier_score": -0.08477138629654335,
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

- 2026：返回306场，抓取时间2026-10-16T01:50:40.095057Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-10-16T01:50:40.341642Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-10-16T01:50:40.594536Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-03T00:00:00Z",
    "train_last_kickoff": "2026-03-22T20:30:00Z",
    "test_start": "2026-04-03T14:30:00Z",
    "test_end": "2026-10-12T19:15:00Z",
    "training_count": 549,
    "testing_count": 138,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 138,
    "scored": 121,
    "excluded": 17,
    "ratio": 0.8768115942028986,
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
    "n": 121,
    "log_loss": 0.9852425638413531,
    "brier_score": 0.5890256681429151,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 121,
    "log_loss": 1.0842050568779305,
    "brier_score": 0.6564029737069903,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4440400363967243,
    "away_rate": 1.1856232939035487,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.09896249303657734,
    "model_minus_baseline_brier_score": -0.06737730556407517,
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

- 2026：返回380场，抓取时间2026-10-16T01:50:42.437562Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-16T01:50:42.702117Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-10-16T01:50:42.958803Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-16T01:50:12.817334Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-04T00:00:00Z",
    "train_last_kickoff": "2026-04-03T00:30:00Z",
    "test_start": "2026-04-04T21:30:00Z",
    "test_end": "2026-10-13T00:00:00Z",
    "training_count": 847,
    "testing_count": 212,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 212,
    "scored": 212,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 212,
    "log_loss": 1.0146749479189976,
    "brier_score": 0.6104476614346355,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 212,
    "log_loss": 1.0664141889976677,
    "brier_score": 0.6441927224045101,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4849557522123893,
    "away_rate": 1.0200589970501475,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.05173924107867012,
    "model_minus_baseline_brier_score": -0.03374506096987462,
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

- 2027：返回380场，抓取时间2026-10-16T01:50:45.152573Z；接口返回成功；完整性尚需核验。
- 2026：返回200场，抓取时间2026-10-16T01:50:45.369630Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-10-16T01:50:45.594975Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-12T01:37:32.019444Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-13T00:00:00Z",
    "train_last_kickoff": "2026-05-10T08:00:00Z",
    "test_start": "2026-05-13T10:00:00Z",
    "test_end": "2026-10-11T07:00:00Z",
    "training_count": 539,
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
    "log_loss": 1.0156216632182888,
    "brier_score": 0.6061896387896664,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 131,
    "log_loss": 1.077035225848287,
    "brier_score": 0.6526288730685437,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3317886932344765,
    "away_rate": 1.1241890639481,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.061413562629998086,
    "model_minus_baseline_brier_score": -0.046439234278877306,
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

- 2026：返回198场，抓取时间2026-10-16T01:50:47.590101Z；接口返回成功；完整性尚需核验。
- 2025：返回232场，抓取时间2026-10-16T01:50:47.808853Z；接口返回成功；完整性尚需核验。
- 2024：返回232场，抓取时间2026-10-16T01:50:48.045991Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-12T01:37:32.019444Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-26T00:00:00Z",
    "train_last_kickoff": "2026-04-25T07:30:00Z",
    "test_start": "2026-04-26T05:00:00Z",
    "test_end": "2026-10-11T05:00:00Z",
    "training_count": 521,
    "testing_count": 129,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 129,
    "scored": 129,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 129,
    "log_loss": 1.073921230681693,
    "brier_score": 0.6499054289830654,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 129,
    "log_loss": 1.113227856764724,
    "brier_score": 0.6768915187076419,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3758389261744965,
    "away_rate": 1.164908916586769,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.03930662608303104,
    "model_minus_baseline_brier_score": -0.0269860897245765,
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

- 2026：返回510场，抓取时间2026-10-16T01:50:50.006102Z；接口返回成功；完整性尚需核验。
- 2025：返回540场，抓取时间2026-10-16T01:50:50.330528Z；接口返回成功；完整性尚需核验。
- 2024：返回526场，抓取时间2026-10-16T01:50:50.598653Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-16T01:50:12.817334Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-26T00:00:00Z",
    "train_last_kickoff": "2026-04-25T18:30:00Z",
    "test_start": "2026-04-26T00:30:00Z",
    "test_end": "2026-10-15T02:30:00Z",
    "training_count": 1193,
    "testing_count": 299,
    "result_availability_delay_hours": 4,
    "embargoed_count": 5,
    "embargoed_match_ids": [
      1490236,
      1490239,
      1490240,
      1490241,
      1490242
    ]
  },
  "coverage": {
    "total": 299,
    "scored": 299,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 299,
    "log_loss": 1.0965138313097502,
    "brier_score": 0.6628149181999413,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 299,
    "log_loss": 1.0755691107617853,
    "brier_score": 0.6497345595044224,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.7205697528278174,
    "away_rate": 1.3602848764139086,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.020944720547964923,
    "model_minus_baseline_brier_score": 0.013080358695518934,
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

- 2026：返回156场，抓取时间2026-10-16T01:50:53.055245Z；接口返回成功；完整性尚需核验。
- 2024：返回188场，抓取时间2026-10-13T07:09:01.974793Z；接口返回成功；完整性尚需核验。
- 2022：返回168场，抓取时间2026-10-13T07:09:02.333026Z；接口返回成功；完整性尚需核验。

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

- 2026：返回153场，抓取时间2026-10-16T01:50:55.529056Z；接口返回成功；完整性尚需核验。
- 2025：返回337场，抓取时间2026-10-12T01:38:00.982032Z；接口返回成功；完整性尚需核验。
- 2024：返回340场，抓取时间2026-10-12T01:38:01.244996Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-04T00:00:00Z",
    "train_last_kickoff": "2026-03-23T01:06:00Z",
    "test_start": "2026-04-04T01:00:00Z",
    "test_end": "2026-10-12T01:15:00Z",
    "training_count": 617,
    "testing_count": 157,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 157,
    "scored": 146,
    "excluded": 11,
    "ratio": 0.9299363057324841,
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
      },
      {
        "match_id": 1550985,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 146,
    "log_loss": 1.114808433548995,
    "brier_score": 0.6732666008588097,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 146,
    "log_loss": 1.088624549876867,
    "brier_score": 0.6604120912896227,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.634817813765182,
    "away_rate": 1.1862348178137652,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.026183883672127983,
    "model_minus_baseline_brier_score": 0.01285450956918699,
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

- 2026：返回462场，抓取时间2026-10-16T01:50:57.701916Z；接口返回成功；完整性尚需核验。
- 2025：返回468场，抓取时间2026-10-12T01:38:03.132496Z；接口返回成功；完整性尚需核验。
- 2024：返回468场，抓取时间2026-10-12T01:38:03.363918Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-13T01:39:25.921509Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-02T00:00:00Z",
    "train_last_kickoff": "2026-04-01T19:30:00Z",
    "test_start": "2026-04-02T17:00:00Z",
    "test_end": "2026-10-12T19:00:00Z",
    "training_count": 827,
    "testing_count": 208,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 208,
    "scored": 175,
    "excluded": 33,
    "ratio": 0.8413461538461539,
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
      },
      {
        "match_id": 1569966,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569968,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 175,
    "log_loss": 1.008648830348647,
    "brier_score": 0.6034777300318936,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 175,
    "log_loss": 1.0564113943756774,
    "brier_score": 0.6381543828667551,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4435045317220543,
    "away_rate": 1.1220543806646526,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.047762564027030496,
    "model_minus_baseline_brier_score": -0.03467665283486143,
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

- 2026：返回495场，抓取时间2026-10-16T01:50:59.614952Z；接口返回成功；完整性尚需核验。
- 2025：返回510场，抓取时间2026-10-12T01:38:05.330194Z；接口返回成功；完整性尚需核验。
- 2024：返回378场，抓取时间2026-10-12T01:38:05.566930Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-15T12:37:46.041190Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-03T00:00:00Z",
    "train_last_kickoff": "2026-04-02T18:00:00Z",
    "test_start": "2026-04-03T18:00:00Z",
    "test_end": "2026-10-14T22:00:00Z",
    "training_count": 1055,
    "testing_count": 266,
    "result_availability_delay_hours": 4,
    "embargoed_count": 2,
    "embargoed_match_ids": [
      1492011,
      1492010
    ]
  },
  "coverage": {
    "total": 266,
    "scored": 266,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 266,
    "log_loss": 1.0709569987964853,
    "brier_score": 0.6478697400338993,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 266,
    "log_loss": 1.079741762868507,
    "brier_score": 0.6534881270588413,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.1487446707721458,
    "away_rate": 0.8389388915206063,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.008784764072021778,
    "model_minus_baseline_brier_score": -0.005618387024941995,
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
