# 足球模型自动训练入门包 v0.1

本包是可运行的基础程序源码，不附带任何真实球队参数。你的 GitHub 已连接，私人仓库 [wuyan0231-hub/football-model](https://github.com/wuyan0231-hub/football-model) 已创建，代码正在部署，尚未确认全部上传成功。尚未进行真实API验收、真实球队训练或启用定时任务。合成数据仅用于检查程序。

你不需要编辑球队评分。账户接入后，程序取得实际赛果并拟合参数；没有有效数据、球队不认识或训练失败时会明确报错。

## 能做什么

- 配置 15 类赛事，自动查询供应商真实赛事 ID、赛季与覆盖标志。
- 对 14 个国内联赛运行时间衰减、正则化攻防泊松**基线**；半衰期180天等为尚未调优的默认配置。
- 按时间分出后续比赛做初步验证，对照联赛平均进球基准，报告概率评分及未覆盖球队。
- 参数只在取得新赛果、历史记录被修正或模型实现/配置改变后重新拟合；不因重复启动而谎称“学到新知识”。
- 生成未来7天赛程的探索性比分分布与胜平负概率，保留预测截点、模型版本和历史结果。
- GitHub Actions 可按日运行，状态保存在独立 `model-state` 分支，源代码分支保持独立。

## 尚不能做什么

- 欧冠当前**仅做数据审计**。跨联赛实力换算和欧冠模型未实现，不能把各国内联赛参数直接拼接。
- 尚未实现阵容/伤停、球员、xG、赛程疲劳、战术和滚球模型。相应影响仍然未量化。
- 未实现自动识别中立场与全部特殊赛制。预测按名义主客场生成，实际使用前必须核实。
- 不处理加时/点球结果；本版仅接收状态为FT的明确90分钟比分，AET/PEN不纳入。特殊赛季、附加赛可能因此缺样本。
- 模型验证是一次固定模型的时间留出检查，**不是完整滚动验证、独立前瞻验收或盈利证明**。
- 不提供下注功能、赔率抓取或收益保证；没有本场有效报价，因此EV写“未量化”。

这是 v1.5 框架 §20 的基础建设阶段，不能称已完整实现框架的所有模型。后续应增加滚动验证、校准及阵容模块，并分别验证每个赛事。

## 最少需要你操作的部分

1. **已完成：GitHub 账户连接及私人仓库创建。** 程序正在部署到 [football-model](https://github.com/wuyan0231-hub/football-model)，不需要你再次创建仓库。后续如需登录或授权，由你在网站安全界面完成。
2. **待确认：API-Football 账户。** 如果尚未注册，创建 [API-Football 免费账户](https://dashboard.api-football.com/register)。先用免费方案检查，不先购买。
3. 在 API-Football 的 Account → My Access 获取密钥，不贴进聊天。
4. 在私人 GitHub 仓库的 Settings → Secrets and variables → Actions → Secrets → New repository secret 添加：名称 `FOOTBALL_API_KEY`，值为你的真实密钥。
5. 等程序上传成功且密钥配置完成后，从 Actions 运行“足球模型：数据检查与自动训练”，先选 `mode=audit`、`league=epl`。这次真实API检查目前尚未执行。
6. 打开运行页面的中文报告。若过去赛季因免费权限缺失，先检查具体原因，可明确缩短 `config.json` 的 `history_seasons` 再试，不要把下载失败当作完整训练。
7. 数据合格后，选 `mode=train`，先 `league=epl`；首次真实拟合与验证检查完成后，再 `league=all`。
8. 首轮运行通过后，设置仓库 Variables 中的 `ENABLE_SCHEDULE` 为 `true`。默认日程为UTC 01:17，即北京时间09:17；GitHub日程可能延迟，不能用于秒级临场刷新。

私人仓库创建已完成；第5步之后的按钮仍依赖工作流代码成功上传，不能把仓库创建视为程序部署或训练已经完成。

日常使用只需查看报告；异常时会明确显示未完成。暂停日程时把 `ENABLE_SCHEDULE` 改为 `false`。本包提供定时配置，下载本包不会创建或启用任何云端任务。

## 结果在哪里

|位置|内容|
|---|---|
|Actions运行摘要|中文运行状态、赛季取得情况、初步验证指标|
|`runtime/reports/latest.md`、同名JSON|最新一次运行报告|
|`runtime/reports/history/`|每一次运行的不可回写报告|
|`runtime/models/`|各赛事最近一次成功拟合的模型|
|`runtime/model_history/`|旧版参数，不用新参数覆写旧预测|
|`runtime/validation/`|时间留出预测、评分与评估模型|
|`runtime/predictions/运行时间/`|该次产生的未来比赛条件基线|
|`runtime/data/`|已清洗的实际赛果，记录缺失赛季|
|`runtime/cache/`|不含密钥的数据响应；仍须按许可仅供你的项目使用|

云端状态在私人仓库的 `model-state` 分支保存；默认源代码分支看不到这些运行文件。Actions 附件只是方便下载，不能替代状态分支。若写入权限不足或分支产生并发冲突，任务会显示失败；有附件不代表状态已成功持久化。不要把数据分支公开。

将具体比赛的预测JSON和赔率截图放入你的分析项目，要求按框架核查数据时间及未量化因素。项目不会因为收到本包或框架就自动读取最新模型；自动接入需另行配置。

## 解释“自动训练”

自动任务下载新增赛果、检查数据、更新受影响模型并保存日志。人员突然受伤不会自动改变历史比分；本包没有阵容模块，不能宣称已经调整该伤停。自动执行不等于程序自动发明新模型或保证越训练越准。

数据和配置未变时沿用原参数。距上次拟合超过14天时，本版停止发布新预测，保留旧文件并显示模型陈旧；赛季间歇后需要重新评估适用性。这个14天是保守操作默认值，不是统计学保证。

若某赛事数据失败，其他赛事可以继续，报告仍会列出失败项，任务返回非零状态。失败赛事不生成新的预测。日程启动不等于每项成功，更不等于准确性已验收。

## 本地可选用法

云端用户不用安装Python。需要本地排错时，可在解压后的目录中使用Python 3.12：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m football_model.cli --mode audit --league epl
python -m football_model.cli --mode train --league all
```

后两条命令需要提前在安全的环境变量中设置 `FOOTBALL_API_KEY`。本说明故意不提供含真实密钥的命令。未设置时程序产生中文错误报告，不下载数据、不生成假球队参数。

## 数据和验证边界

- API赛事目录的覆盖标志不保证每场齐全。程序先查询名称和国家，未精确匹配就停止，不猜赛事ID。
- 账户免费额度和历史权限可能不足。单次最多80个网络请求，包含重试，默认请求间隔6.5秒，并遵守接口返回的额度信息。
- 默认检查最近3个已开始的赛季。任何选中赛季读取失败，本次不训练该赛事；其他赛事继续。
- 当前赛季每次重新查询，旧赛季最多缓存7天。报告保留原始抓取时间，不给缓存换上新的下载时间。
- 只接受FT的 `score.fulltime` 且开赛至少4小时后才可训练；这是一项保守时间缓冲，不是所有比赛完赛时间的事实。
- 时间留出按UTC日期分组防同日泄漏。历史接口可能事后修订赛果，因此并未证明严格还原当时供应商信息；从首次运行开始保存前瞻资料。
- 当前特征中没有供应商赔率、供应商预测和赛后球员数据。不把市场推导的概率称独立基本面预测。
- 调整过超参数后反复查看相同留出时段，不构成新的独立测试。默认值只是起点，不能据一次分数好看直接认定收益优势。
- 真实历史数据规模、首次联网兼容性、云端权限、日程运行和真实预测能力仍需你的账户接入后验收。

## 来源与费用

- [API-Football 官方入门说明](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide)
- [赛事覆盖](https://www.api-football.com/coverage)、[方案和额度](https://www.api-football.com/pricing)、[使用条款](https://www.api-football.com/terms)
- [GitHub Actions保密设置](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
- [GitHub定时运行条件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

数据服务和GitHub运行/存储可能有费用，按账户实际套餐与使用量核对。本包不购买套餐、不配置支付、不授权自动超额付费。
