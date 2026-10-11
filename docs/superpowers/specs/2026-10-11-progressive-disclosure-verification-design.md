# 渐进式披露机制验证 —— 设计草案

> 目标：把 dev tools report 第三章「加载流程是否符合预期」的能力引入 asd，
> 且遵守项目既有铁律——**确定性事实与 LLM 判断严格分离**，无证据的结论一律不出现。

## 0. 先说结论：三层里只有两层可测

报告验证的是「Discovery → Activation → Execution 三层是否按预期运作」。
逐层对照 asd 手里真实有的数据：

| 层 | 报告怎么验的 | asd 能不能验 | 依据 |
|---|---|---|---|
| **L1 Discovery** | 扫描 SKILL.md，统计 description 总量 | **能，且更强** | catalog 静态体检（本地文件） |
| **L2 Activation** | 看 Skill 工具被调用才加载 body | **能，已经在做** | `ir/loads.py` 加载台账 |
| **L3 Execution** | scripts/references 按需读取 | **部分能** | `channel="tool:read"` + skill_id 归因 |

**关键约束**（实测真实会话得出，不是推测）：

- kimi-code 桌面版的 `llm.request` 只记 `systemPromptHash` / `toolsHash`，
  **不记 system prompt 正文**。所以「harness 往系统提示词里塞了多少 token」
  只能作为 `system` 桶残差推断，不能作为 fact——这与现有 `calibration.md`
  的结论一致，asd 不应该越过这条线。
- 报告说「Discovery 仅加载 frontmatter ✅ 符合」，依据是**读源码**；
  asd 是**读日志**的工具，拿不到 harness 源码。所以 asd 能验的是
  *披露的成本与后果*，不是*披露的实现*。

这条边界必须写进设计，否则会违反项目已有的事实/推断分离原则。

## 1. 三块能力，按投入产出排序

### A. catalog 静态体检（最高优先级，纯离线，零 LLM）

报告的核心产出就是这块，而 asd 现在**数据全有、没有出口**。
`catalog.py` 已经在扫 SKILL.md，但只提取 name/description 三个字段就丢掉了其余全部信息。

新增 `asd catalog-audit`，输出报告第三、四章的对等物：

**1.1 体量分级**（报告 3.3）
```
行数 / 估算 token 分级：>500 超标 / 200-500 偏大 / <200 OK
按 activation token 排序，超标项置顶
```

**1.2 description 质量分级**（报告 3.2）
```
最优 50-300 字符 / >300 冗余 / <50 过短 / **解析失败（长度≤2，视为缺陷）**
```
最后一项是 asd 独有的：报告只能「提示风险」，asd 能**判定缺陷**——
因为 `_parse_frontmatter` 的行为是自己控制的，可以直接断言。

**1.3 分层有效性比值**（报告 3.1 的 1:13）
```
discovery_cost = Σ estimate(description)          # 始终常驻
activation_cost = Σ estimate(整份 SKILL.md)       # 触发才付
比值越小，分层越有效
```

**1.4 解析健康度**（asd 独有，报告没有）
```
扫描多行 YAML 标量（| / >），统计自身解析器能否正确还原
```
这条直接把上一轮发现的 `make-bot-ui -> '>-'` 类缺陷**变成常驻体检项**，
而不是等到某次审计恰好命中才暴露。

### B. 单会话披露追踪（增量，接在 tokenstats 之后）

单会话里已经能算但**没单独成块**的：

**2.1 首次触发前的 discovery 沉没成本**
```
会话第一轮到第一次 Skill 加载之间，catalog 清单已经在上下文里待了 N 轮
→ 摊到每次的常驻成本 = discovery_cost × 等待轮数
```
报告完全没提这个视角，但它才是「该不该早加载」的成本依据。

**2.2 层级跃迁违例**（真正的渐进式披露违规）
```
Layer 3 内容（scripts/references）在对应 Layer 2 body 之前进入上下文 → 违例
```
现有的 `stub → body` 配对状态机（`ir/loads.py` 的 pending 逻辑）已经能表达
「先有调用标记、后有正文」，只要再记一个 `execution` 观测的时点就能判。
报告只查了「有没有按需」，没查「顺序对不对」。

**2.3 披露税**
```
本会话实际激活的 skill 的 activation token / 全 catalog activation token
= 这次会话为「可能用到」预先付出的代价
```

### C. 跨会话披露趋势（需要落库，最重）

报告 P2 建议的「激活频率 / 激活后成功率 / 识别低效 skill」，
asd 现在只有内存态 fleet，任务重启即失，做不了时间序列。
这块建议**明确推到 v2**，不要在第一版里做半成品。

## 2. 第一版做什么

建议第一版只做 **A（catalog 静态体检）**，理由：

1. **纯离线、零依赖、确定性** —— 完全符合项目「事实与判断分离」的定位，
   不引入任何 LLM 调用，不产生需要证据担保的结论。
2. **数据已在手** —— `load_catalog()` 已经在扫这些文件，改的是「读多少、怎么呈现」，
   不是新增采集。
3. **直接堵住真实缺陷** —— 上一轮验证出的 3 个解析失败 skill、30 个 >200 行的 skill、
   9 个 >500 行的 skill，今天全部无法被任何 asd 命令看见。
4. **它是 B 和 C 的地基** —— 披露追踪的基线（discovery/activation 比值、
   单个 skill 的体量）都来自 catalog 体检的同一套计算。

B 的 2.1/2.2 可以在第二版接着做，因为 2.2 依赖的时点数据
（`execution` 观测已经在 `ir/skills.py` 里产出）已经存在。

## 3. 边界与红线

写进实现约束，避免能力膨胀时破坏项目既有原则：

1. **不越界到 harness 实现**。asd 验证「披露的成本与后果」，
   不验证「harness 怎么实现的」。系统提示词正文不可见就是不可见，
   `system` 桶继续标注为残差推断。
2. **解析失败是 fact，不是 inference**。`_parse_frontmatter` 的行为完全可控，
   description 长度 ≤2 判为解析失败不需要证据担保——它与
   「skill 被压缩挤出」（日志从不记录，只能推断）是两类完全不同的确定性。
3. **静态体检与行为审计分开**。`catalog-audit` 不需要会话目录，
   也不产出任何关于「agent 做得好不好」的结论——那是 `asd <session>` 的职责。
   两者共用 catalog，但报告分开，不合并成一个分数。
4. **阈值要可解释**。报告用的 500 行 / 300 字符是经验值，
   asd 应标注为「约定阈值」而非「规范要求」，并允许覆盖。

## 4. 预期产出形态

```
$ asd catalog-audit
catalog: 116 skills from ~/.agents/skills, ~/.kimi/skills

分层有效性
  discovery 常驻     ~28k tokens  (Σ description)
  activation 潜在  ~275k tokens  (Σ 整份 SKILL.md)
  比值38:1

解析健康度
  ✗ 3 个description 解析失败
      make-bot-ui            '>-'
      obsidian-wissen-backup ''
      web-access             ''

体量超标（>200 行，按 token 排序）
  1718 行  ~11.7k orchestrating-swarms
  ...

description 质量
  >300 字符 32 个 / <50 字符 7 个
```

> **这段示例数字已被实测推翻，保留仅作历史记录。** 实现后按真实的
> 123 个 skill 重算：`discovery=7649`、`activation=286931`、比值 `37.5:1`、
> 解析失败 `1` 个。即 `activation` 与比值对得上，**唯独 `discovery` 差 3.8 倍**，
> 说明 28k 与它自己写的 38:1 本就不自洽（若真是 28k，比值应为 9.8:1）。
> 这正是本项目要标注为 inference 而非 fact 的那类预估数字。

---

## 5. 实现状态（已落地）

第一版按 §2 的结论只做 A（catalog 静态体检），已全部实现：

| 步骤 | 状态 | 位置 |
|---|---|---|
| 1.1 体量分级（约定阈值，可覆盖） | ✅ | `catalog_audit.py` `SizeEntry` / `_size_tier` |
| 1.2 description 质量分级 | ✅ | `catalog_audit.py` `DescEntry` / `_desc_tier` |
| 1.3 分层有效性比值 | ✅ | `audit_catalog()` 的 `discovery_tokens` / `ratio` |
| 1.4 解析健康度 | ✅ | 复用 `catalog.parse_failures()` |
| CLI `asd catalog-audit` | ✅ | `cli.py` `_run_catalog_audit`，`--json` / `--top` / 阈值覆盖 |
| Web 面板 | ✅ | `POST /api/catalog-audit` + 右上角「catalog audit」面板 |

实测 52 个新测试（`test_catalog_audit.py` 19 / `test_cli_catalog_audit.py` 10 /
`test_web.py` 9 + 其余），全量 574 通过。

### 实现时确认的三条约束

1. **token 估算必须走 `ir.estimator.estimate`**，不能直引 `timeline.estimate_tokens`
   ——后者会绕过 `ESTIMATOR_VERSION`，违反「one estimator in the codebase」。
   审计结果里始终打印估算器版本号。
2. **注入条目无正文时不计入 activation**。`_injected_skill()` 无 `body_path` 时
   `path` 是哨兵 `Path("<injected>")`，`read_text()` 必抛 `OSError`；若不显式跳过，
   要么崩溃、要么被算成 0 行从而拉低比值——0 会被读成「这个 skill 很小」。
   现在这类条目进 `no_body` 列表并在输出里显式披露。这正是 §3「不伪造 activation
   token」在实现层的落点。
3. **CLI 靠 `argv[0]` 预判分流**，没有引入 `add_subparsers`。`session` 是位置参数，
   否则 `asd catalog-audit` 会被解析成「一个叫 catalog-audit 的会话路径」。
   保持平铺结构也就不用动现有 `asd <session>` 的参数约定。

### 仍未做（明确推到后续）

- **B（单会话披露追踪）**：2.1 discovery 沉没成本、2.2 层级跃迁违例、2.3 披露税。
  2.2 依赖的时点数据（`execution` 观测）已就位。
- **C（跨会话披露趋势）**：需要落库，v2。
- **L1/L3 的完整验证**：本命令只做 catalog 静态体检，
  **不验证 harness 的披露实现**（§3 红线 1 仍然成立）。

## 待确认（已按默认答案落地）

1. ~~第一版是否就只做 A？~~ **是。** B 的 2.1/2.2 推到下一版。
2. ~~阈值沿用报告经验值？~~ **沿用** 500 行 / 300 字符，但标注为「约定阈值」并全部可覆盖。
3. ~~输出形态？~~ **CLI 文本 + web UI 面板都做了**，JSON 输出作为第三种形态。
