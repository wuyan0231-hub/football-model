# S2 正负价值权限与旧记录兼容

状态：离线实现及合成回归。此文落实现行批次五框架 §2.3、§9.2、§11.3、§12.3、§12.5 的既有对称要求，不改生产概率、正式框架、模型成熟度或统一 EV 门槛。不代表任何真实任务已经验收，也不代表准确率或收益提高。

## 判词与程序权限

| 声明 | 保留数值 | 额外资格门 | 对适配的作用 |
|---|---|---|---|
| `unquantified` | 数值为 null | 无 | 无模型不自动否决 |
| `unclear` | 允许已复算正或负点估计 | 无新增权限门 | 不自动确认价值、不自动否决 |
| 旧 `negative` | 允许已复算负点估计 | 无新增权限门 | 仅兼容点估计；新记录优先写 `unclear` |
| `confirmed_positive` | 必须有同票正 EV | 用途、同任务资格、引用及不确定性均须满足 | 可结构性支持正向声明，仍须语义审核 |
| `confirmed_adverse` | 必须有同票负 EV | 与正向使用同一准入标准 | 合格后与“适合／条件适合”形成明确结构冲突 |
| 模型 `value_assessment=valid_negative` | 必须绑定同票负 EV 计算 | 与 `confirmed_adverse` 相同 | 合格后不能通过删除候选模型引用或 value 对象隐藏 |

`negative` 原本只检查负号，不能再凭这个标签取得充分不利权限。原 `valid_negative` 缺乏资格时，审计输出缺件或错误，保留原数值和原记录；**不把无依据的强标签转成对比赛的真实负面事实**。诊断权限可保存正负点估计，但不能选择性升级为任一方向的明确价值。

适配冲突是需要重新审阅的结构问题；工具不选择替代方向，不自动下注，也不证明不建议的足球理由正确。历史 `negative` 的旧文字含义仍保留在原报告中，本次兼容解释不改写历史正文。

## 最小扩展：继续使用既有记录和引用表

强声明仅在既有 `model_records[]` 增加以下字段，不新建第二套正式决定或评分系统：

```json
{
  "model_record_id": "本次模型评估ID",
  "candidate_id": "本次候选ID",
  "version_id": "本次版本ID",
  "model_decision_role": "value_assessment",
  "role_declaration_ref": "冻结用途依据source_id",
  "model_output_refs": ["实际输出来源source_id"],
  "task_qualification": {
    "status": "accepted",
    "contract_id": "本次合约ID",
    "quote_id": "本次报价ID",
    "scope_refs": ["既有相关任务验收依据ID"]
  },
  "calculation_record_id": "本次同模型同合约同报价计算ID",
  "uncertainty_assessed": true,
  "uncertainty_support": "negative",
  "uncertainty_refs": ["本次不确定性依据ID"],
  "validation_refs": ["既有相关验证材料ID"]
}
```

以上只示意字段，不是真实依据，不能复制该例宣称模型已验收。

- `model_decision_role` 允许记录 `diagnostic_only` 或 `value_assessment`；强声明仅接受后者。诊断点估计不要求补造完整强声明字段。
- `role_declaration_ref` 指向现有来源/证据引用表中的实际冻结依据。该来源记录必须带原字节 `sha256`、带时区的 `observed_at`、同一 `model_decision_role` 及与本次 `decision_context.model_settings_sha256` 一致的模型设置指纹。
- `model_output_refs` 指向实际已取得的概率/模型输出，每项带 `sha256` 和带时区 `observed_at`。声明依据必须早于所列最早输出的取得时间；同一时间粒度不足以证明先后，因此不视为已证明事前声明。不得只填较早日期而没有冻结原件引用，也不能删除早已看到的输出来制造顺序。
- `task_qualification.scope_refs` 引用已有验收文件，不要求每场重新验收模型。该文件应明确相关联赛、市场/盘口、目标结算范围、预测时距和模型版本；本次只新增“这份既有资格是否适用于这张票”的应用记录。若现有生产模型尚未完成对应任务验收，继续使用探索性点估计，不能把 `accepted` 手填出来。
- `calculation_record_id` 在模型记录中明确绑定该强声明的实际计算，避免从其他模型、候选、版本、合约或报价借用负号。候选 value 仍引用实际展示的计算；另一个合格不利模型即使未被挑进候选 model 列表，也不能被隐藏。
- 正负双方均须 `applicable=true`、已复算、来源为 F/MIX；`uncertainty_refs` 与 `validation_refs` 非空且可解析，方向应分别为 `positive` 或 `negative`。MKT 同价回代不取得此权限。

## 结构可核和语义限制

校验器检查引用能否解析、指纹格式、声明与模型设置是否一致、真实时区顺序的字段关系、模型/版本/票据绑定及计算正负号。证据包校验器仍须核原字节与清单；人工仍须打开声明、验证和不确定性材料，确认真实冻结、范围匹配和方法合理。

**自洽 JSON、早日期或 64 位哈希都不能单独证明原件真实存在。** 此接口不联网、不读任意来源文件、不根据文件名判断相关任务已验收，不解析自由文字中的价格价值。它不能鉴别未披露的更早模型输出，也不能证明某个置信区间合理；这些限制保留为 `manual_review_required`，且始终输出 `economic_advantage_verified=false`、`predictive_quality_verified=false`。

任务资格缺失使相关强声明不完整；实际诊断权限、未验收任务、引用冲突或不确定性不足使强声明不能通过。应保留来源和点估计，再按证据修正判词，不按正负号任意改变模型资格。无模型、有据定性适合、普通首发未知和异步公开参考价沿用原行为。

## 版本、旧记录和验收

输入保持 `match-archive-1.0`／`decision-audit-1.0`。审计输出新增：

```json
{
  "validator_version": "decision-audit-validator-1.1",
  "value_permission_policy": "s2-value-permissions-1.0"
}
```

旧审计保存不动；用新验证器复审必须另存带上述版本的新产物，不能把新规则缺件判断改写成当时已做过的审计。`repeat` 保留这两个版本及逐运行审计，接受新增 `confirmed_adverse` 状态；重复一致性仍不证明正确。

回归入口：

```bash
python -m unittest tests.test_decision_audit tests.test_s2_value_permissions
```

覆盖无模型/诊断点估计兼容、正负对称准入、用途在结果之前的有引用声明、错误时区/缺指纹、首次输出、同票资格、不可解析依据、错误计算负号、跨候选/版本借权、强负面披露、引用失败重复检查和判定版本。全部是工程合成测试，不能充作真实场次受影响或新政策有效的证据。
