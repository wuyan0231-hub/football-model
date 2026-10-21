# 足球模型运行报告

生成时间：2026-10-21T06:06:54.338086Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|
|---|---|---|---|---:|---|---|
|英超|探索性基线|available|10/0/10|830|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|英冠|探索性基线|available|12/0/12|1246|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西甲|探索性基线|available|11/0/11|848|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德甲|探索性基线|available|9/0/9|670|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|意甲|探索性基线|available|13/0/13|830|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法甲|探索性基线|available|9/0/9|678|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德乙|探索性基线|available|9/0/9|688|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法乙|探索性基线|available|9/0/9|700|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧冠|仅审计|audit_only|None/None/None|695|未训练|欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。|
|瑞典超|探索性基线|available|9/0/9|676|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|荷甲|探索性基线|available|9/0/9|711|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|葡超|探索性基线|available|9/0/9|688|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|巴西甲|探索性基线|available|10/0/10|1069|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|日职联J1|探索性基线|available|20/0/20|680|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|韩国K1|探索性基线|available|6/0/6|656|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|美职联MLS|探索性基线|available|16/0/16|1512|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|墨西超Liga MX|探索性基线|available|14/0/14|786|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西乙|探索性基线|available|11/0/11|1046|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|阿根廷甲级联赛|探索性基线|available|15/0/15|1338|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|

预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。

本次接口网络尝试次数：37（含状态查询与重试，不等于当日计费已用量）。

## API额度与限速

|项目|本次记录|
|---|---|
|配置的每日上限|7460|
|供应商日上限／当日已用（最近观测）|7500／37|
|供应商最近回报余额|7463|
|当前有效日余额（保守估计）|7423|
|日额证据状态|known|
|每轮网络次数上限|不设额外次数上限|
|供应商分钟上限／最近剩余|300／263|
|当前请求最小间隔（秒）|0.21000000000000002|
|网络请求软截止／已运行（秒）|3600／52.347784324|
|额度最近观测时间／来源|2026-10-21T06:06:51.788376Z／response_headers|

每轮不设额外次数上限时，仍遵守已核实的当日额度、后续下载预留、分钟限速及软截止；不把每日上限当作每轮可重新使用的余额。未知额度不填零，也不以配置值冒充已核实余额。


## 怎样阅读

- 已取得数据，不代表该赛事全部历史数据完整；请查看各赛季统计和请求失败项。
- 已拟合／沿用参数，不代表通过准确性或收益验收。参数和所有概率都是探索性估计。
- 验证采用按日期划分的时间留出集，未来时段不参与那次拟合；不是完整滚动回测。
- 训练窗口、半衰期、正则强度采用预先固定默认值，尚未调优；不能据反复查看同一留出集声称独立验收。
- 历史数据可能包含事后修订；无原始发布时间快照，历史检查不是严格还原当时供应商资料。
- 正式预测文件保存数据截点、模型版本和未覆盖因素；阵容影响未量化，EV未量化。
- 国内联赛按名义主客生成条件基线；实际场地和特殊赛制仍须核实。
- 新队或与训练队伍缺少连接的队伍不出数值；陈旧模型不自动发布新预测。
- 单项失败会保留旧模型文件，但本次不会把旧文件伪装为新训练，也不会用失败的数据生成预测。

## 英超

- 2026：返回380场，抓取时间2026-10-21T06:06:02.764908Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-04T00:00:00Z",
    "train_last_kickoff": "2026-03-03T19:30:00Z",
    "test_start": "2026-03-04T19:30:00Z",
    "test_end": "2026-10-19T19:00:00Z",
    "training_count": 664,
    "testing_count": 165,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1379258
    ]
  },
  "coverage": {
    "total": 165,
    "scored": 152,
    "excluded": 13,
    "ratio": 0.9212121212121213,
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
      },
      {
        "match_id": 1557431,
        "reason": "未知球队 ID：[64]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1557436,
        "reason": "未知球队 ID：[1346]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 152,
    "log_loss": 1.0356780068032254,
    "brier_score": 0.620304718390474,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 152,
    "log_loss": 1.0967678058255046,
    "brier_score": 0.6644764552022654,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5176824680210685,
    "away_rate": 1.344620015048909,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.06108979902227918,
    "model_minus_baseline_brier_score": -0.04417173681179143,
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

- 2026：返回552场，抓取时间2026-10-21T06:06:05.225240Z；接口返回成功；完整性尚需核验。
- 2025：返回558场，抓取时间2026-10-17T06:32:54.827443Z；接口返回成功；完整性尚需核验。
- 2024：返回557场，抓取时间2026-10-17T06:32:55.048786Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-21T06:06:01.976464Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-13T00:00:00Z",
    "train_last_kickoff": "2026-03-11T20:00:00Z",
    "test_start": "2026-03-13T20:00:00Z",
    "test_end": "2026-10-20T18:45:00Z",
    "training_count": 998,
    "testing_count": 248,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 248,
    "scored": 209,
    "excluded": 39,
    "ratio": 0.842741935483871,
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
      },
      {
        "match_id": 1563205,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563208,
        "reason": "未知球队 ID：[1379]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563210,
        "reason": "未知球队 ID：[68]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563211,
        "reason": "未知球队 ID：[48]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1563150,
        "reason": "未知球队 ID：[39]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 209,
    "log_loss": 1.0936280218711194,
    "brier_score": 0.6622568156942852,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 209,
    "log_loss": 1.091879050014251,
    "brier_score": 0.662081802352303,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4096144216324487,
    "away_rate": 1.1021532298447672,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.0017489718568683976,
    "model_minus_baseline_brier_score": 0.00017501334198222196,
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

- 2026：返回380场，抓取时间2026-10-21T06:06:08.129706Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-06T00:00:00Z",
    "train_last_kickoff": "2026-04-05T19:00:00Z",
    "test_start": "2026-04-06T19:00:00Z",
    "test_end": "2026-10-19T19:00:00Z",
    "training_count": 679,
    "testing_count": 169,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 169,
    "scored": 143,
    "excluded": 26,
    "ratio": 0.8461538461538461,
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
      },
      {
        "match_id": 1570415,
        "reason": "未知球队 ID：[544]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570419,
        "reason": "未知球队 ID：[4665]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1570418,
        "reason": "未知球队 ID：[535]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 143,
    "log_loss": 0.976126699163729,
    "brier_score": 0.5783197164810081,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 143,
    "log_loss": 1.0566628431341123,
    "brier_score": 0.6377642050595451,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5047829286239882,
    "away_rate": 1.1456953642384107,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.08053614397038333,
    "model_minus_baseline_brier_score": -0.059444488578537014,
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

- 2026：返回306场，抓取时间2026-10-21T06:06:10.754719Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-14T00:00:00Z",
    "train_last_kickoff": "2026-03-13T19:30:00Z",
    "test_start": "2026-03-14T14:30:00Z",
    "test_end": "2026-10-18T15:30:00Z",
    "training_count": 534,
    "testing_count": 136,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 136,
    "scored": 122,
    "excluded": 14,
    "ratio": 0.8970588235294118,
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
      },
      {
        "match_id": 1575191,
        "reason": "未知球队 ID：[185]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1575192,
        "reason": "未知球队 ID：[174]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 0.9860753299001811,
    "brier_score": 0.5872066075911048,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0626524188265891,
    "brier_score": 0.6427545449076065,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6847521047708138,
    "away_rate": 1.4733395696913003,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.07657708892640802,
    "model_minus_baseline_brier_score": -0.05554793731650176,
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

- 2026：返回380场，抓取时间2026-10-21T06:06:13.640266Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-15T00:00:00Z",
    "train_last_kickoff": "2026-03-14T19:45:00Z",
    "test_start": "2026-03-15T11:30:00Z",
    "test_end": "2026-10-19T18:45:00Z",
    "training_count": 664,
    "testing_count": 166,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 166,
    "scored": 159,
    "excluded": 7,
    "ratio": 0.9578313253012049,
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
      },
      {
        "match_id": 1550151,
        "reason": "未知球队 ID：[512]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 159,
    "log_loss": 0.9691079128135133,
    "brier_score": 0.575077595961561,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 159,
    "log_loss": 1.0709063365266251,
    "brier_score": 0.649012246112739,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3235515425131679,
    "away_rate": 1.1866064710308504,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.10179842371311187,
    "model_minus_baseline_brier_score": -0.07393465015117795,
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

- 2026：返回306场，抓取时间2026-10-21T06:06:16.353599Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-21T00:00:00Z",
    "train_last_kickoff": "2026-03-20T19:45:00Z",
    "test_start": "2026-03-21T16:00:00Z",
    "test_end": "2026-10-18T18:45:00Z",
    "training_count": 542,
    "testing_count": 136,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 136,
    "scored": 122,
    "excluded": 14,
    "ratio": 0.8970588235294118,
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
      },
      {
        "match_id": 1552785,
        "reason": "未知球队 ID：[1298]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552791,
        "reason": "未知球队 ID：[110]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 1.0335458714866892,
    "brier_score": 0.6227313420739851,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0808040736661468,
    "brier_score": 0.6543006842039452,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.599078341013825,
    "away_rate": 1.2838709677419355,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04725820217945764,
    "model_minus_baseline_brier_score": -0.0315693421299601,
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

- 2026：返回306场，抓取时间2026-10-21T06:06:18.952621Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-04T00:00:00Z",
    "train_last_kickoff": "2026-03-22T12:30:00Z",
    "test_start": "2026-04-04T11:00:00Z",
    "test_end": "2026-10-18T11:30:00Z",
    "training_count": 551,
    "testing_count": 137,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 137,
    "scored": 100,
    "excluded": 37,
    "ratio": 0.7299270072992701,
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
      },
      {
        "match_id": 1576198,
        "reason": "未知球队 ID：[1320]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576205,
        "reason": "未知球队 ID：[161, 180]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576200,
        "reason": "未知球队 ID：[186]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1576197,
        "reason": "未知球队 ID：[1324]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 100,
    "log_loss": 1.0295010775372848,
    "brier_score": 0.6195555981370776,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 100,
    "log_loss": 1.045934356875769,
    "brier_score": 0.6300786264007663,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6092475067996375,
    "away_rate": 1.369900271985494,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.016433279338484263,
    "model_minus_baseline_brier_score": -0.010523028263688672,
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

- 2026：返回306场，抓取时间2026-10-21T06:06:21.566709Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-03T00:00:00Z",
    "train_last_kickoff": "2026-03-21T19:00:00Z",
    "test_start": "2026-04-03T18:00:00Z",
    "test_end": "2026-10-19T18:45:00Z",
    "training_count": 561,
    "testing_count": 139,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 139,
    "scored": 111,
    "excluded": 28,
    "ratio": 0.7985611510791367,
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
      },
      {
        "match_id": 1552502,
        "reason": "未知球队 ID：[89]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552499,
        "reason": "未知球队 ID：[115]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1552500,
        "reason": "未知球队 ID：[83]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 111,
    "log_loss": 1.0696854330004748,
    "brier_score": 0.6487530956203803,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 111,
    "log_loss": 1.1018241832222735,
    "brier_score": 0.6688239022418306,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3971504897595726,
    "away_rate": 1.1282279608192343,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.032138750221798684,
    "model_minus_baseline_brier_score": -0.020070806621450332,
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

- 2026：返回234场，抓取时间2026-10-21T06:06:24.162807Z；接口返回成功；完整性尚需核验。
- 2025：返回281场，抓取时间2026-10-16T01:50:33.285212Z；接口返回成功；完整性尚需核验。
- 2024：返回279场，抓取时间2026-10-16T01:50:33.521192Z；接口返回成功；完整性尚需核验。
## 瑞典超

- 2026：返回240场，抓取时间2026-10-21T06:06:26.714379Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-16T00:00:00Z",
    "train_last_kickoff": "2026-05-11T17:00:00Z",
    "test_start": "2026-05-16T13:00:00Z",
    "test_end": "2026-10-19T17:00:00Z",
    "training_count": 540,
    "testing_count": 136,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 136,
    "scored": 136,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 136,
    "log_loss": 1.0573457996000375,
    "brier_score": 0.6369826012805714,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 136,
    "log_loss": 1.0694118851243726,
    "brier_score": 0.6484390798924918,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.54209065679926,
    "away_rate": 1.3108233117483812,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.01206608552433508,
    "model_minus_baseline_brier_score": -0.011456478611920451,
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

- 2026：返回306场，抓取时间2026-10-21T06:06:29.403611Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-22T00:00:00Z",
    "train_last_kickoff": "2026-03-21T20:00:00Z",
    "test_start": "2026-03-22T11:15:00Z",
    "test_end": "2026-10-18T18:00:00Z",
    "training_count": 568,
    "testing_count": 143,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 143,
    "scored": 143,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 143,
    "log_loss": 1.0129818487515867,
    "brier_score": 0.6054199332816403,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 143,
    "log_loss": 1.1147510616688399,
    "brier_score": 0.6785084783427826,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.735268249780123,
    "away_rate": 1.334212840809147,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.10176921291725316,
    "model_minus_baseline_brier_score": -0.07308854506114226,
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

- 2026：返回306场，抓取时间2026-10-21T06:06:32.089095Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-03T00:00:00Z",
    "train_last_kickoff": "2026-03-22T20:30:00Z",
    "test_start": "2026-04-03T14:30:00Z",
    "test_end": "2026-10-19T19:15:00Z",
    "training_count": 549,
    "testing_count": 139,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 139,
    "scored": 122,
    "excluded": 17,
    "ratio": 0.8776978417266187,
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
    "n": 122,
    "log_loss": 0.9910445973471532,
    "brier_score": 0.5929753438947253,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.084914521374719,
    "brier_score": 0.6569934854237622,
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
    "model_minus_baseline_log_loss": -0.09386992402756589,
    "model_minus_baseline_brier_score": -0.06401814152903695,
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

- 2026：返回380场，抓取时间2026-10-21T06:06:34.740638Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-11T00:00:00Z",
    "train_last_kickoff": "2026-04-05T23:30:00Z",
    "test_start": "2026-04-11T19:30:00Z",
    "test_end": "2026-10-19T23:00:00Z",
    "training_count": 857,
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
    "log_loss": 1.0081293233860296,
    "brier_score": 0.6059843357305171,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 212,
    "log_loss": 1.0635162299407401,
    "brier_score": 0.6421056979006801,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.482798833819242,
    "away_rate": 1.0209912536443149,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.05538690655471057,
    "model_minus_baseline_brier_score": -0.03612136217016293,
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

- 2027：返回380场，抓取时间2026-10-21T06:06:37.518966Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-17T00:00:00Z",
    "train_last_kickoff": "2026-05-16T08:00:00Z",
    "test_start": "2026-05-17T04:00:00Z",
    "test_end": "2026-10-18T07:00:00Z",
    "training_count": 544,
    "testing_count": 136,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 136,
    "scored": 136,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 136,
    "log_loss": 1.0072121082353154,
    "brier_score": 0.5997915829529967,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 136,
    "log_loss": 1.076089399459788,
    "brier_score": 0.6520813612620311,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3213957759412305,
    "away_rate": 1.1175390266299357,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.06887729122447261,
    "model_minus_baseline_brier_score": -0.05228977830903436,
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

- 2026：返回198场，抓取时间2026-10-21T06:06:40.364972Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-02T00:00:00Z",
    "train_last_kickoff": "2026-04-26T07:30:00Z",
    "test_start": "2026-05-02T05:00:00Z",
    "test_end": "2026-10-18T07:30:00Z",
    "training_count": 524,
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
    "log_loss": 1.0739473870074734,
    "brier_score": 0.6499959288295691,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 132,
    "log_loss": 1.1136771423675382,
    "brier_score": 0.676730349557659,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3794089609151572,
    "away_rate": 1.1792183031458532,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.039729755360064845,
    "model_minus_baseline_brier_score": -0.026734420728089847,
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

- 2026：返回510场，抓取时间2026-10-21T06:06:43.149830Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-19T06:18:14.805568Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-02T00:00:00Z",
    "train_last_kickoff": "2026-04-26T23:00:00Z",
    "test_start": "2026-05-02T17:00:00Z",
    "test_end": "2026-10-18T21:00:00Z",
    "training_count": 1206,
    "testing_count": 306,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 306,
    "scored": 306,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 306,
    "log_loss": 1.1047974996658925,
    "brier_score": 0.6678739132196745,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 306,
    "log_loss": 1.088192949411122,
    "brier_score": 0.6583505549330937,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.726067136344799,
    "away_rate": 1.3572316618317448,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.016604550254770478,
    "model_minus_baseline_brier_score": 0.009523358286580796,
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

- 2026：返回153场，抓取时间2026-10-21T06:06:45.889760Z；接口返回成功；完整性尚需核验。
- 2025：返回337场，抓取时间2026-10-19T06:19:42.644472Z；接口返回成功；完整性尚需核验。
- 2024：返回340场，抓取时间2026-10-19T06:19:42.863101Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-21T06:06:01.976464Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-11T00:00:00Z",
    "train_last_kickoff": "2026-04-08T00:00:00Z",
    "test_start": "2026-04-11T01:00:00Z",
    "test_end": "2026-10-21T01:00:00Z",
    "training_count": 627,
    "testing_count": 159,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 159,
    "scored": 146,
    "excluded": 13,
    "ratio": 0.9182389937106918,
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
      },
      {
        "match_id": 1550995,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1551003,
        "reason": "未知球队 ID：[2312]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 146,
    "log_loss": 1.0841587576589395,
    "brier_score": 0.6531546550336851,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 146,
    "log_loss": 1.0839118265951804,
    "brier_score": 0.6571184292662805,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6294820717131475,
    "away_rate": 1.1832669322709164,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.0002469310637591704,
    "model_minus_baseline_brier_score": -0.003963774232595418,
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

- 2026：返回462场，抓取时间2026-10-21T06:06:48.918075Z；接口返回成功；完整性尚需核验。
- 2025：返回468场，抓取时间2026-10-19T06:19:47.138902Z；接口返回成功；完整性尚需核验。
- 2024：返回468场，抓取时间2026-10-19T06:19:47.350460Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-05T00:00:00Z",
    "train_last_kickoff": "2026-04-04T16:30:00Z",
    "test_start": "2026-04-05T12:00:00Z",
    "test_end": "2026-10-19T18:30:00Z",
    "training_count": 835,
    "testing_count": 211,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 211,
    "scored": 174,
    "excluded": 37,
    "ratio": 0.8246445497630331,
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
      },
      {
        "match_id": 1569973,
        "reason": "未知球队 ID：[798]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569975,
        "reason": "未知球队 ID：[547]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569970,
        "reason": "未知球队 ID：[9593]；缺少训练数据，当前模型不支持。"
      },
      {
        "match_id": 1569971,
        "reason": "未知球队 ID：[9571]；缺少训练数据，当前模型不支持。"
      }
    ]
  },
  "model_metrics": {
    "n": 174,
    "log_loss": 0.9999560624076235,
    "brier_score": 0.5975096839060791,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 174,
    "log_loss": 1.0565007288882946,
    "brier_score": 0.6384273947868196,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4416517055655296,
    "away_rate": 1.1220825852782765,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.056544666480671135,
    "model_minus_baseline_brier_score": -0.04091771088074059,
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

- 2026：返回495场，抓取时间2026-10-21T06:06:51.845786Z；接口返回成功；完整性尚需核验。
- 2025：返回510场，抓取时间2026-10-19T06:19:51.678160Z；接口返回成功；完整性尚需核验。
- 2024：返回378场，抓取时间2026-10-19T06:19:51.876153Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-10-20T06:07:10.452032Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-11T00:00:00Z",
    "train_last_kickoff": "2026-04-07T00:15:00Z",
    "test_start": "2026-04-11T00:00:00Z",
    "test_end": "2026-10-20T00:15:00Z",
    "training_count": 1068,
    "testing_count": 270,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 270,
    "scored": 270,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 270,
    "log_loss": 1.0685173799045324,
    "brier_score": 0.646443854630662,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 270,
    "log_loss": 1.0888325258143987,
    "brier_score": 0.659804545743021,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.1516144127281236,
    "away_rate": 0.8418343472157229,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.020315145909866272,
    "model_minus_baseline_brier_score": -0.013360691112359047,
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
