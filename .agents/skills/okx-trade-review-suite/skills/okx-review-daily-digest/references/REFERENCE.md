# okx-review-daily-digest · 算法与规则参考

本文件定义聚合编排、建议去重与优先级排序规则，不重复子 Skill 的分析公式。

---

## 1. 聚合范围与子 Skill 清单

固定可聚合模块（7 个）：

1. `okx-review-attribution`
2. `okx-review-equity-curve`
3. `okx-review-streak`
4. `okx-review-benchmark`
5. `okx-review-behavior`
6. `okx-review-fees-audit`
7. `okx-review-hold-duration`

说明：`okx-review-daily-digest` 仅做编排与拼装，不重算子模块指标。

---

## 2. 调用与容错策略

### 2.1 调用顺序

```text
attribution -> equity -> streak -> benchmark -> behavior -> fees -> hold-duration
```

### 2.2 超时与重试

- 单模块超时：默认 90s
- 重试次数：默认 2 次
- 失败策略：默认“跳过失败模块并继续”

### 2.3 最小可发布条件

```text
success_modules >= min_modules_required (默认 5)
```

低于阈值时输出“降级总报”（仅含成功模块摘要，不给 Top3 强建议）。

---

## 3. 总览卡指标合成

示例合成字段：

| 指标 | 来源模块 | 合成规则 |
|------|----------|----------|
| 净值变化 | equity-curve | 取区间收益 |
| 最大回撤 | equity-curve | 取 MDD |
| 胜率 | streak / attribution | 优先 streak，缺失则 attribution |
| Alpha | benchmark | 主基准 alpha |
| 费用占比 | fees-audit | 费用/毛利比例 |
| 行为风险 | behavior | 追高/割肉/报复核心指标 |
| 时长指数 | hold-duration | 拿不住盈利指数 |

---

## 4. 综合建议去重算法

### 4.1 建议标准化

每条子建议映射为结构：

```text
{
  source_skill,
  priority_class: 风控/行为/策略,
  action_verb,
  target_metric,
  threshold,
  suggested_value,
  confidence
}
```

### 4.2 语义去重

```text
if action_verb + target_metric 相同
and suggested_value 差异 <= tolerance
=> 视为同类建议，保留置信度更高的一条
```

### 4.3 冲突裁决

优先级固定：`风控 > 行为 > 策略`。

同优先级冲突时：
1. 选择覆盖更多模块支持的建议
2. 若仍冲突，选阈值更保守的建议

### 4.4 Top3 生成

```text
score = w1*priority_weight + w2*confidence + w3*cross_module_support
按 score 降序取前 3
```

默认 `priority_weight`: 风控=3，行为=2，策略=1。

---

## 5. “本期最重要的3件事”生成规则

来源：综合建议池 + 指标异常池。

生成条件：
1. 至少 1 条风控动作
2. 至少 1 条行为或执行动作
3. 至少 1 条策略动作

若某类缺失，按下一高分建议补位。

---

## 6. 路由防冲突规则

| 用户意图 | 路由目标 |
|----------|----------|
| “完整复盘/周报/月报/一键总报” | `okx-review-daily-digest` |
| “只看回撤/权益曲线” | `okx-review-equity-curve` |
| “只看连亏连胜” | `okx-review-streak` |
| “只看手续费” | `okx-review-fees-audit` |
| “只看行为问题” | `okx-review-behavior` |

若句子同时包含“总报 + 某模块细节”，先输出总报，再附模块深链。

---

## 7. 输出层级约束

- 简要：只保留总览卡 + Top3 + 每模块 1 行摘要
- 标准：简要 + 每模块关键指标 + 1-2 条建议
- 完整：标准 + 各模块 ASCII 可视化与来源追踪

---

## 8. 边界与注意事项

1. 子模块口径不一致时，以“口径说明”显式提示。
2. 聚合报告不替代子模块深度排障。
3. 定时任务失败需保留失败日志，避免静默丢报。
4. 本 Skill 不做任何交易指令。

---

## 9. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：7 模块聚合、建议去重、优先级排序、定时总报 |
