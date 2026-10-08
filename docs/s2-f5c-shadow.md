# S2 F5-C：每日生产预测总球收缩影子试运行

本模块只在真实已录制的生产180天预测上派生总球收缩候选，**不重新拟合、没有API访问、不替换正式模型、不生成投注收益结论**。入口为`python -m football_model.total_shrink_shadow capture|score`。与原F4的90/180/365天影子根目录完全分开。

## 与F5历史研究的关系

候选公式保持`T′=(1−α)T+αm`，保留原主客强度比例；α固定0、0.25、0.5、0.75、1。α=0直接完整复用生产原预测，不用复算值覆盖原件。m使用原生产训练指纹绑定的未加权主客进球均值：各侧`(总进球+0.5)/(比赛数+0.5)`；中立场两侧取平均，总和不变。m不是生产模型的intercept或home_advantage，也不是今日追加的数据样本均值。

本实现版本`s2-f5c-daily-total-shrink-pilot-1.0`明确区别于F5历史v1：基线从每周离线重拟合改为每日真实已录制生产预测；选参成熟时间从历史假设的开赛+48小时改为首次独立结果实际观测且保存后+48小时。5802场已知结果的开发数据、F4原185场对照都不补入本池。初轮冷启动α=0属于预声明行为，不能称候选改善或确认验证已启动。

## 真实时间与记录链

`input_cutoff`是本次候选输入真实冻结时刻。它与原生产模型`training_cutoff`是不同时间；允许合法生产预测在训练截止之后生成并录制，但它必须在本候选输入截止之前已经存在。记录真实生产forecast父ID、原预测原点、训练截止、模型拟合、生成、资料观测及生产录制时间。不能提供历史`--as-of`。

捕获从当前`integration/index.json`逐赛事定位真实成功run，并由现有生产源门禁阻断`blocked_latest_failure`。请求全集是这一run的完整预测行；不支持、已开赛、窗口外、缺原生产录制、原件损坏等各自保留coverage。已知较新的报价赛程响应若改期或不再NS，将拒绝本轮资格；原有旧版本仍保留。

完整production forecast及其prediction、run report、model、training data父件全部内容寻址保存。共享sources按原source ID及原字节SHA去重，production_parents只引用它们。模型用原`score_distribution`实际复算；联赛均值用原`prospective._baseline`从同训练指纹复算。data、schedule、report、index与生产父记录交叉绑定，欧国联另核冻结登记表、举办地区、neutral和证据时点。

候选完整五档payload先写入并`fsync`，再读取真实时钟。保存跨开赛的payload留下且被隔离，不能录成合法未来receipt。receipt所表示的是本次执行环境的落盘完成；`remote_persistence_status=pending_state_commit_verification`仍需另核真实state commit保存时间。文件本身不是远端持久保存证明。

## 选参

只使用此前真正录制的本版本同联赛历史配对。180天窗口按真实开赛时间；每场最多一个权重，选择与当时最新已可得结果身份一致的首个合法配对。读取真实独立fixture结果原件，绑定身份、常规比分、赛事、请求、原响应哈希和实际fetched_at。

结果的实际可用时间为`max(observed_at,captured_at)`。同身份首个合格独立结果的这个时间+48小时必须不晚于本次输入截止；所用最新修订版本自己的观测与保存也必须已经发生。开赛+48小时不能替代这一条件。历史资格和排除原因全部保存；选参引用冻结结果修订和原配对，后来结果修订不回写原α。

生产ridge与min_train亦属于冻结协议，改变配置另建协议；原父模型不符合这两个注册字段时拒绝，不用新配置暗中重训。满40场按未截断泊松总球Log loss最小选择α，1e−12内同损失选最小α；不足40场回退0。五档均保存使未来选参有真实同基线、同均值证据。欧国联选参稀疏启动、MLS自动力度不稳定仍是专项未解决问题，本版本不因这两个赛事修改原网格、窗口或门槛。

## 评分与完整性

每联赛每比赛按照最新独立赛果身份选首个合法配对，重复日报版本不算独立比赛。缺赛果保持pending；结果冲突、父件损坏等保留拒绝原因。修订结果与历史评分追加保存，原forecast和旧分数不覆盖。主要描述指标为候选减生产基线的总球Log loss；同时报告大2.5 Brier、1X2 Log loss/Brier、比分Log loss及argmax命中差。固定α全部为探索性辅助，不事后把最佳固定α当主策略。

原件内容哈希、父引用、身份、时间、参数、原α=0复用完全严格。只有native复算矩阵、1X2和尾质量的跨数值环境舍入允许绝对1e−15；预期进球及非派生元数据不放宽。实际Python、NumPy和SciPy环境随报告保存。合成测试只证明工程行为。

## 容量、操作与交付

共享F4写锁与同一原始逻辑/保守压缩持久容量预算；任一新父件、payload、receipt、coverage或报告超预算明确延期，原局部记录保留。容量报告仍受有界预留及完整状态上限约束，不越界强写。

```bash
python -m football_model.total_shrink_shadow capture --runtime runtime --config config.json --league all --source-commit ACTUAL_FULL_SOURCE_SHA
python -m football_model.total_shrink_shadow score --runtime runtime --source-commit ACTUAL_FULL_SOURCE_SHA
```

当前根`runtime/total_shrink_shadow`；捕获immutable在`runs/`，评分在`score_runs/`，派生当前视图`reports/latest.json`、`reports/score-latest.json`。命令0表示操作无全局/赛事源错误，1表示源/评分错误或capacity_deferred；每场请求coverage必须另核，0退出码不意味着请求全集都通过。当前视图不能替代内容寻址原报告。

本阶段全部`confirmation_eligible=false`，属于工程pilot。独立未来确认协议冻结、真远端state保存回执、样本资格登记与确认队列激活须另验；本轮记录不倒填为确认样本。不自动推广任何α，不改生产180天、框架1.7、赔率规范、预算或日程。
