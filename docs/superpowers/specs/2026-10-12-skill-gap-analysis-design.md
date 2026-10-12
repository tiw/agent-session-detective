# Skill GAP 分析（SKILL.md 期望 × IR 实际）— 设计

日期：2026-10-12
定位：模型行为审计——skill 调度失灵的度量（该触发未触发 / 加载了未使用 / join 失真）。

## 目标

把三块已有积木接成一条 GAP 管线：

1. **期望侧**：`catalog.py` 静态解析磁盘 SKILL.md（frontmatter description + 正文开头 800 字 = `trigger_excerpt`；injected 通道保留）。
2. **实际侧**：IR document 的 `SkillEntity` 观测（listing / body / stub / execution，含 aliases 与 per-agent 归属）。
3. **判定**：`judge.py` 反事实触发判定（verbatim evidence + confidence 校验，R7/R8），候选池改由 IR 派生。

## 决策记录

- 期望抽取用静态分析；LLM 只用于「会话是否满足触发条件」的语义判定（复用 judge 基建）。
- 判定范围：候选预过滤（未加载 ∩ 会话文本含其名/别名），`--judge-limit` 沿用；未判数透明呈现。
- IR 是「已加载」的唯一事实源（body/stub/execution 任一即 loaded）；旧 `timeline.consumed_skill_names()` 不再参与候选过滤，仅保留给 `--expect` 门。

## 模块与纯度边界

新模块 `src/agent_session_detective/ir/gap.py`，纯函数，显式吃三源：

```python
skill_gaps(document, catalog, judgments) -> dict
gap_candidates(document, catalog) -> (candidate_skills, stats)   # 判定前
```

`build_analyses(document)` 保持只读 document 的纯度契约——`skill_gaps` 不进它，由 CLI 与 web 在组装 payload 时并键 `skill_gaps`。

## 输出 schema

```jsonc
{
  "join": {"matched": 12, "ir_only": [/* {skill_id, listed, loaded, executed} */], "catalog_only": 231},
  "sets": {"listed": 15, "loaded": 9, "executions": 6},
  "listed_unloaded": [/* listing≥1 且 body/stub/execution=0 的 skill_id */],
  "missed_triggers": [/* Judgment 字段 + skill_id；triggered=true 才入列 */],
  "loaded_unused": [/* body≥1 且 execution=0；匹配到 catalog 时附 description 对照 */],
  "unjudged": {"count": 0, "prefiltered": 0, "over_limit": 0, "judge_unconfigured": false},
  "notes": [/* join 失真、judge 缺席等条件性说明 */]
}
```

- `ir_only`：IR 观测到但 catalog 匹配不到的 skill（含观测摘要）——join 失真即发现，计数不猜测；其中 loaded 的子集即「磁盘找不到 SKILL.md」的身份数据。
- `catalog_only` 计数为常态大数（磁盘 ≠ 会话可用集），只报数不列名。

## 预过滤与判定流

1. `gap_candidates`：listed_unloaded ∩（名字或别名出现在会话 human_text 拼接文本中）→ 候选 Skill 列表（映射回 catalog 条目取 trigger_excerpt）；prefiltered 剔除数入 `unjudged`。
2. `judge_session` 改为不再内部过滤 consumed（候选过滤权上移给调用方）；`summarize_turns(timeline)` 沿用。单调用方是 cli.py:190，随改随测。
3. CLI 把 judgments 传 `skill_gaps` 产出 missed_triggers；`--judge-limit` 截断数记入 `unjudged.over_limit`；`--no-judge` 时 judge_unconfigured=true，结构性 GAP（join / loaded_unused / listed_unloaded）照常输出。

## 展示集成

- `report.py`：`render_report` 增参 `gap`，新增「Skill GAP」节——missed_triggers 卡片（evidence 引文、turn、confidence）、loaded_unused 表（附期望用途对照）、join/sets 统计行、unjudged 透明计数。
- `web.py`：payload 增 `skill_gaps` 键；`webapp/app.js` 新面板，复用现有 skill 节的渲染样式。
- catalog 默认目录 `DEFAULT_SKILL_DIRS` 增加 `~/.qoder/skills`（flat 一层）；插件缓存嵌套布局继续走 `--skills-dir` 深扫（已有 recursive 支持）。

## 范围守卫

本期不做：磁盘正文与 body sha1 的版本一致性比对、20k 截断检测、渐进披露遵循判定、listing description 资产质量度量。`ir_only` 的存在性记录为这些后续项留了钩子。

## 测试（TDD）

- `gap.py` 纯函数表驱动：三集合 join、listed_unloaded、loaded_unused、ir_only 摘要、预过滤、unjudged 计数、notes 触发条件。
- `judge_session` 去内部过滤后的候选直判契约；fake judge 模式沿既有测试。
- catalog 默认目录含 `~/.qoder/skills`。
- CLI 集成冒烟：synthetic document + injected catalog + fake judge → report 含 GAP 节、JSON payload 含 skill_gaps 键。
- web payload 冒烟。
