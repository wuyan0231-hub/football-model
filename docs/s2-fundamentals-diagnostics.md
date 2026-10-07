# S2 基本面失败诊断与窄化准入修复

核验日期：北京时间 2026-10-07。依据是修改交接包固定源码及本轮以固定状态提交 `ad826fa62b7d0675d42f3f2e0ffbea62d7130d2e` 取得的原缓存。本模块只采集、规范化和诊断，`fundamentals_used_in_probabilities` 始终为 `false`，不改 λ、赔率价值或生产模型参数。

## 已核实的真实根因

兼容审查只能确认两场历史首发失败；本轮取得真实缓存后，已经把根因查明。使用交接包中的旧 `fundamentals.py` 逐份调用 `make_snapshot`，两场均报 `DataError: player.id 必须为不小于 1 的整数`。两个队的首发均是完整 11 人，异常来自替补席第 7 项的 `player.id=null`。

| 比赛 | 受影响队 ID | 原缓存 Git blob | 缓存原字节 SHA-256 |
|---|---:|---|---|
| 1492346 | 1062 | `f706c773c7c65ff0f85804b885d80bf501afb792` | `e732284ae312baa9c14601d76fdd79e22413ecf8fbadfff380713047e181f12f` |
| 1492363 | 147 | `05b5af059c5bacf8385a4b0b44ffb0b244c85b9f` | `91ff2c004cab78a2c55c90405f2b887cc7147dbc6352fddc4c6526635232ed56` |

缓存路径分别为：

- `runtime/cache/api_cache/2d819c8bb306f25ce1c9903f89f183b83e7de6b87d1599a3540fd7631a4d0f32.json`
- `runtime/cache/api_cache/b5691b4cf3210d730c42c0e5a9f7420ad5a1452a03ca86af1d73f06797077be1.json`

修复仅允许**替补**的缺失 ID 保留 `null`，附 `identity_status=provider_id_missing`。不合并两名身份未知者，不按姓名补造 ID。完整首发及其 ID 仍可进入已有的定性阵容连续性特征；缺失首发 ID、非法 ID 0/bool、已知球员重复、首发替补重叠仍拒绝。球队身份与原时间资格校验不放宽。

修后两份完整原响应均通过 `make_snapshot` 和 `save_snapshot` 的重建校验，双方完整首发为 true，各保留 1 名未知 ID 替补，产生 `unidentified_substitute` 质量提示。旧失败记录没有回写；这是当前工程复验，不是补造历史赛前成功。

## 失败按阶段留下证据

`collect_run` 在每场记录 `diagnostics`，将失败投影按内容地址写入 `runtime/fundamentals/failures/<match_id>/<sha256>.json`。阶段覆盖来源请求、规范化、快照保存、赛程验证/保存、元数据、历史文件、特征构建/保存。每条包含 `stage/category/code/endpoint/match_id/checked_at/payload_sha256`，保存成功时有 `diagnostic_path`；保存本身失败时明确 `diagnostic_save_failed`，不伪称原件已保存。

失败分类：`budget`、`provider`、`validation`、`storage`。常见业务失败另有固定 code，如 `invalid_player_id`、`duplicate_starter`、`team_identity_mismatch`。未知异常只记固定通用 code，任意异常文本、请求头、认证值、账户对象不进入诊断报告。

失败文件只保留足球字段白名单。非必要自由文本转为单向指纹；未知字段只留存在标记。`payload_sha256` 的范围明确为 **whitelist_projection_canonical_json**，`source_http_bytes_preserved=false`。它不能冒充 HTTP 原字节哈希或证明完整传输原件留存。

`replay_verified=true` 只在投影实际再次通过 `make_snapshot` 触发同一已列明业务错误时设置。两次仅同为 DataError 或落在同一通用类别不足以证明重现。抓取失败无响应、磁盘失败、未列明错误或投影改变了触发条件，均保持 false。

独立审查后补充分层时间记录：`collection_started_at` 是本次采集及窗口检查开始，`response_observed_at` 保留响应自身的真实 `fetched_at`，`failure_recorded_at/checked_at` 在捕获异常后调用真实时钟取得。`collect_run` 传入同一真实运行时钟；独立调用默认 UTC 实时时钟，也允许测试显式注入。输入上下文时间和响应时间不因失败记录更新而覆盖，禁止用开始时间或 `max(开始时间,响应时间)` 伪造结束时间。延迟返回回归使用开始后第 15 秒响应、第 16 秒记录失败，三类时间均按实际测试事件分别保存。

报告中的新错误原因包括 `invalid_evidence`、`evidence_fetch_failed`、`snapshot_save_failed`、`diagnostic_save_failed`、`collection_dispatch_failed`。`provider_error`、`budget_deferred` 等既有原因继续保留。已保存的其他数据不因一项后续失败而丢弃。

正常 `lineup_window_not_open`、覆盖 false/unknown、非 90 分钟过程统计隔离仍是预期缺项，不变成整个采集失败。质量提示另列 `quality_diagnostics`，不充当比分概率的批准或拒绝条件。

## S1 真实射门矛盾

对 S1 `API/live_results/responses/009_fixtures_statistics.json`（SHA-256 `c2ee408fb9ced5c5a25c91390cd8b342eda176c03b8f1b31a57c5e6a4d299b49`）实际运行 `statistics_diagnostics`，比赛 1493165 的 456 队有三项矛盾：

| 比较 | 原数值 |
|---|---|
| 总射门与禁区内外 | 16 对 9+6=15 |
| 全场与两半场总射门 | 16 对 4+11=15 |
| 全场与两半场被封堵射门 | 2 对 0+1=1 |

全部原值保持不变，标记 `shot_statistics_inconsistent`，动作是隔离未来依赖射门的派生变量。只比较完整、唯一、有限非负的数值项，不将 null/缺字段/重复字段当 0，不把百分比或 xG 做简单半场闭合。当前模型并未消费这些射门字段；告警既不证明 xG 错，也不自动删除其他已核实字段。正常生产请求目前不保证带有半场统计，因此半场检查仅在响应确实提供相关字段时进行，未检查不等于一致。

## 验证与剩余边界

相关回归命令：

```bash
PYTHONPATH=.:tests python -m unittest tests.test_fundamentals tests.test_fundamentals_collection tests.test_fundamentals_client tests.test_s2_fundamentals_diagnostics -q
```

首轮相关 46 项通过，包含 12 项新增：真实替补缺 ID 的精简回归、身份门禁、失败投影脱敏与复验、同类型不同错误不冒称重现、供应商/存储故障隔离、正常首发窗口、S1 射门矛盾与缺失值保护。独立审查时间修复后又单独运行 1 项新增延迟返回回归并通过；最终整合回归以本轮统一测试记录为准。另外进行了上述两份完整真实缓存的旧/新函数对照与保存核验，不把合成错误测试称为线上效果。

尚未完成的事项：球员出场分钟端点/分钟 null 与 0 的语义清洗、全赛事负荷、统计连续覆盖验收、真实持续采集的诊断累积、特征的时序训练与样本外增量。此次修复的 **球员 ID 为 null** 与 S1 的 **出场分钟为 null/0** 是不同问题，后者不能据此标记已解决。所有更新仍需固定提交及实际部署/运行另行验收；没有宣称准确率提高。
