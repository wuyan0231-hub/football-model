# S2 分层运行摘要

状态：已实现离线代码与回归测试；实际部署、下一次 Actions 运行和 model-state 保存须另验。入口是 `python -m football_model.run_summary`，不改变模型、前瞻准入、API 额度或定量参数。

## 分层口径

| 层次 | 读取和计数方式 | 不得混用 |
|---|---|---|
| 本轮模型支持 | 本轮 `reports/latest.json` 中逐赛事 `forecast_availability`；缺计数字段保留 unknown，同时另列已知支持数 | CLI 退出 0 不等于每场可用，更不等于准确率验收 |
| 本轮前瞻接受／拒绝 | 对 `prospective/coverage` 完整性校验，按本轮 run_id 和报告原字节来源指纹筛选；接受项另核 `forecasts` 对象 | 不用累计数相减；部分执行仅表示已观测记录，不代称完整目标集 |
| 累计预测与成熟结果 | 本轮生成的前瞻派生报告中，版本数、协议内去重比赛、跨协议独立比赛、协议内已评分／待赛果分别列示 | 多版本不算多场；多协议样本不直接当独立比赛 |
| 基本面完整／部分／正常缺失 | 本轮采集报告中的每场状态和白名单缺失原因；例如窗口未开、赛事不覆盖与保存失败分开 | 完整指所请求采集操作完成，不保证所有特征字段完整；不作为比分模型概率门禁 |
| 输出检查／真实保存 | 直接使用 `steps.check.outcome`、`steps.persist.outcome` | 不使用 `continue-on-error` 处理后的绿色 conclusion |
| 必要步骤结果 | 恢复、CLI、录制检查、输出检查、保存；train 模式还包括前瞻 | 汇总前尚未完成附件上传和最终门禁，故不冒称最终 GitHub job conclusion |

## 如何证明是本轮

工作流在恢复状态前建立独立临时 context，在 model、prospective 和 fundamentals 实际执行前分别记录真实阶段开始时间。模型报告须匹配源码 SHA、模式、赛事范围、实际 run_started_at 派生的 run_id，且开始和生成时间均在本次阶段内。前瞻派生报告须匹配模型 run_id；coverage 须同时匹配报告快照指纹和当前阶段时间。基本面报告还逐场检查 `forecast_run_id`。

旧 latest、缺少 marker、错运行关联、未来时刻、对象哈希不符、重复比赛覆盖对象均不展示为本轮成功，数值保持 unknown。即使 CLI 或前瞻步骤失败，已经生成且关联成立的局部结果仍可显示。不存在以缺文件推算零接受／零拒绝的回退。

## 安全和保存

摘要仅输出固定字段、白名单状态／原因、经过校验的计数和时间。不会复制原报告自由文本、异常消息、环境全量变量或认证信息；未知原因显示 `unclassified_reason`。旧 `model_fitted_at` 缺失异常按精确匹配映射为固定原因码。

保留各生产步骤的 `continue-on-error`，让输出检查和保存能处理局部成果；既有最终失败门禁保留，摘要失败也不得显示完整成功。基本面自身失败继续单独报告，不新增概率门禁。

摘要必须在 `persist` 之后生成，才能报告已观察到的真实保存 outcome，因此摘要 JSON 放在 `$RUNNER_TEMP/football-run-summary.json`，由独立的 `football-run-summary-<run_id>-<attempt>` 便捷附件上传，并写入 Actions 运行摘要。原 `football-report` 附件路径与 ZIP 根布局保持不变。摘要不属于同次 model-state 保存内容，也不触发第二次状态提交。只有 `persist.outcome == success` 才说明此前已通过检查的运行状态成功保存。

## 验证与边界

`python -m unittest discover -s tests -p test_s2_run_summary.py -v` 覆盖：CLI 成功＋前瞻失败＋保存成功时，模型支持仍显示、必要步骤结果失败；旧 latest 拦截；本轮／累计分开；缺件不是零；基本面三类分开；错误消息脱敏；重复／损坏 coverage 拦截；协议间去重及 audit 跳过场景。

新摘要是运行解释与证据关联修复，不是预测准确率或收益增量证明。完整真实运行验收需要工作流部署后的新产物；对既有 artifact 的复验仅作历史证据重述。
