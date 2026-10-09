# F5-C容量与单场原件接入修复

本次解决共享状态超过400 MiB后新增影子记录和F5-C失败诊断受阻，以及单场接入包未读取已保存F5-C的问题。保留生产180天模型、数学公式、F5-C五档、180天／40成熟样本／48小时政策、原始预测、失败历史及正式确认未激活状态。

## 显式容量与旧状态

`football_model/storage_policy.py`集中设置：物理保存1 GiB，逻辑原件4 GiB，影子新增写入800 MiB，单文件仍60 MiB。容量是项目保护阈值，不是已查询的平台套餐限额。F4和F5-C共用完整状态估算及写锁，有界诊断可以使用业务预算之外的空间，最终仍受完整状态硬上限约束。

schema-1清单允许已发布的旧500 MiB／2 GiB与新1 GiB／4 GiB两组声明；实际树和恢复仍服从调用者当前硬上限。任意伪造声明、重复／穿越路径、错误长度／哈希、损坏gzip、凭据和过量逻辑原件仍拒绝。旧提交与旧manifest不改写。

这次扩容解除当前阻塞。完整日更仍采用原有无损gzip传输及全量恢复；新的F5-C单场读取可按哈希闭包选择恢复。外部持久冷库分片、全年真实增速和548天完整预算尚须独立部署／验收，不据本次扩容关闭长期容量门禁。

## 单场接入和原件交付

后台capture和score两者均尝试后生成`runtime/integration/f5c-index.json`，保留最近失败状态。索引仅用于定位，接入验证不可变原件闭包、同一比赛身份、同一完整生产预测行、真实时序、α=0原样一致、四档复算、冻结选择及已有独立结果评分。旧状态缺索引时可扫描真实原件。

当前单场命令：

已有评分从本轮不可变`score_runs`报告定位本场`score_id`，校验报告引用与评分原件，再读取对应独立结果；按需读取不要求下载其他比赛的评分blob。完整日更的加载机制不因这项单场优化改变。

```sh
python -m football_model.integration match --runtime runtime --config config.json \
  --league brazil_a --match-id MATCH_ID --state-commit FULL_STATE_SHA \
  --output NEW_PACKET.json --html NEW_REPORT.html --zip NEW_REPORT.zip
```

替换大写占位并使用真实固定状态。JSON包总有`f5c_comparison`；父预测失配、晚到、缺原件、未捕获、capacity延期和评分等待分别披露。HTML显示对照及完整读取记录；ZIP包含实际原字节证据闭包与逐文件SHA-256清单。它不自动获得赔率、计算盘口EV、产生第二份GPT判断或证明远端赛前持久保存。

历史原件复核：

```sh
python -m football_model.f5c_access audit --runtime SELECTED_RUNTIME \
  --state-tree FIXED_TRANSPORT_TREE --league brazil_a --match-id MATCH_ID \
  --forecast-id ACTUAL_FORECAST_SHA --output NEW_AUDIT.json \
  --html NEW_AUDIT.html --zip NEW_AUDIT.zip
```

只取得本场所需归档blob亦可恢复校验；完整manifest必须来自同一固定提交，不可伪造精简清单。此入口标为历史复核，没有历史`--as-of`，不新增赛前资格或正式样本。已有score原件按独立结果和原概率重算；未保存独立score不能补造为后台完成。

## 发布与验收

integration、shadow_models、cloud_state及存储／读取模块的纯维护发布改走独立F5-C工作流，移出自动训练的push路径。原生产模型／数据／新联赛训练路径及北京时间09:17日程保留。独立路线不提供API密钥给派生／评分／索引，不训练不采集。

合并后核实际工作流outcome、原报告、保存提交及压缩原件回读；不能只看绿色step或本地测试。三场历史修复只追加对照附件，不覆盖原报告，不追认当时聊天已纳入F5-C。工程通过和预测效果继续分别报告。
