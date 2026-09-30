# Agent Session Detective

> 事后审计 agent 会话对 skill 的遵守情况 —— 该加载的加载了吗？什么时候加载的？本该触发却没触发的是哪些？

`asd` 读取一次 agent 会话的原始日志，重建 skill 的完整生命周期（加载、上下文开销、被压缩挤出），用 LLM 反事实判断找出**本该触发却从未加载**的 skill，最后渲染成一个自包含的交互式 HTML 报告。

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
```

报告默认写到 `<会话目录>/skill-audit.html`，可用 `--out` 改。

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

## CLI 参数

| 参数 | 说明 |
|---|---|
| `session` | 会话目录（含 `wire.jsonl`）。省略则自动找最近一个 |
| `--sessions-root` | 自动定位会话时的根目录，默认 `~/.kimi/sessions` |
| `--skills-dir` | 追加 skill 目录到 catalog，可重复 |
| `--out` | 报告输出路径，默认 `<会话目录>/skill-audit.html` |
| `--open` | 生成后直接用浏览器打开 |
| `--no-judge` | 只出事实，跳过 LLM 判断 |
| `--expect` | 逗号分隔的预期 skill 名，渲染成命中/缺失清单 |
| `--judge-limit` | 只判断前 N 个未消费的 skill |
| `--steps` | 待评估的 playbook 文件，可重复（需要 judge） |
| `--gate` | CI 门槛；配合 `--steps` / `--expect` 决定退出码 |

> ⚠️ **默认根目录的坑**：`--sessions-root` 默认是 `~/.kimi/sessions`（CLI 格式日志）。桌面版 kimi-code 的会话在 `~/.kimi-code/sessions/`，需要显式指定：
> `--sessions-root ~/.kimi-code/sessions`

---

## 支持的日志格式

`asd` 同时理解两种磁盘格式，并在解析层统一成同一个 `Event` 模型，下游模块只看到一种形状。

| 格式 | 主日志 | 子代理日志 |
|---|---|---|
| **CLI**（protocol 1.9） | `<session>/wire.jsonl` | `<session>/subagents/<id>/wire.jsonl` |
| **桌面版**（protocol 1.5） | `<session>/agents/main/wire.jsonl` | `<session>/agents/agent-*/wire.jsonl` |

桌面版记录（`turn.prompt`、`context.append_loop_event`、`token_counting.measured`、`subagent.spawned`）会被翻译成 CLI 格式对应的事件类型；子代理里加载的 skill 会归因到父会话的时间线上。

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
    ├── catalog.py    # 扫描本地 skill 目录，提取触发语料
    ├── judge.py      # 反事实触发判断
    ├── if_eval.py    # 指令遵循度评估（移植自 AWS Skill Eval）
    └── report.py     # HTML 渲染
```

skill catalog 从 `~/.agents/skills` 和 `~/.kimi/skills` 扫描，可用 `--skills-dir` 追加。

---

## 已知限制

- **只读日志，不插桩**——历史会话可审计，但精度受限于日志本身记录了什么。日志不记录"skill 被丢弃"，所以挤出只能标注为推断。
- **只支持 Kimi Code 的两种格式**。Claude Code 和框架无关的中间格式未实现。
- **v1 不做跨会话统计**，一次只审一个会话。
- **"加载了但没遵守"是独立的一条轴**：`--steps` 的分数衡量的是"规定动作执行度"，被 playbook 明文授权、带 `skip: <原因>` 的跳过**仍然计 0 分**。这是有意的——判断遵守度需要 `--steps` 评分和 `--expect` 清单两个轴一起看。
- **`if_eval` 的步骤解析比较挑格式**：只认顶格的 `N. ` 编号行，缩进续行会被拼接。
- **`--judge-limit` 按 catalog 顺序截断**，落到哪批 skill 上不稳定。
- 仓库目前**没有自动化测试**。

---

## 相关文档

- [需求与设计决策](docs/brainstorms/2026-09-29-agent-skill-audit-requirements.md) —— 为什么这样设计、v1 边界在哪
- [poteto-mode 路由合规测试方案](docs/poteto-mode-test-plan.md) —— 用靶场应用验证 agent 是否按路由规则加载 skill，含 T1–T4 用例与轮次结论
