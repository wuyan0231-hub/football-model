# S2 只读容量预算与长期门禁

`scripts/s2_capacity_budget.py` 只读取已保存完整状态快照及可选的前后压缩清单，输出 JSON。它不训练、不调用足球 API、不改状态、不删除原件。清单比对会拒绝旧路径丢失或变化；相同压缩 blob 按一次新增计算，已存在 blob 的新引用单独登记。

```bash
python scripts/s2_capacity_budget.py \
  --before /absolute/snapshot_before.json \
  --after /absolute/snapshot_after.json \
  --opportunities 185 \
  --goal-opportunities 9000 \
  --goal-days 365 \
  --manifest-before /absolute/state_storage_before.json \
  --manifest-after /absolute/state_storage_after.json
```

快照必须带 `scope=full_fixed_saved_tree`、带时区的 `saved_at`、`physical_bytes` 与 `logical_bytes`。必须额外提供 `estimated_writer_persisted_bytes`，才能检验影子写入器的 400 MiB 保守门槛；已保存实际大小不能替代这项保守估算。物理硬上限 500 MiB，逻辑硬上限 2 GiB。快照排除 Git 历史及克隆开销，因此这些开销另行监测。

F5-C 准备协议 v2 的实际配置是：至少入组 10,000 个独立合法机会且至少经过 365 天，548 天硬截止。10,000 是最低计划目标，不是入组上限；365 天前达到目标后仍持续登记完整范围，关闭检查点也保留超额对象。因此下列调用只计算“10,000 最低目标”和“548 天假定强度”的压力情景，不能证明整个真实协议有足够容量，结果仍保留长期门禁未知/未通过。

```bash
python scripts/s2_capacity_budget.py \
  --before /absolute/snapshot_before.json \
  --after /absolute/snapshot_after.json \
  --opportunities 185 \
  --goal-opportunities 10000 \
  --goal-days 548 \
  --manifest-before /absolute/state_storage_before.json \
  --manifest-after /absolute/state_storage_after.json
```

v2 另有入组结束后的 14 天结果及修订收尾期；它没有新入组，但仍有结果保存开销。548 天情景不包含这段额外写入，正式容量验收必须另覆盖收尾期。

`opportunities` 是输入批次中独立比赛机会数，非版本数或 API 查询数。9000 是接续计划的情景参数，不是有效性或盈利门槛。工具分别给出每个机会生成一次与七次记录的线性压力情景，以及假定每日 0、25、185 个等效版本的 365 天情景。这些条件计算均排除日常生产、赛果修订、选参池增长及将来对象大小变化，不是预计每天增速，也不是可靠上下界。

通过 `--daily-snapshot` 可追加实际每日完整快照。仅标记 `daily_observation=true` 的独立日期参与统计；少于 14 个日期、13 个相邻间隔时，不输出“已识别每日增速”。即使有观察序列，区间净增量也不能证明未来最高增速。

三个门禁分开读取：当前保存内容、条件为下一次同大小操作，以及长期运行。前两项通过只能说明短期边界。长期门禁现始终保持未通过，原因是外部持久归档与按需读取尚未部署、全年负载未验证。离线分片恢复成功或存在归档方案不能替代云端写入、固定定位回读及实际全流程验收。

每次正常每日流程保存后，追加一份完整快照；同时记录新比赛数、重复预测版本数、报价对象数、赛果版本数、新增选择池对象、原/压缩字节增量和异常延期。先连续观察至少 14 日，再基于更繁忙赛事周做压力验收；这 14 日是容量诊断窗口，不是预测质量门槛。

已有原件必须先无损归档并独立恢复核对，随后才能设计生产的热状态与冷状态读取。当前生产加载仍会恢复全部逻辑状态，单纯多切几个文件不会解决 2 GiB 逻辑上限。长期方案必须增加固定提交定位的冷对象目录、按需回读及引用闭包验收；完成真实持久保存和恢复前不能从热状态移走唯一副本。

本轮提供独立离线工具 `scripts/s2_lossless_shards.py`。`create SOURCE NEW_DEST` 将原字节逐件 gzip 压缩并按字节上限分片，默认每片 4 MiB；清单保留原路径、原哈希、原长度、分片哈希和偏移。`restore SHARDS NEW_DEST` 先验证全部分片与原件，再创建新目录恢复；损坏、缺片、越界路径、清单校验失败或目标已有内容时拒绝。压缩相同原件只存一次，路径映射仍全部保留。

```bash
python scripts/s2_lossless_shards.py create /absolute/frozen_originals /absolute/new_shards
python scripts/s2_lossless_shards.py restore /absolute/new_shards /absolute/new_restoration
```

该分片格式不被现行 `state_storage` / `cloud_state` 自动读取，不能放进生产状态冒充上线。它用于迁移开发和恢复证据检验，原目录不改动。
