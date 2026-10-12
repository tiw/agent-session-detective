# 流程签名：直通 vs pipeline 观测对比 — 设计

日期：2026-10-12
定位：范式仪表盘的第三块（A 库健康、C 流程对比之后，B manifest 遵从度另行立项）——把「直通 vs pipeline」从观点之争变成可测量的观测对比。

## 目标

回答一个问题：**这个会话跑在多少流程机器（workflow machinery）上，机器本身花了多少 token？**

三个交付物：

1. **流程签名（自变量）**：每会话一个纯函数分类块——机制特征 + 粗 style 三分类（`pipeline` / `direct` / `mixed`）。
2. **ceremony 科目（因变量之一）**：listing 重注入 + 协议重读进账单，作为透镜行（display-only，不重复计入 EST 小计）。
3. **fleet 分组对比（因变量聚合）**：`fleet_stats` 按 style 分组，聚合 turns / cache / ceremony / compactions / interventions，web 面板呈现对比表。

## 决策记录

- **观测实验，不是随机对照**。混杂（任务难度、会话长度）不控制，只诚实声明；分层靠流程签名 + 已有判分粗分；真正的因果结论留给用户发起的配对实验（同任务直通/pipeline 各跑一次，ASD 出单会话对比）。UI 固定渲染警示文案。
- **复用既有度量，不建平行系统**。协议重读 = 既有 repeat 分类 `post_compaction`（tokenstats.py）；listing 重注入 = IR skill listing 观测的 per-skill 出现次数；干预数 = IR 1.6 interventions。新代码只做「取数 + 分组」，不新造计量。
- **style 规则只用存在性，不用阈值**：`pipeline` = workflow 文件触碰 ≥1 且 skill loads ≥1；`direct` = 两者皆 0；其余 `mixed`。listing 密度、inject 量等连续特征只呈现不判定，避免「阈值待校准」债。
- **progress.md 读写需要文件路径成为 IR 一等公民**。现状 tool_call 只存 `preview[:200]`，全参数文本只在 extraction 时可得 → IR 1.9 给 `ContentItem` 加 `file_path`（tool_call 参数解析出的字符串路径，None = 无或不可解析）。这正是 B（manifest 遵从度）数据缺口的公共前置，先落一半：字段 + tool_call 解析；完整 file_reads 台账留给 B。
- **指纹不自造版本号**：payload 新增 `process` 键与 ceremony 行全部由 IR document / session 文件派生，IR 1.8→1.9 使 fingerprint 的 `ir%s` 段自变，旧缓存自失效；无 web-local 独立变化，不手工 bump vN。
- **新度量先进报告不进判分**：judge 提示词、IF 评测、gap 候选池一概不动——A/B 的被测对象不能中途变化。

## 模块与纯度边界

新模块 `src/agent_session_detective/ir/process.py`，纯函数：

```python
process_signature(document) -> dict
```

只读 `document.items / skills / skill_loads / interventions`。document-pure，故注册进 `build_analyses`（对照：`skill_gaps` / `library` 因需要 catalog 留在组装层）。

`items.py` 的 ToolCall 分支在 extraction 时解析 arguments（dict 或 JSON str）取 `file_path`，写入新字段——全参数文本只在此处可得。

## 输出 schema

```jsonc
{
  "style": "pipeline" | "direct" | "mixed",
  "inject": {"items": 3, "tokens_est": 8400},
  "listing": {"skills": 37, "occurrences": 74,
              "reinject_occurrences": 37, "reinject_tokens_est": 4200},
  "skill_loads": {"loads": 5, "reloads": 2},
  "workflow": {"reads": 14, "writes": 6},
  "interventions": {"total": 20, "signal": 8},
  "notes": ["no skill listings observed — listing re-injection unmeasurable"]
}
```

- `inject`：bucket="inject" 的 item 计数与 tokens_est（constitution/rules/reminder/memory 等 harness 注入的体量）。
- `listing`：per-skill listing 观测按序，首次 = catalog 提供，其后 = 重注入；`reinject_tokens_est` = 首次之外观测的 tokens_est 之和。
- `workflow`：tool_call 的 `file_path` 命中 `WORKFLOW_FILE_RE`（basename 为 `progress.md`，或路径含 `.changes` 目录段）且工具名为 read / write / edit；读写分开计数。
- `interventions`：`signal` = label ≠ "noise"（noise 是 harness 嘟囔，不算人干预）。
- `notes`：条件性说明，如无任何 listing 观测（非 Qoder 形会话，listing 重注入不可测）。

## ceremony 账单行

`build_bill(..., process=None)` 新参数。当 process 存在且 ceremony > 0 时追加行：

- key `ceremony`，label `仪式开销`，tokens_est = listing 重注入 + 协议重读（`repeat_class_totals["post_compaction"]`）。
- **display-only**：不入 `EST_ROW_KEYS`。原因：listing tokens 已含在 `context_growth` 的 skill 桶里，协议重读已含在 `repeat_tax` 里——ceremony 是换个透镜看同一笔钱，再进小计就是重复计费。detail 明示这一点。
- 不传 process（既有调用方与测试）行为不变。

## fleet 分组

`fleet_stats` 每会话追加：`build_audit_document` + `process_signature` → `s["process"]`（独立 try，失败置 None，会话不退出其余统计）。按 style 聚合 `fleet["process_groups"]`：

```jsonc
{"pipeline": {"sessions": 2, "avg_turns": 41.5, "with_usage": 2,
              "avg_cache_hit_rate": 0.87, "total_output_tokens": 90000,
              "total_repeat_extra_tokens": 23700,
              "repeat_class_totals": {"post_compaction": 22900},
              "total_listing_reinject_tokens_est": 4200,
              "total_ceremony_tokens_est": 27100,
              "total_compactions": 3,
              "total_interventions_signal": 8,
              "avg_interventions_signal": 4.0}}
```

fleet 已是显式慢端点（UI 显示进度、按需触发），每会话多建一份 IR document 属可接受成本；单一实现保证单会话审计与 fleet 口径一致。

## 展示集成

- `report.py`：`render_report` 增参 `process`，Skill GAP 节后插「流程签名」节——style 徽章、特征行、ceremony 估算（listing 来自 process 块，协议重读来自 `token_stats.repeat_class_totals`）。
- `cli.py`：计算 `process_signature(document)` 传入 render_report。CLI 无 bill（bill 是 web-only），ceremony 数字进流程签名节。
- `web.py`：payload 增 `process` 键；`build_bill` 传 `process=process_block`。
- `webapp/app.js`：HELP 增 `process` 键；单会话面板（gaps 面板后）；`renderFleet` 增 process_groups 对比 + 固定混杂警示「观测分组（非随机对照）：任务难度等混杂未控制」。

## 范围守卫

本期不做：task class 分层（无数据源）、judge 维度进 fleet（fleet 不跑 LLM）、billed 维度进 fleet（fleet 不查账单库）、配对实验编排（用户发起）、B 的 manifest 遵从度（等 paze 侧物化 mode/manifest）、完整 file_reads 台账（B 复用 IR 1.9 字段再加）。

## 测试（TDD）

- IR 1.9：`file_path` 落盘（read/write/edit、dict/str 参数、无路径 → None）；`IR_VERSION` pin 断言更新（test_ir_schema.py:124、test_ir_dispatch.py:462）。
- `process.py` 表驱动（SimpleNamespace duck-typed document，同 test_judge.py 的 FakeTimeline 先例）：三种 style 判定、listing 重注入账目、workflow 路径匹配边界（`my_progress.md` 不算、`.changes` 目录内算、非读写工具不算）、interventions 计数、无 listing 的 note。
- bill：ceremony 行存在/数值/不入 EST 小计；process=None 不出行。
- web：run_audit payload 含 process 且 style 正确（qoder 形合成 transcript：attachment listing ×2 + read `.changes/.../progress.md` + stub/body 配对出 load）；fleet 两会话分出 pipeline/direct 两组。
- CLI/report：render_report 收到 process；报告含「流程签名」节。
- app.js：`node --check` + 浏览器手动验证（无法起浏览器则明确报告）。
