# 模型读取：授权通道、固定版本与本地复算

版本：2026-10-16；配套完整框架 v1.7 修订批次13；有图调用项目唯一现行《赔率截图读取规范》，不绑定其版本。本页说明授权取件、固定活动／历史原件、校验落地和实际复算；本轮实现待云端验收，文件落地、历史读取或文档同步不等于调用、发布或效果通过。

## 为什么网页或 git 失败，连接器仍可能成功

普通网页、终端 git、已授权 GitHub 连接器是不同通道。网页工具的 `DisabledError` 只证明该调用未成功；git 退出128可能来自网络、代理、认证或其他原因，须看实际错误，不能统一写成认证失效。终端不会自动继承连接器的仓库授权，框架文件也不会给新对话授予权限。

**先发现并使用本会话已有的 GitHub 连接器。** 成功后无需为凑记录重复测试网页或 git；失败时检查其他当前可用、已授权且适合该仓库的通道。全部适用授权路径均不可用/失败后，才记录“本会话无法读取模型”并转定性。不能据此断言没有训练或赛事未覆盖。不要改公开仓库、索取/输出密钥、读取受限认证文件，或借修复访问权限重新训练。

## 分析者的固定读取次序

工具名会随会话变化，先从工具目录按 `GitHub`、`fetch`、`fetch_file` 发现并阅读当前参数说明；不要只找普通网页工具。当前可用工具的调用示意如下，操作均为只读：

```javascript
// 通过 GitHub 连接器发现两条分支当前提交；实际 SHA 从响应取得。
await tools.mcp__codex_apps__github_fetch({
  url: "https://api.github.com/repos/wuyan0231-hub/football-model/branches/main"
});
await tools.mcp__codex_apps__github_fetch({
  url: "https://api.github.com/repos/wuyan0231-hub/football-model/branches/model-state"
});
// 查看近期调度/推送等运行；按返回的工作流、联赛、时间与状态继续核 jobs。
await tools.mcp__codex_apps__github_fetch({
  url: "https://api.github.com/repos/wuyan0231-hub/football-model/actions/runs?per_page=30"
});
// sourceCommit/stateCommit 为实际返回的完整40位 SHA，不是字符串占位值。
await tools.mcp__codex_apps__github_fetch_file({
  repository_full_name: "wuyan0231-hub/football-model",
  ref: stateCommit,
  path: "runtime/integration/index.json",
  encoding: "base64"
});
```

1. **先核近期运行与失败范围。** 结合 Actions 事件、工作流、联赛和状态保存步骤，区分目标联赛、仅其他联赛及全局/索引失败；必要时翻页、读取相关 jobs/steps。`fetch_commit_workflow_runs` 当前仅返回 PR 触发且只有第一页，不能单独证明每日任务成功。记录所见运行 ID、检查时间、状态保存证据与仍未知范围。索引写入前的失败不会自动出现在旧索引中。
2. **固定一次源码和状态 SHA。** 分支读取用于发现；之后以 `source_commit` 读代码/配置/规则，以 `state_commit` 读索引及状态。全部使用完整 SHA，不在正式取文件时继续读浮动分支。同次获取中分支推进则保留本次固定快照，不悄悄混入新文件；需要更新时另建一次获取记录。
3. **原件优先 `fetch_file` 完整响应。** 指定 `encoding="base64"`，省略 `start_line/end_line`，取得完整原字节、Git blob SHA 和固定提交 URL。普通 `fetch` 适合元数据；返回文本摘要、截断内容或只读部分行不能伪装完整原件。保留完整 `structuredContent`，不要只复制屏幕展示的截断摘要。
4. **沿原索引取关联文件。** 本场索引、模型、预测、目标运行报告、完整验证及预测指向的场地快照都从同一 `state_commit` 取得；逐场验证未取得时如实记只有摘要。已知较新相关失败要纳入判断，不能只选择某个旧成功报告。每次预测的 `source_commit` 与本次复算代码提交分别保留。
5. **落地后才执行原单场命令。** 下述 helper 校验文件传输一致性；命令成功仍不等于本场通过接入。只有真实 `integration match` 返回及输出支持时才写已复算，随后另核赛程、伤停、赔率及具体盘口 EV。

### 最小可执行文件集合

| 来源 | 必取原件与选择规则 |
|---|---|
| `source_commit` | `AGENTS.md`、本页、`docs/model-integration.md`、当次完整框架/图规；`config.json`、`requirements.txt`；该提交 `football_model/` 下全部 Python 源码及其数据资源（当前包括 `__init__.py`、`cli.py`、`data.py`、`model.py`、`integration.py`、`nations.py`、`venue_registry.py`、`match_locations.py`、`coverage_audit.py`、`nations_venues.json`、`nations_venue_city_aliases.json`、`nations_match_locations.json`）。按固定提交目录实际内容核清，不能只下载 `integration.py`。helper 从同一源码提交取得。 |
| `state_commit` | 原 `runtime/integration/index.json`；目标联赛索引列出的模型、预测、对应最近尝试/训练运行报告和完整逐场验证；欧国联另取本条预测 `registry_snapshot` 指向的冻结文件。若原索引或较新已知记录引用相关失败，保留相应证据，不能漏掉后称全部通过。 |
| 当次执行 | 连接器原响应、访问尝试、Actions 核查记录、helper 清单、真实 match 命令/退出状态/输出。无需为只读复算下载历史 API 缓存或调用训练；新增依赖按实际 import/文件引用补齐。 |

`integration.py` 会经 `cli.py` 导入其他模块，只拿它和模型 JSON 不足以运行。已有运行环境须核依赖版本；缺依赖时依据同提交 `requirements.txt` 准备环境，无法准备则标“已读取、未复算”，不能复制结果填通过。保留原目录结构，不在单场/单联赛部分下载目录执行 `integration index` 重建“最新”索引。

## 把连接器原响应安全落地

首次还没有helper文件时，先按固定`source_commit`完整`fetch_file`读取`scripts/materialize_model_access.py`。通过工具的文件写入能力，或将base64严格解码为字节后安全写入临时源码目录，并以`sha1("blob "＋字节数＋NUL＋原字节)`核对响应的Git blob；取得文件后才执行。不得把响应内容直接拼进shell命令执行，`JSON.stringify()`不是shell转义。这个引导步骤也保存原响应和实际指纹，后续所需文件由helper统一校验。只有连接器而没有本地执行能力时，可如实记录已读取原件，模型状态仍为“已读取、未复算”，不手填调用通过。

输入为 UTF-8 JSON：`schema_version=1`、`repository`、完整 `source_commit/state_commit` 和 `files` 数组；每项 `role` 为 `source` 或 `state`，`path` 为仓库相对路径，`response` 为该文件的真实 `fetch_file.structuredContent`。下例只是结构示意，省略号不能执行、不能代替原响应：

```json
{
  "schema_version": 1,
  "repository": "wuyan0231-hub/football-model",
  "source_commit": "<实际源码40位SHA>",
  "state_commit": "<实际状态40位SHA>",
  "files": [
    {
      "role": "state",
      "path": "runtime/integration/index.json",
      "response": {
        "content": "<完整base64原响应>",
        "encoding": "base64",
        "sha": "<接口返回的Git blob SHA>",
        "display_url": "https://github.com/wuyan0231-hub/football-model/blob/<实际状态40位SHA>/runtime/integration/index.json",
        "display_title": "runtime/integration/index.json"
      }
    }
  ]
}
```

从已取得 helper 的源码目录运行，`NEW_DIR` 选择尚不存在的新目录：

```bash
python scripts/materialize_model_access.py --input connector-files.json --destination NEW_DIR
```

helper 不联网、不读取认证、不训练、不修模型；核对固定仓库/提交 URL、路径、完整字节与 Git blob 后按仓库路径写入新目录，并生成 `model_access_manifest.json`。清单保存实际落地时间及每文件字节数、SHA-256、Git blob，状态为 `files_materialized_not_model_verified`。指纹一致性不单独证明提交归属、近期任务成功或预测有效；提交归属还依赖真实授权读取响应和固定引用证据。保留这些原响应，不手填匹配哈希来替代调用。

随后从 `NEW_DIR` 执行原命令；占位值按实际目标替换：

```bash
python -m football_model.integration match \
  --runtime runtime --config config.json \
  --league unl --match-id <真实比赛ID> \
  --state-commit <实际状态40位SHA> \
  --output <本次复核JSON路径>
```

当前未来分析默认省略 `--as-of`；历史截点核验严格执行框架§3与§6，不能用过去时刻使已开赛比赛或后取得的材料伪装为赛前。不同结果分别记录：文件已落地、已读取未复算、调用失败、已复算；真实盘口计算再单列。helper 清单不替代 match 输出。

## 新状态中的无损历史归档

2026-10-08起，状态传输可使用`state_storage.json`清单与`state_archives/<压缩SHA256>.json.gz`。日更加载会在业务开始前按原路径、原字节完整还原；正常单场所需索引、模型、数据、预测、登记表及索引关联的当前验证仍保留直读。旧固定状态提交不改写，未归档文件继续按原入口读取。

若要复核的历史报价或影子父证据路径在新状态中未直存，先从**同一固定state_commit**完整读取`state_storage.json`，按条目的`path`精确定位（相对runtime，例如`shadow_models/forecasts/<真实SHA>.json`）。再按真实`blob_sha256`取`state_archives/<真实blob_sha256>.json.gz`，使用`encoding="base64"`取得完整原响应。不要因路径归档误判原证据丢失，也不能自行制作精简清单冒充远端清单。

原`materialize_model_access.py`现在接受这两个状态传输根路径，仍核固定状态提交URL、完整Git blob与原字节；源程序从同一固定source_commit取得。将原远端清单及所需压缩对象的响应按`role="state"`落入新目录后，运行：

```sh
python scripts/materialize_state_archive.py --state-root NEW_DIR \
  --path shadow_models/forecasts/ACTUAL_FORECAST_SHA.json \
  --output NEW_DIR/本次归档读取记录.json
```

`ACTUAL_FORECAST_SHA`必须换成清单中的实际逻辑路径；多个目标重复`--path`。入口校验清单结构与路径、60MiB单个原件限制、2GiB逻辑声明、单成员gzip、压缩SHA/大小、解压原SHA/大小及JSON；原文件已存在则拒绝覆盖。只取得目标对象时只能称“所选归档原字节已恢复”，不能称整个状态已恢复或模型已复算。随后仍执行原`integration match`或相应评分原函数；记录压缩对象Git blob和恢复原件SHA，二者不可混用。

## 访问记录与最后回退

每次在本场 `model_access.json` 或 `record.json.model_records[]` 的附属记录保存：

- `access_attempts[]`：通道/工具、真实带时区时刻、操作及仓库/引用/路径、成功/失败/不可用/未尝试、错误类别、脱敏错误摘要；未尝试说明不必尝试或通道不可用的依据。不得保存认证头、令牌、密码或含凭据 URL。
- 错误类别：`tool_disabled`、`tool_unavailable`、`network_or_proxy`、`authentication_required`、`permission_denied_or_not_found`、`integrity_failed`、`other`；只有具体响应支持时才认定认证原因。HTTP 404 可能是隐藏私有仓库或路径缺失，不自动确定其一。
- 固定源码/状态提交、实际取得文件/原响应与指纹清单、近期 Actions 检查及未知范围、未取得项和原因、实际复算状态/日志引用。访问记录只证明本次渠道状态，不能承诺所有新会话自动获得权限。

有合法已有离线包时可核原件、固定提交、清单与适用性继续复核，但必须标“离线快照”、实际生成/取得时间、时效和“最近 Actions 未核实”（如实适用时）。不能称最新线上状态；不能因此绕过目标联赛失败、陈旧、时间前视、场地及比赛资格门禁。最新失败范围无法排除时，不宣称当前数值接入已完整通过；保存已完成的快照检查并限定用途。

所有当前可用授权渠道均未取得必要原件时，定性完成可支持部分，数字写未量化，并说明确切缺口。只有已有授权确被认证/权限阻断，才通过受支持的连接界面恢复；不让用户逐场手工上传模型，不借普通网页失败索取新权限。

## 2026-10-10：F5-C选择恢复与旧清单

新代码同时验读旧500 MiB／2 GiB和新1 GiB／4 GiB的schema-1清单，实际大小仍受当前硬上限约束。F5-C单场可从固定完整manifest仅取得所需blob，按不可变引用恢复生产父来源、输入、协议、选择池、赛程／场地与评分闭包，再用原函数校验。完整日更继续全量无损恢复；选择读取不等于已部署长期外部冷库。详见[f5c-storage-access-repair.md](f5c-storage-access-repair.md)。


## 2026-10-13：特征候选的来源与读取

新候选读取另按`docs/feature-models.md`及框架6.6进行；这不把只读落地步骤变成训练。固定训练／预测源码提交、研究状态提交、候选卡、特征原件、训练数据及完整比分分布；goalmodel另固定公开源提交、源文件hash和真实R环境。只有实际命令与原件支持时才写已执行，不由包名或文档示例推断可运行。

普通单场生产复算仍走原`integration match`与F5-C读取闭包。特征影子原件独立取得，来源／父输入／时间不相符时拒绝该候选；不能用另一天研究λ拼接当前报价或当前生产行。完整原件、原响应、持久保存与返回状态如实保留；源版本校验只证明制品身份，不证明预测有效或已晋级。


## 2026-10-13：修订批次十的v2读取与旧v1保留

新特征研究使用feature-models-v2.json及research_design_version=feature-research-v2，research_artifact_namespace=protocol_hash_v2的模型与研究摘要按league／protocolhash读取，旧v1默认路径保留并继续后台研究、捕获和评分。先核固定源码提交中的完整协议与真实hash，再核每份模型卡、输入闭包、队列和评分绑定同一协议；研究／捕获／评分命令显式传--protocol，不由默认文件或latest推断当前设计。

G0_full、G0和G1按独立分支取得真实R输入／输出、训练ID、特征池排除、源文件与依赖指纹、逐场失败和完整矩阵；M1继续核固定offset、系数贡献与原基础λ。共同成功样本和含回退的完整机会台账都从实际原件恢复；只有合法冻结基线存在、原回退政策适用时才能把候选失败记作基线回退评分。缺基线、独立赛果或时点资格的记录保留未知，不补0或恢复赛前资格。

旧v1协议、状态目录、原模型卡、首失败／首合法捕获和评分保持不可变历史身份。新v2不能重命名旧文件、换hash或复制旧样本；复核旧v1需指定旧协议及对应固定状态。G0_full历史执行与未来捕获各自核回执，不能把历史已拟合当本场已运行。上述原件取得和验读不改变原生产integration match及F5-C闭包，也不代表新分支已晋级、已部署或效果改善。


## 2026-10-13：批次十一高级特征闭包

按`docs/advanced-models.md`取得独立高级协议、真实hash、逐联赛候选卡、研究报告与未来快照／评分／决定原件。每层引用均从同一固定状态恢复并核原字节；原基本面和赛程来源不只读取数值摘要。先核M0完整生产父行，再分别核高级分支的冻结来源、实际预处理／系数、矩阵与时间资格，不能将最新卡套入旧预测。goalmodel还需已运行R的训练输入／输出及固定公开源码；不执行R拟合的确定性读回不能被称为重新训练。

原件路径可能存储归档，按固定状态真实清单恢复，不能因未直读而断言后台未执行。高级研究可能为`advanced-compact-json-v1`分块与共享对象；须沿全部引用核hash并无损重建，不把外层包装当完整报告。嵌入统计／球员投影必须取其原`/fixtures?id`响应并按声明路径重建，不能当作曾请求`/fixtures/players`的证据。高级协议hash、实际算法文件指纹、同场同父匹配、全部机会台账和首失败各自核验；成功只证明相应实际范围。新增读取／决定入口不自动改变生产采用、正式确认资格或旧F5／v1／v2身份。

高级捕获的当前远端赛前保存证明核`advanced-durability-v3`及方法`git_tree_typed_reference_closure_and_fixed_attempt_job_log_exact_saved_commit_v3`：固定run attempt的已完成job日志必须含精确状态提交，使用服务端job完成时刻作保守保存上界，并复验该状态的完整原件引用闭包。只有已登记的生产replay路径及原schema／status／metadata契约按canonical payload摘要核逻辑身份，同时另绑定真实gzip原字节SHA；其他引用保持原字节hash严格匹配。旧v2证明和失败记录保留，不能只改标签升级为v3。本地commit时间、状态metadata或本地receipt不能单独证明远端赛前保存；类型语义误用、真实原件损坏、日志受限和保存失败分别记录，不笼统称数据坏了或排除诊断引用。

## 2026-10-13：批次十二共用状态与旧清单兼容

半小时高级及旧v1／v2捕获／评分可共享一次恢复与最终保存，但读取时仍按各自协议hash、真实源／状态提交、原件引用和独立回执判定；共同job成功不代替每个子步骤成功。旧4 GiB／1 GiB和2 GiB／500 MiB清单继续按其原声明验读，当前8 GiB逻辑／1792 MiB物理上限不追改旧原件。实际push pack和磁盘检查、GitHub状态提交及原字节回读分别留证；扩容不是长期容量证明。新增安全失败类别只解释拒绝原因，不使错误来源成为可用特征，也不能据一个旧latest称最新流程已通过。

## 2026-10-16：批次十三的固定冷历史与连接器离线读取

本轮按需冷读不把旧原件删掉，也不把固定状态的同一路径替换成后来latest。活动展开工作集8 GiB、传输树1792 MiB、累计冷历史原字节／展开体积和实际pack分别计量。冷层仅包含已在可达固定`model-state`提交中核实保存的内容寻址研究objects／inputs，当前目录保存带精确路由的catalog；模型卡、队列、首失败、当前指针和未远端保存的新文件保持活动状态。具体身份与边界见框架13.3、16.6及`advanced-models.md`，目前为本地实现待云端验收。

选择读取的顺序如下，所有取得动作保留完整真实响应及Git blob：

1. 固定当前源码及状态完整SHA；从该状态读取`runtime/history_archive/catalog.json`。catalog若为`fixed-git-history-catalog-index-v1`，再从同一状态取得其声明的`runtime/history_archive/catalog_chunks/<sha>.json`，核引用hash、计数及全部需要的路由。原`fixed-git-history-catalog-v1`内联清单也可验读，不能自己制作删减清单。
2. 按目标逻辑路径精确定位条目，读取其`state_commit`、`transport_path`、`manifest_sha256`、`blob_sha256`、`raw_sha256`及大小。这里`state_commit`是原件保存源提交，不替换本次分析的当前状态SHA。向该精确源提交取得原`state_storage.json`和对应`state_archives/<blob_sha256>.json.gz`，或条目明确指定的原始路径；不探测其他聊天文件、不切换到浮动历史分支、不以当前相同路径冒充旧来源。
3. 使用`materialize_model_access.py`输入`schema_version=2`保存混合来源。当前源码／状态仍分别用`role=source/state`；历史文件单项使用`role=history`，`commit`填写该条目完整40位源提交，`path`是该源Git树中的原路径，不是本地落地路径。原响应`display_url`必须精确绑定`https://github.com/wuyan0231-hub/football-model/blob/<该源commit>/<原path>`，`sha`及字节继续严格核Git blob。旧schema 1只接收source／state，兼容行为不变。
4. helper将历史项自动落在`history_sources/<commit>/<原path>`，并在`model_access_manifest.json`保存`role=history`、本地`path`、原`source_path`、源`commit`、原字节SHA-256、Git blob与大小。当前source／state不能占用`history_sources/`保留空间。缺祖先清单、gzip、catalog块或来源指纹失配即拒绝对应读取，不从网络或本地Git隐式补件。
5. 用选择恢复入口沿真实catalog验证源清单、压缩与原始hash并恢复所需逻辑原字节；再沿高级compact全部对象引用取得剩余闭包。只恢复一个顶层对象不等于整份报告、模型或本场闭包齐全。最后仍执行真实`integration match`或对应研究／评分原函数，记录实际命令、结果和使用边界。

以下只说明单个历史传输对象的schema 2结构，占位值必须换成真实响应，完整输入还须包含当前catalog／分块、源清单、源码和所需状态；不构成已执行回执：

```json
{
  "schema_version": 2,
  "repository": "wuyan0231-hub/football-model",
  "source_commit": "<本次固定源码40位SHA>",
  "state_commit": "<本次固定当前状态40位SHA>",
  "files": [
    {
      "role": "history",
      "commit": "<catalog指定的源状态40位SHA>",
      "path": "state_archives/<实际压缩SHA256>.json.gz",
      "response": {
        "content": "<该源提交取得的完整base64原响应>",
        "encoding": "base64",
        "sha": "<真实Git blob SHA>",
        "display_url": "https://github.com/wuyan0231-hub/football-model/blob/<同一源状态40位SHA>/state_archives/<同一实际压缩SHA256>.json.gz"
      }
    }
  ]
}
```

```sh
python scripts/materialize_model_access.py --input selected-responses.json --destination NEW_DIR
python scripts/materialize_state_archive.py --state-root NEW_DIR --path advanced_models/inputs/ACTUAL_INPUT_SHA256.json --output NEW_DIR/archive-access.json
```

`NEW_DIR`必须是尚不存在的新目录，`ACTUAL_INPUT_SHA256`及路径必须来自实际引用。下载的大文件若工具返回空`content`但附Git blob，不能当作空原件或后台未执行；可用同一真实blob的受支持读取接口取得完整字节，保留两条真实响应、路由与封套转换记录，再核Git blob，禁止把截断或自填内容伪装原响应。必要源字节仍取不到时如实报告读取受阻，不改hash或制作“精简原件”。

离线连接器路径只消费已经materialize并核验的当前与历史来源，不联网、不读取认证、不训练；Git工作流内的按需读取也只跟随固定catalog源路由。无论走哪条路径，“文件已落地”“所选历史原字节已恢复”“全引用闭包通过”“本场已复算”“远端赛前持久资格通过”均是独立状态。本轮helper存在或本地测试通过，不能代替云端部署和真实保存回读。
