# 足球模型运行报告

生成时间：2026-10-08T01:34:09.165029Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|
|---|---|---|---|---:|---|---|
|英超|探索性基线|available|10/0/10|810|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西甲|探索性基线|available|10/0/10|829|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德甲|探索性基线|available|9/0/9|652|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|意甲|探索性基线|available|10/0/10|810|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法甲|探索性基线|available|9/0/9|660|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|德乙|探索性基线|available|9/0/9|670|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|法乙|探索性基线|available|9/0/9|682|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧冠|仅审计|audit_only|None/None/None|668|未训练|欧冠跨联赛模型尚未实现；不会拼接国内参数生成欧冠概率。|
|瑞典超|探索性基线|available|8/0/8|660|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|荷甲|探索性基线|available|9/0/9|693|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|葡超|探索性基线|available|9/0/9|678|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|巴西甲|探索性基线|available|14/0/14|1039|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|日职联J1|探索性基线|available|10/0/10|660|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|韩国K1|探索性基线|available|6/0/6|644|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|美职联MLS|探索性基线|available|27/0/27|1467|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|欧洲国家联赛|探索性基线|no_upcoming|0/0/0|434|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。 场地筛选：454场常规赛果中纳入434场、排除20场；未来7天可用预测0场，不支持0场。|
|墨西超Liga MX|探索性基线|available|9/0/9|765|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|西乙|探索性基线|available|11/0/11|1024|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|
|阿根廷甲级联赛|探索性基线|available|16/0/16|1307|数据未变，沿用参数|未含阵容伤停、xG与赛程；未证实预测或收益优势。|

预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。

本次接口网络尝试次数：56（含状态查询与重试，不等于当日计费已用量）。

## API额度与限速

|项目|本次记录|
|---|---|
|配置的每日上限|5000|
|供应商日上限／当日已用（最近观测）|7500／12|
|供应商最近回报余额|7488|
|当前有效日余额（保守估计）|4988|
|日额证据状态|known|
|每轮网络次数上限|不设额外次数上限|
|供应商分钟上限／最近剩余|300／288|
|当前请求最小间隔（秒）|0.21000000000000002|
|网络请求软截止／已运行（秒）|3600／25.015694728|
|额度最近观测时间／来源|2026-10-08T01:34:08.085158Z／response_headers|

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

- 2026：返回380场，抓取时间2026-10-08T01:33:44.715050Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
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

- 2026：返回380场，抓取时间2026-10-08T01:33:45.665755Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
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
    "log_loss": 0.9851099683448425,
    "brier_score": 0.5842896550892365,
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
    "model_minus_baseline_log_loss": -0.05707358978285115,
    "model_minus_baseline_brier_score": -0.043511818441335226,
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

- 2026：返回306场，抓取时间2026-10-08T01:33:46.604997Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-04T00:00:00Z",
    "train_last_kickoff": "2026-03-01T18:30:00Z",
    "test_start": "2026-03-04T19:30:00Z",
    "test_end": "2026-09-20T17:30:00Z",
    "training_count": 523,
    "testing_count": 129,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 129,
    "scored": 119,
    "excluded": 10,
    "ratio": 0.9224806201550387,
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
      }
    ]
  },
  "model_metrics": {
    "n": 119,
    "log_loss": 0.9737440875377393,
    "brier_score": 0.5770224551055229,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 119,
    "log_loss": 1.072133058183249,
    "brier_score": 0.649564330865701,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6857688634192933,
    "away_rate": 1.466093600764088,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.09838897064550978,
    "model_minus_baseline_brier_score": -0.07254187576017812,
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

- 2026：返回380场，抓取时间2026-10-08T01:33:47.530546Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
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

- 2026：返回306场，抓取时间2026-10-08T01:33:48.462170Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-08T00:00:00Z",
    "train_last_kickoff": "2026-03-07T18:00:00Z",
    "test_start": "2026-03-08T14:00:00Z",
    "test_end": "2026-09-20T18:45:00Z",
    "training_count": 527,
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
    "log_loss": 1.050319494477463,
    "brier_score": 0.6362230170234667,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 122,
    "log_loss": 1.0938957872409198,
    "brier_score": 0.6633282431046252,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.604739336492891,
    "away_rate": 1.2881516587677726,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04357629276345687,
    "model_minus_baseline_brier_score": -0.027105226081158507,
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

- 2026：返回306场，抓取时间2026-10-08T01:33:49.570147Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-14T00:00:00Z",
    "train_last_kickoff": "2026-03-13T17:30:00Z",
    "test_start": "2026-03-14T12:00:00Z",
    "test_end": "2026-09-20T11:30:00Z",
    "training_count": 535,
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
    "log_loss": 1.0376294980697105,
    "brier_score": 0.6244869348802135,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 107,
    "log_loss": 1.0574800174485997,
    "brier_score": 0.638048727621463,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.591970121381886,
    "away_rate": 1.3678804855275444,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.019850519378889153,
    "model_minus_baseline_brier_score": -0.013561792741249579,
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

- 2026：返回306场，抓取时间2026-10-08T01:33:50.905896Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-14T00:00:00Z",
    "train_last_kickoff": "2026-03-13T19:00:00Z",
    "test_start": "2026-03-14T13:00:00Z",
    "test_end": "2026-09-19T18:00:00Z",
    "training_count": 548,
    "testing_count": 134,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 134,
    "scored": 112,
    "excluded": 22,
    "ratio": 0.835820895522388,
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
    "n": 112,
    "log_loss": 1.081221321498157,
    "brier_score": 0.6566574432166199,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 112,
    "log_loss": 1.106153416900453,
    "brier_score": 0.6714635672591954,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3956244302643572,
    "away_rate": 1.1276207839562442,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.024932095402296017,
    "model_minus_baseline_brier_score": -0.01480612404257553,
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

- 2026：返回234场，抓取时间2026-10-08T01:33:52.519602Z；接口返回成功；完整性尚需核验。
- 2025：返回281场，抓取时间2026-10-08T01:33:52.723625Z；接口返回成功；完整性尚需核验。
- 2024：返回279场，抓取时间2026-10-08T01:33:52.944926Z；接口返回成功；完整性尚需核验。
## 瑞典超

- 2026：返回240场，抓取时间2026-10-08T01:33:53.765894Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
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
    "log_loss": 1.0140908873377192,
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
    "model_minus_baseline_log_loss": -0.048252851462558866,
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

- 2026：返回306场，抓取时间2026-10-08T01:33:55.101116Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-13T00:00:00Z",
    "train_last_kickoff": "2026-03-08T19:00:00Z",
    "test_start": "2026-03-13T19:00:00Z",
    "test_end": "2026-09-20T14:45:00Z",
    "training_count": 555,
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
    "log_loss": 0.9936391564619192,
    "brier_score": 0.591330192791206,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 138,
    "log_loss": 1.1028697761100887,
    "brier_score": 0.670495801984243,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.7236723672367236,
    "away_rate": 1.342034203420342,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.10923061964816949,
    "model_minus_baseline_brier_score": -0.07916560919303695,
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

- 2026：返回306场，抓取时间2026-10-08T01:33:56.529011Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
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
    "log_loss": 0.9965798294286146,
    "brier_score": 0.5974498325364818,
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
    "model_minus_baseline_log_loss": -0.08985712056093509,
    "model_minus_baseline_brier_score": -0.060407124794804035,
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

- 2026：返回380场，抓取时间2026-10-08T01:33:58.101445Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-04T04:01:43.659093Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-22T00:00:00Z",
    "train_last_kickoff": "2026-03-21T19:00:00Z",
    "test_start": "2026-03-22T00:00:00Z",
    "test_end": "2026-10-03T21:30:00Z",
    "training_count": 827,
    "testing_count": 211,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1492184
    ]
  },
  "coverage": {
    "total": 211,
    "scored": 211,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 211,
    "log_loss": 1.0316081735372429,
    "brier_score": 0.6217132908288088,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 211,
    "log_loss": 1.059299177514172,
    "brier_score": 0.6390599904211136,
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
    "model_minus_baseline_log_loss": -0.02769100397692914,
    "model_minus_baseline_brier_score": -0.017346699592304837,
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

- 2027：返回380场，抓取时间2026-10-08T01:33:59.646771Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-05-09T00:00:00Z",
    "train_last_kickoff": "2026-05-06T10:00:00Z",
    "test_start": "2026-05-09T05:00:00Z",
    "test_end": "2026-09-20T08:00:00Z",
    "training_count": 529,
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
    "log_loss": 1.0202667445398814,
    "brier_score": 0.6094659391605005,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 131,
    "log_loss": 1.0774177336320996,
    "brier_score": 0.6528705420344714,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3342776203966005,
    "away_rate": 1.1170915958451368,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.05715098909221816,
    "model_minus_baseline_brier_score": -0.043404602873970854,
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

- 2026：返回198场，抓取时间2026-10-08T01:34:01.048848Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T07:08:07.346716Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-22T00:00:00Z",
    "train_last_kickoff": "2026-04-21T10:30:00Z",
    "test_start": "2026-04-22T10:30:00Z",
    "test_end": "2026-09-27T10:00:00Z",
    "training_count": 515,
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
    "log_loss": 1.0835530808425058,
    "brier_score": 0.6564374537592599,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 129,
    "log_loss": 1.1217305548410492,
    "brier_score": 0.6828928672699479,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.3840931134820562,
    "away_rate": 1.1668283220174587,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.03817747399854343,
    "model_minus_baseline_brier_score": -0.02645541351068803,
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

- 2026：返回510场，抓取时间2026-10-08T01:34:02.099205Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T07:09:05.348136Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-04-19T00:00:00Z",
    "train_last_kickoff": "2026-04-18T18:30:00Z",
    "test_start": "2026-04-19T00:30:00Z",
    "test_end": "2026-10-07T00:30:00Z",
    "training_count": 1168,
    "testing_count": 292,
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
    "total": 292,
    "scored": 292,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 292,
    "log_loss": 1.105748774409796,
    "brier_score": 0.6697298175413408,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 292,
    "log_loss": 1.0757427226848173,
    "brier_score": 0.6501000136091382,
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
    "model_minus_baseline_log_loss": 0.030006051724978722,
    "model_minus_baseline_brier_score": 0.019629803932202594,
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

- 2026：返回156场，抓取时间2026-10-08T01:34:03.620968Z；接口返回成功；完整性尚需核验。
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

## 墨西超Liga MX

- 2026：返回153场，抓取时间2026-10-08T01:34:05.733661Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T07:10:02.868065Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-22T00:00:00Z",
    "train_last_kickoff": "2026-03-21T03:06:00Z",
    "test_start": "2026-03-22T01:00:00Z",
    "test_end": "2026-09-28T03:10:00Z",
    "training_count": 610,
    "testing_count": 154,
    "result_availability_delay_hours": 4,
    "embargoed_count": 1,
    "embargoed_match_ids": [
      1492564
    ]
  },
  "coverage": {
    "total": 154,
    "scored": 144,
    "excluded": 10,
    "ratio": 0.935064935064935,
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
    "n": 144,
    "log_loss": 1.1056509260371459,
    "brier_score": 0.6672412106105097,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 144,
    "log_loss": 1.074173394095313,
    "brier_score": 0.650595954700968,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.6388206388206388,
    "away_rate": 1.1867321867321867,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": 0.03147753194183278,
    "model_minus_baseline_brier_score": 0.01664525590954169,
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

- 2026：返回462场，抓取时间2026-10-08T01:34:06.886766Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-06T01:31:29.416454Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-31T00:00:00Z",
    "train_last_kickoff": "2026-03-29T19:00:00Z",
    "test_start": "2026-03-31T17:00:00Z",
    "test_end": "2026-10-05T18:30:00Z",
    "training_count": 820,
    "testing_count": 204,
    "result_availability_delay_hours": 4,
    "embargoed_count": 0,
    "embargoed_match_ids": []
  },
  "coverage": {
    "total": 204,
    "scored": 175,
    "excluded": 29,
    "ratio": 0.8578431372549019,
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
      }
    ]
  },
  "model_metrics": {
    "n": 175,
    "log_loss": 1.015460715567593,
    "brier_score": 0.6084018091189838,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 175,
    "log_loss": 1.0637277305596862,
    "brier_score": 0.6430359085684186,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.4387568555758683,
    "away_rate": 1.1230956733698965,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.04826701499209318,
    "model_minus_baseline_brier_score": -0.03463409944943485,
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

- 2026：返回495场，抓取时间2026-10-08T01:34:08.111468Z；接口返回成功；完整性尚需核验。
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
  "evaluation_data_cutoff": "2026-10-07T01:32:56.771491Z",
  "split": {
    "method": "chronological_fixed_model_holdout",
    "grouped_by": "kickoff_utc_calendar_date",
    "train_cutoff": "2026-03-23T00:00:00Z",
    "train_last_kickoff": "2026-03-22T18:30:00Z",
    "test_start": "2026-03-23T01:15:00Z",
    "test_end": "2026-10-06T00:15:00Z",
    "training_count": 1044,
    "testing_count": 261,
    "result_availability_delay_hours": 4,
    "embargoed_count": 2,
    "embargoed_match_ids": [
      1491991,
      1491987
    ]
  },
  "coverage": {
    "total": 261,
    "scored": 261,
    "excluded": 0,
    "ratio": 1.0,
    "exclusions": []
  },
  "model_metrics": {
    "n": 261,
    "log_loss": 1.072054618405647,
    "brier_score": 0.6483420215462691,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline_metrics": {
    "n": 261,
    "log_loss": 1.0802308996205219,
    "brier_score": 0.6537402308693604,
    "brier_definition": "mean(sum_over_3_outcomes((probability - outcome_indicator)^2))",
    "log_loss_probability_floor": 1e-15
  },
  "baseline": {
    "name": "training_league_home_away_mean_poisson",
    "home_rate": 1.1474389660124462,
    "away_rate": 0.8401148875059837,
    "smoothing": "(sum_goals + 0.5) / (n_matches + 0.5)",
    "same_scored_matches_as_model": true
  },
  "comparison": {
    "model_minus_baseline_log_loss": -0.008176281214874903,
    "model_minus_baseline_brier_score": -0.005398209323091319,
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
