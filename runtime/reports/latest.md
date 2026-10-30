# 足球模型运行报告

生成时间：2026-10-30T07:20:29.251959Z（UTC）
程序版本：0.1.0；运行模式：train。

本程序生成探索性历史比分基线。没有球员/伤停、xG、战术、赛程特征，也没有经过验证的收益优势。
数据来源 API-Football；基本面训练不读取赔率或供应商预测。欧冠只做数据审计。

|赛事|运行状态|预测可用性|可用/不支持/未来场次|可用90分钟赛果|训练参数变化|说明|
|---|---|---|---|---:|---|---|
|英超|未完成|failed|None/None/None|380|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|英冠|未完成|failed|None/None/None|557|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|西甲|未完成|failed|None/None/None|380|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|德甲|未完成|failed|None/None/None|308|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|意甲|未完成|failed|None/None/None|380|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|法甲|未完成|failed|None/None/None|308|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|德乙|未完成|failed|None/None/None|308|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|法乙|未完成|failed|None/None/None|310|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|欧冠|未完成|failed|None/None/None|279|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|瑞典超|未完成|failed|None/None/None|242|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|荷甲|未完成|failed|None/None/None|321|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|葡超|未完成|failed|None/None/None|308|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|巴西甲|未完成|failed|None/None/None|380|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|日职联J1|未完成|failed|None/None/None|0|未训练|这些已请求赛季未完整取得：[2027, 2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|韩国K1|未完成|failed|None/None/None|232|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|美职联MLS|未完成|failed|None/None/None|522|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|墨西超Liga MX|未完成|failed|None/None/None|340|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|西乙|未完成|failed|None/None/None|468|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|
|阿根廷甲级联赛|未完成|failed|None/None/None|378|未训练|这些已请求赛季未完整取得：[2026, 2025]。本次不训练；核对套餐或明确缩短训练窗口。|

预测可用性：available=全部已保存可用；partial=部分可用；unavailable=有赛程但全部不支持；no_upcoming=窗口内无待预测比赛；failed=处理失败。以上不表示准确性或收益验收。

本次接口网络尝试次数：73（含状态查询与重试，不等于当日计费已用量）。

## API额度与限速

|项目|本次记录|
|---|---|
|配置的每日上限|7460|
|供应商日上限／当日已用（最近观测）|100／28|
|供应商最近回报余额|72|
|当前有效日余额（保守估计）|70|
|日额证据状态|known|
|每轮网络次数上限|不设额外次数上限|
|供应商分钟上限／最近剩余|10／7|
|当前请求最小间隔（秒）|6.01|
|网络请求软截止／已运行（秒）|3600／465.57626020400005|
|额度最近观测时间／来源|2026-10-30T07:20:14.584699Z／response_headers|

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

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回380场，抓取时间2026-10-30T07:13:07.868905Z；接口返回成功；完整性尚需核验。
## 英冠

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回557场，抓取时间2026-10-25T06:13:58.565482Z；接口返回成功；完整性尚需核验。
## 西甲

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回380场，抓取时间2026-10-30T07:13:49.947311Z；接口返回成功；完整性尚需核验。
## 德甲

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回308场，抓取时间2026-10-30T07:14:13.979532Z；接口返回成功；完整性尚需核验。
## 意甲

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回380场，抓取时间2026-10-30T07:14:38.017680Z；接口返回成功；完整性尚需核验。
## 法甲

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回308场，抓取时间2026-10-30T07:15:02.062698Z；接口返回成功；完整性尚需核验。
## 德乙

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回308场，抓取时间2026-10-30T07:15:26.100442Z；接口返回成功；完整性尚需核验。
## 法乙

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回310场，抓取时间2026-10-30T07:15:50.138664Z；接口返回成功；完整性尚需核验。
## 欧冠

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回279场，抓取时间2026-10-30T07:16:14.176919Z；接口返回成功；完整性尚需核验。
## 瑞典超

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回242场，抓取时间2026-10-30T07:16:38.223158Z；接口返回成功；完整性尚需核验。
## 荷甲

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回321场，抓取时间2026-10-30T07:17:02.257626Z；接口返回成功；完整性尚需核验。
## 葡超

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回308场，抓取时间2026-10-30T07:17:26.300709Z；接口返回成功；完整性尚需核验。
## 巴西甲

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回380场，抓取时间2026-10-30T07:17:50.349079Z；接口返回成功；完整性尚需核验。
## 日职联J1

- 2027：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
## 韩国K1

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回232场，抓取时间2026-10-30T07:19:08.491297Z；接口返回成功；完整性尚需核验。
## 美职联MLS

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回526场，抓取时间2026-10-30T07:19:32.739837Z；接口返回成功；完整性尚需核验。
## 墨西超Liga MX

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回340场，抓取时间2026-10-26T06:26:20.556016Z；接口返回成功；完整性尚需核验。
## 西乙

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回468场，抓取时间2026-10-26T06:26:44.589359Z；接口返回成功；完整性尚需核验。
## 阿根廷甲级联赛

- 2026：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2025：返回0场，抓取时间无；API-Football 返回错误（可能涉及套餐、赛季或额度）：{"plan": "Free plans do not have access to this season, try from 2022 to 2024."}。
- 2024：返回378场，抓取时间2026-10-26T06:27:08.641081Z；接口返回成功；完整性尚需核验。