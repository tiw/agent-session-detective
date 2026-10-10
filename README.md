# Agent Session Detective

> 事后审计 agent 会话对 skill 的遵守情况 —— 该加载的加载了吗？什么时候加载的？本该触发却没触发的是哪些？

`asd` 读取一次 agent 会话的原始日志，重建 skill 的完整生命周期（加载、上下文开销、被压缩挤出），用 LLM 反事实判断找出**本该触发却从未加载**的 skill，最后渲染成一个自包含的交互式 HTML 报告。

支持 Kimi Code（CLI 和桌面版）、Qoder transcript 和 Codex session 三种格式。

纯标准库实现，零第三方依赖。

---

## 核心思想

**确定性事实与 LLM 判断严格分离。**

- 所有加载事件、token 消耗、压缩事件都从日志**确定性抽取**，可复核、可追溯。
- LLM 只回答一个问题：*"在对话的这个决策点上，这个 skill 该不该触发？"*
- 每条阳性判断**必须附逐字引用**的证据。没有证据的判断被当作**工具缺陷丢弃**，而不是当作发现呈现。

报告里凡是推断出的结论（例如"被压缩挤出"——日志从不记录这件事）都显式标注为 inference，与 log fact 区分开。

这套设计针对三种真实观察到的失败模式：

| 失败模式 | v1 覆盖 |
|---|---|
| 触发条件明显匹配，但 skill 从未加载 | ✅ 完整支持（反事实判断 + 证据） |
| skill 加载了，但内容不全 / 不对 | ⬜ 只呈现事实 |
| skill 内容在上下文里，但模型没遵守 | ⬜ 通过 `--steps` 单独评估 |

---

## 安装

```bash
cd /Users/ting/work/agent_session_detective
pip install -e .
asd --help
```

或者不安装，直接用源码跑：

```bash
PYTHONPATH=src python3 -m agent_session_detective --help
```

要求 Python >= 3.9。

---

## 快速开始

```bash
# 审计最近一次会话，只出事实，不调 LLM
PYTHONPATH=src python3 -m agent_session_detective --no-judge

# 审计指定会话，带上"预期应该被消费的 skill"清单
PYTHONPATH=src python3 -m agent_session_detective ~/.kimi-code/sessions/wd_xxx/session_yyy \
  --no-judge \
  --expect "Poteto Mode,how,why,architect,principle-fix-root-causes,tdd,unslop" \
  --open

# 审计指定的 Qoder transcript
PYTHONPATH=src python3 -m agent_session_detective \
  ~/.qoder/projects/<workspace>/<session>.jsonl --no-judge

# 审计指定的 Codex session
PYTHONPATH=src python3 -m agent_session_detective \
  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl --no-judge
```

省略 `session` 时，会在 Kimi 会话和 `~/.qoder/projects` 下的 Qoder transcript 中选择最近修改的一份。

报告默认写到 `<会话目录>/skill-audit.html`；Qoder transcript 则写到同目录的 `<session>.skill-audit.html`，可用 `--out` 改。

### 开启 LLM 触发判断

judge 走任意 OpenAI 兼容端点，凭据从环境变量读：

```bash
export ASD_JUDGE_BASE_URL=https://api.deepseek.com/v1
export ASD_JUDGE_API_KEY=sk-...
export ASD_JUDGE_MODEL=deepseek-flash

PYTHONPATH=src python3 -m agent_session_detective <会话目录>
```

`ASD_JUDGE_API_KEY` 为空时会回落到 `DEEPSEEK_API_KEY`（并默认 base_url / model）。没配凭据就不报错，只降级为"事实模式"。

`--judge-limit N` 只判断前 N 个未消费的 skill，跑便宜的抽样。

### 评估指令遵循度

`--steps` 传入 playbook 文件，逐条评判它的编号步骤：

```bash
PYTHONPATH=src python3 -m agent_session_detective <会话目录> \
  --steps ~/.agents/skills/poteto-mode/playbooks/bug-fix.md \
  --steps ~/.agents/skills/poteto-mode/playbooks/feature.md \
  --gate 0.75
```

每个步骤判 `covered` / `partial` / `skipped`，权重 1.0 / 0.5 / 0.0，覆盖率**由判定重算**而非采信模型自报。判定同样必须带逐字证据，证据对不上轨迹的一律降级为 skipped。

默认门槛 0.75（Mostly Followed）——步骤是规定性的，所以及格线高于中位数。

### 当 CI 门禁用

两条失败路径的触发条件**不一样**，别搞混：

| 失败路径 | 需要 `--gate` 吗 |
|---|---|
| IF 覆盖率低于门槛 | **不需要**。`--steps` 一开就生效，用默认门槛 0.75 或 `--gate` 覆盖 |
| `--expect` 里的 skill 缺失 | **需要**。不给 `--gate` 时 `--expect` 只渲染清单，退出码仍是 0 |

所以想卡住"路由回归"，必须显式给 `--gate`：

```bash
asd <会话目录> --expect "Poteto Mode,bug-fix,tdd" --gate 0.75 || echo "路由回归"
```

其它非零退出：找不到会话、没解析出任何事件、用了 `--steps` 但没配 judge。

---

## Web 界面

`--serve` 在本地起一个 Web 应用，浏览器里扫描会话、配置并运行审计、看报告：

```bash
PYTHONPATH=src python3 -m agent_session_detective --serve          # http://127.0.0.1:8471
PYTHONPATH=src python3 -m agent_session_detective --serve --port 9000
```

- **会话列表**：扫描 `~/.kimi-code/sessions` 和 `~/.kimi/sessions` 两个根目录，显示**最近 10 个**会话（工作区 + 时间 + 是否已审计）。列表只做文件发现（stat 级，毫秒级），不解析日志——轮数/skill/token 统计都在你点审计那一刻才算
- **fleet 汇总**：侧栏 `fleet` 按钮按需触发，跨会话聚合（会话数、平均轮数、平均缓存命中率、增长形状分布、重复注入总量）——「纠规则不纠个案」的入口；要解析全部日志，约 10 秒，期间页面可继续操作
- **审计表单**：填 `--expect` 清单、选 playbook 做 IF 评估、勾选是否跑 missed-trigger 判定（LLM 部分后台线程执行，页面轮询进度）
- **报告渲染**：expectations 核对表、findings、IF 逐步判定、skill 生命周期、事件流，与 CLI 版同一套数据形状
- **事件流角色 badge**：每条事件标注 `user` / `think` / `say` / `tool` / `result` / `compact`，人的输入、模型思考、模型回复、工具 traffic 一眼分开

JSON API 也可以独立使用（`GET /api/sessions`（最近 10 个，纯发现）、`GET /api/fleet`（按需全量聚合，慢）、`POST /api/audit`、`GET /api/job/<id>`、`GET /api/playbooks`），方便接进自己的工具链。

> ⚠️ 服务无鉴权，只绑定 `127.0.0.1`，不要暴露到公网。

### 板块解读

报告每个板块标题旁都有个 `?`，点开浮层有同样的说明。判定是判断还是事实、证据在哪里核对，看这张表：

| 板块 | 是什么 | 怎么读 |
|---|---|---|
| **summary** | 本次审计的硬数字 | 轮数、事件数、加载/直读次数、judge 是否可用、耗时。异常的第一道筛查 |
| **expectations** | 按路由规则该被消费的 skill 清单 | `loaded` = 经 Skill 工具正式加载；`file-read` = SKILL.md 被当文件读过（kimi-code 下是绕过机制，codex 下是正规方式，**harness 相对**）；`missing` = 都没有，路由没走的第一信号 |
| **findings** | 本该触发却从未加载的 skill | 判定是 LLM 反事实推理（**判断**）；证据是从会话原文的逐字引用（**可核对的事实**）。无证据或证据非原文的判定被丢弃计为工具缺陷——缺陷多时先怀疑解析器，别急着怀疑 agent |
| **instruction following** | 所选 playbook 每个编号步骤的执行判定 | `covered` = 轨迹里有实际行动/结果；`partial` = 做了一部分；`skipped` = 未执行。**带 skip 理由也计 skipped**——这轴度量步骤执行度，不度量裁量合规（那看 expectations）。覆盖率由判定重算，不信模型自报 |
| **token governance** | 第 0 步审计的四指标 | 缓存命中率（usage 记录，全 agent 合计）；每轮增量形状（linear / sublinear / accelerating）；六桶分解（**system 桶是残差，标注推断**；`skill` 技能正文与 `inject` harness 包裹进你输入里的内容单独成桶，`tool` 只装其余工具结果；其余按事件内容估计）；prompt 哈希翻转（hash 变化是日志事实，「缓存失效」是标注的推断）。附重复注入检测：同一份工具结果被多次读入 ≈ 状态未外置 |
| **skill lifecycle** | 每次加载/直读的完整记录 | 展开可见**加载时捕获的内容快照**（之后 skill 文件改了也不影响本报告）；`evicted?` = 压缩后可能被挤出上下文（**推断**，日志从不记录这件事）。声明了 `disable-model-invocation` 的 skill 会带核对提示：日志无法区分点名与自调 |
| **event feed** | 原始事件流（最近 400 条） | 角色 badge：`user` 人的输入、`think` 模型思考、`say` 模型回复、`tool` 调用、`result` 返回、`compact` 压缩；`[subagent:x]` 表示来自子代理 |

---

## CLI 参数

| 参数 | 说明 |
|---|---|
| `session` | Kimi 会话目录（含 `wire.jsonl`）或 Qoder transcript（`~/.qoder/projects/<workspace>/<session>.jsonl`）。省略则自动找最近一个 |
| `--sessions-root` | 自动定位最近 Kimi 会话时的根目录，默认 `~/.kimi/sessions` |
| `--skills-dir` | 追加 skill 目录到 catalog，可重复 |
| `--out` | 报告输出路径，默认 Kimi 的 `<会话目录>/skill-audit.html` 或 Qoder 的 `<session>.skill-audit.html` |
| `--open` | 生成后直接用浏览器打开 |
| `--no-judge` | 只出事实，跳过 LLM 判断 |
| `--expect` | 逗号分隔的预期 skill 名，渲染成命中/缺失清单 |
| `--judge-limit` | 只判断前 N 个未消费的 skill |
| `--steps` | 待评估的 playbook 文件，可重复（需要 judge） |
| `--gate` | CI 门槛；配合 `--steps` / `--expect` 决定退出码 |
| `--serve` | 启动 Web 应用（默认端口 8471） |
| `--port` | `--serve` 的端口 |

> ⚠️ **默认根目录的坑**：`--sessions-root` 默认是 `~/.kimi/sessions`（CLI 格式日志）。桌面版 kimi-code 的会话在 `~/.kimi-code/sessions/`，需要显式指定：
> `--sessions-root ~/.kimi-code/sessions`

---

## 支持的日志格式

`asd` 同时理解三种磁盘格式，并在解析层统一成同一个 `Event` 模型，下游模块只看到一种形状。

| 格式 | 主日志 | 子代理日志 |
|---|---|---|
| **CLI**（protocol 1.9） | `<session>/wire.jsonl` | `<session>/subagents/<id>/wire.jsonl` |
| **桌面版**（protocol 1.5） | `<session>/agents/main/wire.jsonl` | `<session>/agents/agent-*/wire.jsonl` |
| **Qoder transcript** | `~/.qoder/projects/<workspace>/<session>.jsonl` | 不适用 |

桌面版记录（`turn.prompt`、`context.append_loop_event`、`token_counting.measured`、`token_counting.turn_recorded`、`usage.record`、`llm.request`）会被翻译成 CLI 格式对应的事件类型；子代理里加载的 skill 会归因到父会话的时间线上。缓存与输出 token 两种格式都有：桌面版在 `usage.record`，CLI 版在 `StatusUpdate.token_usage`；prompt 前缀稳定性（`systemPromptHash`/`toolsHash`）只有桌面版记录。

Qoder transcript 支持提取对话、工具和 skill 事实，以及其中存在的 usage 记录；上下文测量、压缩事件和 prompt/tool 哈希在该格式中不可用，报告会显示为不可用，不会用推断值替代。

Codex session 支持提取对话、工具调用、推理摘要和 token usage 记录；上下文测量、压缩事件和 prompt/tool 哈希在该格式中不可用，报告会显示为不可用，不会用推断值替代。

skill 的识别有两条独立路径，报告里分开呈现：

1. **正式加载** —— `Skill` 工具调用（带返回内容快照）
2. **直读** —— 用文件工具直接读 `SKILL.md`，绕过 Skill 机制

第 2 条是日志事实；至于它是否构成对正式加载的"合理替代"，留给读者判断。

---

## 报告结构

单文件 HTML，无 JS、无外部资源，折叠用原生 `<details>`。

| 区块 | 内容 |
|---|---|
| **Expectations** | `--expect` 清单的命中情况：`loaded` / `file-read only` / `MISSING` |
| **Findings** | 本该触发却没触发的 skill，每条带决策点、轮次、置信度、逐字证据 |
| **Instruction Following** | `--steps` 的逐步判定表 + 覆盖率 + PASS/FAIL |
| **SKILL.md Read as Plain Files** | 绕过 Skill 机制的直读记录 |
| **Skill Lifecycle** | 每次加载：内容快照、token 估计、加载后的上下文占用、是否可能被压缩挤出 |
| **Compactions** | 压缩事件起止时间 |
| **Context Usage** | 上下文 token 占用的折线图（内联 SVG） |
| **Token Governance** | 缓存命中率、每轮增量形状判定、六桶分解堆叠图（system 桶为残差推断）、prompt 哈希翻转表、重复注入检测 |
| **Event Feed** | 原始事件流，每条带时间戳和来源 |

skill 展示的是**加载时捕获的内容快照**——所以即使 skill 文件后来被改了，报告仍然准确。

---

## 项目结构

```
.
├── pyproject.toml
├── docs/
│   ├── brainstorms/
│   │   └── 2026-09-29-agent-skill-audit-requirements.md   # 需求与设计决策
│   └── poteto-mode-test-plan.md                           # 靶场测试方案与轮次记录
└── src/agent_session_detective/
    ├── cli.py        # 命令行入口、参数、退出码
    ├── wire.py       # 日志解析（两种格式 → 统一 Event）
    ├── timeline.py   # 事实时间线：加载、直读、压缩、token 序列
    ├── tokenstats.py # token 治理统计：缓存、增长形状、六桶、哈希翻转、重复注入
    ├── catalog.py    # 扫描本地 skill 目录，提取触发语料
    ├── judge.py      # 反事实触发判断
    ├── if_eval.py    # 指令遵循度评估（移植自 AWS Skill Eval）
    ├── report.py     # HTML 渲染
    ├── web.py        # Web 应用：JSON API + 审计任务队列
    └── webapp/       # 单页 UI（index.html / style.css / app.js）
```

skill catalog 从 `~/.agents/skills` 和 `~/.kimi/skills` 扫描，可用 `--skills-dir` 追加。

---

## 已知限制

- **只读日志，不插桩**——历史会话可审计，但精度受限于日志本身记录了什么。日志不记录"skill 被丢弃"，所以挤出只能标注为推断。
- **支持 Kimi Code 的两种格式、Qoder transcript 和 Codex session**。Claude Code 和框架无关的中间格式未实现。
- **跨会话只有轻量 fleet 聚合**（web 会话列表上方的汇总条），不做趋势存储和历史对比；一次深度审计仍然只针对一个会话。
- **"加载了但没遵守"是独立的一条轴**：`--steps` 的分数衡量的是"规定动作执行度"，被 playbook 明文授权、带 `skip: <原因>` 的跳过**仍然计 0 分**。这是有意的——判断遵守度需要 `--steps` 评分和 `--expect` 清单两个轴一起看。
- **`if_eval` 的步骤解析比较挑格式**：只认顶格的 `N. ` 编号行，缩进续行会被拼接。
- **`--judge-limit` 按 catalog 顺序截断**，落到哪批 skill 上不稳定。
- **Web 服务只绑定 localhost、无鉴权**，不适合共享或暴露；审计任务存在内存里，重启即失。
- 仓库目前**没有自动化测试**。

---

## 开发记录

### 2026-10-09

| 时间 | 内容 |
|---|---|
| 09:57 | **每轮上下文由账单记录重建**：请求完整 prompt（input + cache read + creation）即模型所见上下文，Qoder transcript / Codex 这类只有 usage 记录的格式也能画每轮增长图；同修 subagent 压缩事件泄漏进主 timeline 的 bug |
| 11:30 | **IR 1.1：skill 加载证据台账 + dispatch 检测**——新增 `ir/loads.py`、`ir/dispatch.py`、`ir/phase_rules.py`，加载证据链与派发链进入中间表示 |
| 15:16 | **discovery 收紧**：会话发现只认真实会话、拒绝 Qoder terminal CLI transcript；每轮增长图缺席时说明原因而非静默跳过 |
| 15:40–18:07 | **IR 1.2：actual skill tree 全链路**——从会话事实构建真实 skill 树（裸 id 并入命名空间变体、别名感知加载标记、嵌套派发链、loose agent 子树与孤儿提示），渲染为独立 HTML 页（新增 `ir/skill_tree.py`、`tree_html.py`）；CLI 加 `--tree-out`，Web 加 `GET /api/tree` |
| 19:27 | tree 页统一为 webapp 暗色主题，来源列表折叠 |
| 19:56 | **Qoder terminal-cli transcript 降级审计**：经侧栏 discovery 收编，可审计但标注降级 |
| 20:12 | billed usage 设计 spec（approach A，opt-in flag）+ 实现计划落盘 |
| 21:46 | **IR 1.3：操作者原话进 IR**，按干预形式（intervention form）分类标注——新增 `ir/intervention.py` |
| 跨零点 | **billed usage 实现**（10-10 凌晨 00:06–00:19）：IR 加 `BilledUsage` 可选后置字段；`billing.py` 只读 SharedClientCache reader（uuid join）；CLI `--billed-usage` / `--billed-db`；tree 页 header 渲染账单块；`GET /api/tree?billed=1` |

设计文档见 `docs/superpowers/specs/`，实现计划见 `docs/superpowers/plans/`。

---

## 相关文档

- [需求与设计决策](docs/brainstorms/2026-09-29-agent-skill-audit-requirements.md) —— 为什么这样设计、v1 边界在哪
- [poteto-mode 路由合规测试方案](docs/poteto-mode-test-plan.md) —— 用靶场应用验证 agent 是否按路由规则加载 skill，含 T1–T4 用例与轮次结论
