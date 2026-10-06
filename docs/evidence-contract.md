# 离线证据、历史截点与读图评估接口

`football_model.evidence` 实现证据文件的冻结与核验、已经结构化的报价行选择、真实截图标注集的评估入口。不联网、不执行 OCR、不读取赛果接口、不训练或调整模型。

本模块沿用 `match-archive-1.0` 的稳定比赛键、版本、原件路径和指纹概念，生成明确标记为 `frozen_analysis_inputs_subset_not_complete_match_report` 的输入子包。它不替代完整框架要求的 HTML/累计 ZIP、合约、条件、执行和赛后结算记录。

## 1. 只冻结实际白名单文件

调用：

```bash
python -m football_model.evidence freeze \
  --source-root /path/to/acquired-inputs \
  --spec /path/to/freeze-spec.json \
  --output /path/to/new-frozen-version
```

`--output` 必须不存在。程序不覆盖原版本，只复制 `files` 指定的原字节；不会搜索目录或自动纳入旁边的文件。路径必须相对 `source-root`，拒绝绝对路径、`..`、软链接、非普通文件及重复来源/路径。

以下仅为格式示意，时刻和比赛身份必须替换为实际记录：

```json
{
  "match_key": "provider:123",
  "version_id": "pre-r1",
  "analysis_cutoff": "2026-10-06T20:00:00+08:00",
  "kickoff_at": "2026-10-06T22:00:00+08:00",
  "future_information_exposure": false,
  "files": [
    {
      "source_id": "original-image-1",
      "path": "images/odds.png",
      "role": "screenshot",
      "observed_at": "2026-10-06T19:59:00+08:00",
      "published_at": null
    }
  ]
}
```

允许角色为 `screenshot/source/model/prediction/rules/decision/report/code`。本场最终赛果、目标答案、结算属于独立评分侧，不得标成这些角色塞入分析子包；`result/target` 等角色直接拒绝。程序可以核验角色声明和时间，但**不能证明任意图片或文本的语义没有夹带赛果**。执行者仍须检查实际内容。

时间必须带时区；未知观测时间用 `null`。`observed_at` 表示原资料真实取得时间，不能因重复读取缓存而刷新；`published_at` 只有可靠证据时填写。已知发布时间不得晚于观测时间，任何资料时刻不得在真实冻结时刻之后。分析截点不能设为未来。

输出包含：

- `originals/`：白名单文件原字节，保留原相对路径。
- `record.json`：真实完成原件复制后的 `created_at`、比赛/版本/截点、来源清单和时间资格。
- `manifest.json`：原件和 `record.json` 的字节数及 SHA-256；清单不自算自身哈希。
- 标准输出 JSON：清单 SHA-256 回执及资格。应把此回执固定在独立保存记录或可信提交中。

不接受调用者传入 `created_at/generated_at`。历史 `analysis_cutoff` 不能倒填实际生成时间。如果冻结跨越开赛时刻，按实际复制完成时间归类。

## 2. 核验、时间准入和不可证明的边界

```bash
python -m football_model.evidence verify \
  --bundle /path/to/new-frozen-version \
  --manifest-sha256 ACTUAL_INDEPENDENTLY_SAVED_DIGEST
```

核验清单、原件字节、路径、未列入文件、来源记录、时间及派生资格。`--manifest-sha256` 可省略，此时仅证明内部一致性；把整个包连清单一起重写，只有独立可信的外部摘要才可以检出。传入与本包一起随意改写的摘要没有额外真实性价值。

| 输出 | 含义 |
|---|---|
| `status=verified` | 原件与记录满足本模块结构、指纹和时间规则，不等于预测有效 |
| `qualification=prospective_input_candidate` | 声明的各项输入在截点前可得，截点和实际冻结均在开赛前，且执行者声明未见未来资料；仍不是已发布的前瞻预测 |
| `qualification=research_only` | 有迟抓、未知观测时间、开赛后冻结、非赛前截点，或已见/不知是否见过未来资料 |
| `artifact_existed_at_analysis_cutoff` | 该包真实生成时间是否不晚于分析截点；不能把稍后冻结的包声称为当时已经发布 |
| `historical_blindness_proven=false` | 哈希和声明不能证明模型、执行者未接触赛果；历史重演保留此限制 |
| `prediction_validated=false` | 工程检查不提供预测准确率或收益验收 |

迟抓/未知资料会保留在研究包中并给出逐来源失败理由；不会悄悄删除后继续冒充完整赛前证据。Python 入口 `load_analysis_inputs(bundle_dir, expected_manifest_sha256=None)` 先重新核验，只要任一输入在截点后取得或取得时间未知便拒绝读取。它返回白名单原件的字节、比赛键、版本和完整资格，不扫描额外文件。历史研究可以读取当时确实已取得的原件，但返回值仍可能是 `research_only`，不能改称盲预测。

冻结不代表物理文件永远不可改：操作策略是不覆盖，后续篡改由指纹核验和外部摘要检测。真实来源与可信时刻仍需独立的获取记录支撑。

## 3. 历史截点报价选择

```bash
python -m football_model.evidence quotes \
  --input /path/to/normalized-quote-rows.json \
  --as-of 2026-10-06T20:00:00+08:00
```

输入为行数组。每行至少含 `row_id/source_id/market/sequence/row_order/quote_time/time_precision/status`。`source_id` 是此接口的独立报价来源分组键，接入完整档案时须另保留图规来源配置及原图 `source_id` 的映射；`sequence` 是有明确页面顺序的截图/页面序列，不能跨不相关页面猜顺序。`row_order=0` 是页面最上方最新行；同组顺序不重复。

市场为 `1x2/asian_handicap/totals`。开放完整行另需 `odds_format` 和 `odds`；后两市场还需数值 `line`，以所显示的主队让球线或总球线表示，四分之一球为最小单位。赔率键：

| 市场 | `odds` 键 |
|---|---|
| `1x2` | `home/draw/away` |
| `asian_handicap` | `home/away` |
| `totals` | `over/under` |

赔率制式为 `decimal/hong_kong/malay/indonesian`，必须与原图核对；此入口不推断制式。封盘行可没有数值，但 `status=closed` 不能被历史数字覆盖。裁切导致缺字段的 `open` 行判为 `incomplete`，也不会从旧行补值。

`quote_time` 必须带时区，未知为 `null`。精度为 `second` 时原文字段须明确含秒；为 `minute` 时表示整个分钟区间，不能添加虚构秒数。比如 `20:00` 与 `20:00:30` 截点重叠，不能证明该行在截点前。

每个来源 × 市场 × 页面组输出：

- `snapshot_latest`：截图最上方状态，原样保留。
- `as_of_latest`：排除明确截点后行后，截点内最新整行状态。
- `excluded_after_cutoff`：已排除的行 ID。
- `blocking_row` 与状态：分钟区间跨截点为 `ambiguous_at_cutoff`，未知报价时刻为 `unknown_quote_time`；两者均禁止跳过再取旧行。

例如 20:05 有价、19:58 封盘、19:55 有价，在 20:00 截点采用 19:58 封盘。同分钟的两个状态都明确早于截点时，可按页面顺序取最上方；不能为了跨越截点或拼接不同页面而发明秒数。明确违反上新下旧的时间序列拒绝并要求核图。

## 4. 真实截图评估输入接口

```bash
python -m football_model.evidence benchmark \
  --truth /path/to/human-reviewed-labels.json \
  --predictions /path/to/reader-output.json
```

标准答案数组每行包含 `row_id/image_sha256/verified_by/fields`。`fields` 是该行实际核对过的字段对象，不将不可见字段伪装为标准答案。预测数组每行包含同一 `row_id` 与 `fields`，或者 `abstain=true` 表示拒识。原图摘要和核对者标识必须存在；本接口本身不取得图片或认证核对者身份。

输出至少读到一个字段的行覆盖、完整行覆盖、字段覆盖、关键字段覆盖及缺失数、已读取字段准确率、全部标注字段正确比例、字段计数及重大错误行数/字段数/已读行错误率。拒识和缺字段降低覆盖，不计作读对；只读到非关键字段也不会变成完整行通过。数值比较不会将布尔值 `false/true` 冒充 `0/1`，包括嵌套赔率对象。关键字段默认包括来源、市场、页面/顺序、报价时间、封盘状态、线位、赔率与制式；Python API可显式指定关键字段集合。

当前仓库测试只验证评估器如何计算，包括拒识与错误线位反例，**不是已完成真实截图准确率测试**。用户实际原图与独立核对标注进入此入口后，才能形成注明样本范围的读图结果；第二遍回图本身不等于准确率证明。

所有 CLI 结果为 JSON；结构、文件、时区等失败返回 `status=rejected` 和稳定原因码，退出码 2。成功生成受限研究包可以退出 0，但必须读取 `qualification/reasons`，不能只看退出码称前瞻通过。
