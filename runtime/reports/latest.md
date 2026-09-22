# 足球模型运行报告

生成时间：2026-09-22T13:59:02.719327Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|状态|可用90分钟赛果|训练参数变化|说明|
|---|---|---:|---|---|
|英超|探索性基线|810|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西甲|探索性基线|829|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德甲|探索性基线|651|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|意甲|探索性基线|810|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法甲|探索性基线|659|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德乙|探索性基线|669|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法乙|探索性基线|681|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧冠|仅审计|639|未训练|欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。|
|瑞典超|探索性基线|660|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|荷甲|探索性基线|688|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|葡超|探索性基线|678|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|巴西甲|探索性基线|1037|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|日职联J1|探索性基线|608|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|韩国K1|探索性基线|642|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|美职联MLS|探索性基线|1432|完成新拟合|未含阵容伤停、xG与赛程；未证实预测或收益优势。|

本次接口请求次数：58。

## 怎样阅读

- 已取得数据，不代表该赛事全部历史数据完整；请查看各赛季统计和请求失败项。
- 已拟合／沿用参数，不代表通过准确性或收益验收。参数和所有概率都是探索性估计。
- 验证采用按日期划分的时间留出集，未来时段不参与那次拟合；不是完整滚动回测。
- 训练窗口、半衰期、正则强度采用预先固定默认值，尚未调优；不能据反复查看同一留出集声称独立验收。
- 历史数据可能包含事后修订；无原始发布时间快照，历史检查不是严格还原当时供应商资料。
- 正式预测文件保存数据截点、模型版本和未覆盖因素；阵容影响未量化，EV未量化。
- 中立场/特殊场地和赛制尚未自动识别，预测仅为按名义主客生成的条件基线，使用前必须核实。
- 新队或与训练队伍缺少连接的队伍不出数值；陈旧模型不自动发布新预测。
- 单项失败会保留旧模型文件，但本次不会把旧文件伪装为新训练，也不会用失败的数据生成预测。

## 英超

- 2026：返回380场，抓取时间2026-09-22T13:59:09.445174Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-09-22T11:58:10.276009Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-09-22T11:38:33.467033Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T11:59:37.749723Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-02-22T00:00:00Z",
    "train_last_kickoff": "2026-02-21T20:00:00Z",
    "test_start": "2026-02-22T14:00:00Z",
    "test_end": "2026-09-20T15:30:00Z",
    "training_count": 646,
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
    "log_loss": 1.037203612314545,
    "brier_score": 0.6201796784719195,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 155,
    "log_loss": 1.0919218998841462,
    "brier_score": 0.6614788141773641,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.519721577726218,
    "away_rate": 1.3433874709976799,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.05471828756960129,
    "model_minus_baseline_brier_score": -0.041299135705444634,
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

- 2026：返回380场，抓取时间2026-09-22T13:59:22.444698Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-09-22T13:59:28.951268Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-09-22T13:59:35.448239Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-21T00:00:00Z",
    "train_last_kickoff": "2026-03-20T20:00:00Z",
    "test_start": "2026-03-21T13:00:00Z",
    "test_end": "2026-09-20T19:00:00Z",
    "training_count": 661,
    "testing_count": 168,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 168,
    "scored": 148,
    "excluded": 20,
    "ratio": 0.8809523809523809,
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
      }
    ]
  },
  "model_metrics": {
    "n": 148,
    "log_loss": 0.9851099683447847,
    "brier_score": 0.5842896550892052,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 148,
    "log_loss": 1.0421835581276937,
    "brier_score": 0.6278014735305717,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5003779289493575,
    "away_rate": 1.1436130007558578,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.057073589782908996,
    "model_minus_baseline_brier_score": -0.043511818441366534,
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

- 2026：返回306场，抓取时间2026-09-22T13:59:48.448404Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-09-22T13:59:54.947765Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-09-22T14:00:01.629102Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-01T00:00:00Z",
    "train_last_kickoff": "2026-02-28T17:30:00Z",
    "test_start": "2026-03-01T14:30:00Z",
    "test_end": "2026-09-20T17:30:00Z",
    "training_count": 520,
    "testing_count": 131,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 131,
    "scored": 122,
    "excluded": 9,
    "ratio": 0.9312977099236641,
    "exclusions": [
      {
        "match_id": 1545417,
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
      }
    ]
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 0.9647855412429569,
    "brier_score": 0.5703758372806355,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0685162889855357,
    "brier_score": 0.6472196192936618,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6820365033621518,
    "away_rate": 1.4707012487992315,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.10373074774257884,
    "model_minus_baseline_brier_score": -0.07684378201302633,
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

- 2026：返回380场，抓取时间2026-09-22T14:00:14.519249Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-09-22T14:00:20.957204Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-09-22T14:00:27.467801Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-02T00:00:00Z",
    "train_last_kickoff": "2026-03-01T19:45:00Z",
    "test_start": "2026-03-02T17:30:00Z",
    "test_end": "2026-09-20T18:45:00Z",
    "training_count": 648,
    "testing_count": 162,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 162,
    "scored": 157,
    "excluded": 5,
    "ratio": 0.9691358024691358,
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
      }
    ]
  },
  "model_metrics": {
    "n": 157,
    "log_loss": 0.9747729183178263,
    "brier_score": 0.5796389280763297,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 157,
    "log_loss": 1.0703694514677629,
    "brier_score": 0.6485679872943252,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3145720894371626,
    "away_rate": 1.1927525057825752,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.09559653314993655,
    "model_minus_baseline_brier_score": -0.06892905921799553,
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

- 2026：返回306场，抓取时间2026-09-22T14:00:40.489001Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-09-22T14:00:46.950462Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-09-22T14:00:53.448928Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-08T00:00:00Z",
    "train_last_kickoff": "2026-03-07T18:00:00Z",
    "test_start": "2026-03-08T14:00:00Z",
    "test_end": "2026-09-20T18:45:00Z",
    "training_count": 526,
    "testing_count": 132,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1387922
    ]
  },
  "coverage": {
    "total": 132,
    "scored": 122,
    "excluded": 10,
    "ratio": 0.9242424242424242,
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
      }
    ]
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 1.050323514244506,
    "brier_score": 0.6362446368743706,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0939297060569246,
    "brier_score": 0.6633517856787604,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6058879392212726,
    "away_rate": 1.288698955365622,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04360619181241865,
    "model_minus_baseline_brier_score": -0.027107148804389802,
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

- 2026：返回306场，抓取时间2026-09-22T14:01:06.453241Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-09-22T14:01:12.945466Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-09-22T14:01:19.447203Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-14T00:00:00Z",
    "train_last_kickoff": "2026-03-13T17:30:00Z",
    "test_start": "2026-03-14T12:00:00Z",
    "test_end": "2026-09-20T11:30:00Z",
    "training_count": 534,
    "testing_count": 135,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 135,
    "scored": 107,
    "excluded": 28,
    "ratio": 0.7925925925925926,
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
      }
    ]
  },
  "model_metrics": {
    "n": 107,
    "log_loss": 1.0373890274138287,
    "brier_score": 0.6243100635560455,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 107,
    "log_loss": 1.0572039214252986,
    "brier_score": 0.6378411681179919,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5949485500467726,
    "away_rate": 1.3666978484565013,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.019814894011469963,
    "model_minus_baseline_brier_score": -0.013531104561946394,
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

- 2026：返回306场，抓取时间2026-09-22T14:01:32.435438Z；接口返回成功；完整性尚需核验。
- 2025：返回310场，抓取时间2026-09-22T14:01:38.944353Z；接口返回成功；完整性尚需核验。
- 2024：返回310场，抓取时间2026-09-22T14:01:45.455887Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-13T00:00:00Z",
    "train_last_kickoff": "2026-03-09T19:45:00Z",
    "test_start": "2026-03-13T19:00:00Z",
    "test_end": "2026-09-19T18:00:00Z",
    "training_count": 543,
    "testing_count": 138,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 138,
    "scored": 116,
    "excluded": 22,
    "ratio": 0.8405797101449275,
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
      }
    ]
  },
  "model_metrics": {
    "n": 116,
    "log_loss": 1.0617767232752142,
    "brier_score": 0.6427660073861412,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 116,
    "log_loss": 1.1021117598654961,
    "brier_score": 0.6689645728257696,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3992640294388226,
    "away_rate": 1.1269549218031278,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.040335036590281925,
    "model_minus_baseline_brier_score": -0.026198565439628396,
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

- 2026：返回234场，抓取时间2026-09-22T14:01:58.455460Z；接口返回成功；完整性尚需核验。
- 2025：返回281场，抓取时间2026-09-22T14:02:04.941878Z；接口返回成功；完整性尚需核验。
- 2024：返回279场，抓取时间2026-09-22T14:02:11.462560Z；接口返回成功；完整性尚需核验。
## 瑞典超

- 2026：返回240场，抓取时间2026-09-22T14:02:24.439286Z；接口返回成功；完整性尚需核验。
- 2025：返回242场，抓取时间2026-09-22T14:02:30.943424Z；接口返回成功；完整性尚需核验。
- 2024：返回242场，抓取时间2026-09-22T14:02:37.465529Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-03T00:00:00Z",
    "train_last_kickoff": "2026-05-02T13:00:00Z",
    "test_start": "2026-05-03T12:00:00Z",
    "test_end": "2026-09-20T14:30:00Z",
    "training_count": 526,
    "testing_count": 134,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 134,
    "scored": 134,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 134,
    "log_loss": 1.014090887337719,
    "brier_score": 0.6051517930637095,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 134,
    "log_loss": 1.062343738800278,
    "brier_score": 0.6437025863356207,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.5280151946818614,
    "away_rate": 1.3095916429249763,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04825285146255909,
    "model_minus_baseline_brier_score": -0.03855079327191124,
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

- 2026：返回306场，抓取时间2026-09-22T14:02:50.464866Z；接口返回成功；完整性尚需核验。
- 2025：返回309场，抓取时间2026-09-22T14:02:56.950299Z；接口返回成功；完整性尚需核验。
- 2024：返回321场，抓取时间2026-09-22T14:03:03.469952Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-13T00:00:00Z",
    "train_last_kickoff": "2026-03-08T19:00:00Z",
    "test_start": "2026-03-13T19:00:00Z",
    "test_end": "2026-09-20T14:45:00Z",
    "training_count": 551,
    "testing_count": 137,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 137,
    "scored": 137,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 137,
    "log_loss": 0.9890370896896303,
    "brier_score": 0.5881402626228807,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 137,
    "log_loss": 1.1003027845918396,
    "brier_score": 0.6688434562834299,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.7234814143245694,
    "away_rate": 1.342701722574796,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.11126569490220928,
    "model_minus_baseline_brier_score": -0.08070319366054923,
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

- 2026：返回306场，抓取时间2026-09-22T14:03:16.444431Z；接口返回成功；完整性尚需核验。
- 2025：返回308场，抓取时间2026-09-22T14:03:22.971087Z；接口返回成功；完整性尚需核验。
- 2024：返回308场，抓取时间2026-09-22T14:03:29.450629Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-21T00:00:00Z",
    "train_last_kickoff": "2026-03-15T20:30:00Z",
    "test_start": "2026-03-21T15:30:00Z",
    "test_end": "2026-09-20T19:30:00Z",
    "training_count": 540,
    "testing_count": 137,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1396475
    ]
  },
  "coverage": {
    "total": 137,
    "scored": 122,
    "excluded": 15,
    "ratio": 0.8905109489051095,
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
      }
    ]
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 0.9965798294285452,
    "brier_score": 0.5974498325361877,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0864369499895496,
    "brier_score": 0.6578569573312858,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4458834412580943,
    "away_rate": 1.188714153561517,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.08985712056100448,
    "model_minus_baseline_brier_score": -0.06040712479509813,
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

- 2026：返回380场，抓取时间2026-09-22T14:03:42.463995Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-09-22T14:03:48.953631Z；接口返回成功；完整性尚需核验。
- 2024：返回380场，抓取时间2026-09-22T14:03:55.458079Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-22T00:00:00Z",
    "train_last_kickoff": "2026-03-21T19:00:00Z",
    "test_start": "2026-03-22T00:00:00Z",
    "test_end": "2026-09-20T22:30:00Z",
    "training_count": 827,
    "testing_count": 209,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1492184
    ]
  },
  "coverage": {
    "total": 209,
    "scored": 209,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 209,
    "log_loss": 1.0308018455681907,
    "brier_score": 0.6212626509078011,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 209,
    "log_loss": 1.0594882941043988,
    "brier_score": 0.6391927816153575,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4749244712990937,
    "away_rate": 1.0265861027190333,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.028686448536208164,
    "model_minus_baseline_brier_score": -0.017930130707556446,
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

- 2027：返回380场，抓取时间2026-09-22T14:04:08.488543Z；接口返回成功；完整性尚需核验。
- 2026：返回200场，抓取时间2026-09-22T14:04:14.956805Z；接口返回成功；完整性尚需核验。
- 2025：返回380场，抓取时间2026-09-22T14:04:21.456874Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-10T00:00:00Z",
    "train_last_kickoff": "2026-05-09T07:00:00Z",
    "test_start": "2026-05-10T05:00:00Z",
    "test_end": "2026-09-20T08:00:00Z",
    "training_count": 486,
    "testing_count": 122,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 122,
    "scored": 122,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 122,
    "log_loss": 1.004257139319615,
    "brier_score": 0.5954383386699638,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0646654255665948,
    "brier_score": 0.6445463214920983,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3617677286742036,
    "away_rate": 1.129496402877698,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.060408286246979914,
    "model_minus_baseline_brier_score": -0.04910798282213458,
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

- 2026：返回198场，抓取时间2026-09-22T14:04:34.469553Z；接口返回成功；完整性尚需核验。
- 2025：返回232场，抓取时间2026-09-22T14:04:40.944386Z；接口返回成功；完整性尚需核验。
- 2024：返回232场，抓取时间2026-09-22T14:04:47.452404Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-22T00:00:00Z",
    "train_last_kickoff": "2026-04-21T10:30:00Z",
    "test_start": "2026-04-22T10:30:00Z",
    "test_end": "2026-09-20T10:00:00Z",
    "training_count": 514,
    "testing_count": 128,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 128,
    "scored": 128,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 128,
    "log_loss": 1.0811768718029224,
    "brier_score": 0.6547198576387225,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 128,
    "log_loss": 1.1199532416645872,
    "brier_score": 0.6817329822813649,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3828960155490768,
    "away_rate": 1.1671525753158407,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.03877636986166477,
    "model_minus_baseline_brier_score": -0.027013124642642383,
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

- 2026：返回510场，抓取时间2026-09-22T14:05:00.471330Z；接口返回成功；完整性尚需核验。
- 2025：返回540场，抓取时间2026-09-22T14:05:06.973247Z；接口返回成功；完整性尚需核验。
- 2024：返回526场，抓取时间2026-09-22T14:05:13.469650Z；接口返回成功；完整性尚需核验。

验证结果（完整字段见同名JSON）：

```json
{
  "validation_status": "exploratory_single_fixed_holdout",
  "deployment_approved": false,
  "is_rolling_backtest": false,
  "minimum_test_matches": 40,
  "meets_minimum_supported_test_matches": true,
  "evaluation_data_cutoff": "2026-09-22T13:59:02.719327Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-18T00:00:00Z",
    "train_last_kickoff": "2026-04-12T23:00:00Z",
    "test_start": "2026-04-18T02:30:00Z",
    "test_end": "2026-09-20T23:00:00Z",
    "training_count": 1148,
    "testing_count": 284,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 284,
    "scored": 284,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 284,
    "log_loss": 1.1093141719458994,
    "brier_score": 0.6724068500418223,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 284,
    "log_loss": 1.0858193516336527,
    "brier_score": 0.6572471038198499,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.7226817588158467,
    "away_rate": 1.3578580757509795,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.0234948203122467,
    "model_minus_baseline_brier_score": 0.01515974622197247,
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
