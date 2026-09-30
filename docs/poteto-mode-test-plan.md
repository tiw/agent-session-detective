# poteto-mode 路由合规测试方案

测试目的：验证 kimi-code 在 poteto-mode 下是否按其路由规则加载 skill 和 principle，用 agent-session-detective（asd）在另一个窗口做事后审计。

关键前提：

- ~~`poteto-mode` 装了 `disable-model-invocation: true`，模型不能自动触发~~ **2026-09-29 已移除**：桌面版把 `disable-model-invocation: true` 的 skill 整个排除在注册表外（`/poteto-mode` 报 40415 即此因），而 poteto-mode 的路由目标（how/why/architect/principle-*/tdd/unslop 等 45 个）全都带这个标记，不移除则整条路由链不可用。已批量移除并备份在 `/tmp/pstack-skills-backup-20260929.tgz`（回滚：解压覆盖回 `~/.agents/skills/`）。副作用：这些 skill 恢复模型可自动触发，日常会话有被自动加载的可能。
- **必须开新窗口测试**：skill 清单在会话启动时绑定（profile.bind），旧会话（含报 404 的 session_e11e331b）不会刷新。
- 读 `playbooks/*.md` 是路由的**正常机制**（router 明文要求 "open its file"）；读 `SKILL.md` 绕过 Skill 机制才是问题信号。审计时两者分开看。
- `deslop` 在 Non-negotiables 里被要求（"Before commit → the deslop skill"）但**未安装**，相关项预期 MISSING，属已知目录缺口而非路由失败。

## 用例

## 靶场应用

`/Users/ting/work/poteto_test/target/` 是一个纯标准库的迷你任务 API（git 仓库，tag `origin-state` 标记初始状态）：

- **植入 3 个 bug**（T1 材料）：B1 `GET /items/999` 返回 500 而非 404；B2 `POST /items` 读错 key 导致 title 恒为 `untitled`；B3 `DELETE` 删的是邻居。
- **3 个待加 feature**（T2 材料）：F1 `GET /health`；F2 `PATCH /items/<id>`；F3 `?sort=priority`。
- `tests/test_api.py` 就是规格：绿灯 3 个（必须保持）、bug 红 3 个、feature 未实现 3 个。被测 agent 修到全绿即实现达标，无需人工判断。
- 每轮测试后重置：`git reset --hard origin-state`。

### T1 bugfix（核心用例）

新窗口开在 `/Users/ting/work/poteto_test/target`，输入：

```
/poteto-mode 这个 API 有 bug，tests/test_api.py 里 test_bug1/2/3 三个用例红了。修好它们，按你的流程来。
```

预期路由链（bug-fix.md + Non-negotiables）：

| 预期 | 来源 |
|---|---|
| Poteto Mode | 用户显式调用 |
| playbooks/bug-fix.md 读取 + 步骤逐字进 TodoList | router 机制 |
| how | step 2 种子假设 |
| why | step 2 回归历史 |
| architect | step 3（若跨函数边界） |
| principle-fix-root-causes | 调试 |
| principle-model-the-domain | 任何代码 |
| tdd | step 5（有廉价本地测试路径时） |
| principle-sequence-verifiable-units | step 5 明文 |
| opening-a-pr playbook | step 6 |
| principle-prove-it-works | 收尾验证 |
| unslop | 任何回复面 |

判定要点：先复现再修（step 1）；TodoList 前几项是 playbook 步骤原文；跳过步骤须有 `skip: <原因>`。

### T2 feature

新窗口开在 `/Users/ting/work/poteto_test/target`（先 `git reset --hard origin-state` 复位），输入：

```
/poteto-mode tests/test_api.py 里 test_feature1/2/3 三个功能还没实现：GET /health、PATCH /items/<id>、?sort=priority。加上它们，按 feature playbook 走。
```

预期：Poteto Mode、playbooks/feature.md、how、architect、arena（实现存在多个合理形状时）、principle-model-the-domain、principle-sequence-verifiable-units、opening-a-pr、unslop。判定要点：step 3 的吞吐检查点四个 todo 项（不适用的维度标 `n/a:` 而不是删除）；每个写文件的 delegate 有自己的 worktree。

### T3 investigation（只读，防过度路由）

```
/poteto-mode 调查一下 target/app.py 里 DELETE /items/<id> 为什么删错对象，只要结论和证据，不要改任何代码。
```

预期：Poteto Mode、playbooks/investigation.md。预期**不出现**：architect、tdd、opening-a-pr。此用例抓"路由过度"（该加载的没加载对称的问题：不该加载的乱加载）。

### T4 negative control（不触发）

新窗口，**不要**提 poteto-mode：

```
<项目里的一个纯 casual 问题，例如"这个文件是干什么的">
```

预期：Poteto Mode 不出现（`disable-model-invocation` 应阻止自动触发）。此用例抓误触发。

## 审计协议

每个用例跑完后先做**实现达标验证**（机械检查，不用人判断）：

```bash
cd /Users/ting/work/poteto_test/target
python3 tests/test_api.py        # T1: test_bug1/2/3 转绿且绿灯不灭；T2: 全绿
git diff --stat origin-state     # 改动是否最小（Laziness Protocol）
```

然后做**路由审计**（会话目录示例 `~/.kimi-code/sessions/wd_poteto_test_*/session_*/`）：

```bash
cd /Users/ting/work/agent_session_detective
PYTHONPATH=src python3 -m agent_session_detective <会话目录> --no-judge \
  --expect "Poteto Mode,how,why,architect,principle-fix-root-causes,principle-model-the-domain,tdd,principle-sequence-verifiable-units,principle-prove-it-works,unslop" \
  --open
```

（T2/T3/T4 按各自预期清单调整 `--expect`。）

报告核对顺序：

1. **Expectations 表**：MISSING 项是路由没按规则走的第一信号。
2. **SKILL.md Read as Plain Files**：任何非 Poteto Mode 的 SKILL.md 直读 = 绕过机制的 workaround（上一个会话的 failure mode）。
3. **Skill Lifecycle**：加载时点是否在做相关步骤之前，而不是事后补读。
4. **Findings**：开了 judge 时看 missed trigger 反查（T3/T4 的反向验证）。

TodoList 逐字核对（机械化）：在报告 Event Feed 里找 TodoList 工具调用，比对前几个条目与 playbook 步骤文本。这步暂不自动化。

## 记录

每轮测试把结论记在会话目录名的旁边：日期、用例、MISSING 项、是否为目录缺口（如 deslop）。积累几轮后区分三类问题：路由没走（模型问题）、skill 没装（目录缺口）、规则本身有歧义（playbook 描述问题）。

### Round 1（2026-09-29）— T1 bugfix

会话：`~/.kimi-code/sessions/wd_poteto_test_52f213ee78da/session_d3eb4a59-8402-43be-8a00-bf8cb4991fd3`

**实现：通过。** 3 bug 全修（各有 curl 前后证据），3 原绿保持，diff 4 增 3 删，提交 `109e80e`。

**路由：顶层合规，中层静默丢链。**

- ✅ Poteto Mode 经 Skill 工具正式加载（`/skill:Poteto Mode`）；bug-fix playbook 6 步进 TodoList；step 6 正确 `skip: 无 remote`。
- 🟡 4 个 principle（fix-root-causes、laziness-protocol、prove-it-works、sequence-verifiable-units）以读文件方式加载——SKILL.md 原文即 "Read the leaf skill in full"，灰色地带，暂不判违规。
- ❌ how / why / architect / tdd 全程 MISSING 且无 skip 说明。模型把三个一行 bug 判为 trivial，跳过了 step 2/3/5 明文要求的技能链——playbook 只授权"跳过要标 skip"，未授权这层裁量。
- 回复格式合规（结果→根因→验证→流程决策，principle 逐条引用）。

**归因：模型裁量越界（第一类）。** playbook 中层步骤的"按需加载"没有强制机制，靠模型自觉——这是路由体系的结构性弱点，比单个 skill 没装更值得记录。

### Round 2（2026-09-29）— T2 feature

会话：`~/.kimi-code/sessions/wd_target_fe501ec3a1a5/session_ad4c9229-25cb-4afc-ab27-854a520e70c0`

**实现：通过。** 4 个 feature 测试全绿（含 PATCH 404 边界），3 原绿保持，3 bug 测试如实声明"刻意保留"未动；diff +30/−2 未提交（agent 声明"说一声就 commit"，git status 证实）。

**路由：明显好于 T1，skip 纪律到位。**

- ✅ Poteto Mode 正式加载；feature playbook 8 步进 TodoList（基本逐字）。
- ✅ **5 个跳过步骤全部带 `skip: <原因>`**（how：文件已全文读、hook 点明显；architect：单文件单形状；commit：用户未要求；interrogate：设计无争议；PR：无 remote）——这正是 playbook 明文授权的模式（"stays in the list with a one-line skip: <reason>"）。T1 的违规是跳过**无**说明，本轮同类裁量**有**说明即合规。
- ✅ step 4 delegate 真实发生（agent-0 实现，main 逐行复核 diff）；验证自己跑的非转述。
- ❌ step 3 吞吐检查点未展开为 4 个 todo 项（playbook 要求逐项保留、不适用标 `n/a:`），只在回复里回顾了四个维度——半合规。
- 🟡 delegate 未给独立 worktree（共享目录 Edit；单文件串行工作，影响小）。formal load 后又直读了一次 poteto-mode 的 SKILL.md（良性）。

**归因：规则理解基本到位（比 R1 好），残余偏差在检查点展开这类形式要求（第三类，规则本身偏繁琐 vs 模型倾向概括）。**

**跨轮趋势：** T1 的无声丢链 → T2 的带理由跳过。同一模型的行为方差很大，"中层链遵守度"目前不可预测，需要更多轮次才能判断是否为稳定改进（比如提示词里 feature 的 skip 表述更醒目）。

### Round 3（2026-09-30）— A/B：kimi vs codex 同题 bugfix

环境：`/tmp/ab/{kimi,codex}-target`（同一初始提交的独立副本，排除互相干扰）。题目与 T1 相同。基础设施事故：两臂首跑同遭 provider 429（共享后端过载），重跑后完成。

**kimi-cli（`-p` 无头模式）：skill 全程缺席。** CLI 的 skill 清单是独立的注册表缓存（51 个，与桌面版修复前的旧清单一致），手动恢复的 45 个 pstack skill 不在其中。`--skills-dir ~/.agents/skills` 也无法注入。模型把 `/skill:Poteto Mode` 当无效文本忽略后裸跑：3 bug 全修、自测通过、范围声明诚实（exit 0）。结论：kimi 桌面版的 skill 注册表修复没有传导到 CLI 进程，CLI 走自己的发现/校验路径（待查）。

**codex（`codex exec` + 显式指针）：流程最显式的一轮。**

- ✅ 先读 SKILL.md 全文（4 次 cat/sed），随后明确声明「Playbook match: this is a Bug fix」再读 bug-fix.md——playbook 匹配是显式说出来的
- ✅ 先复现后修（「the playbook requires runtime evidence first」），修复前后各跑一次全套，最后对 3 个 bug 用例单独隔离验证
- ✅ 读了 principle-fix-root-causes；回复带 Principles applied 段（fix-root-causes、laziness-protocol，叶文件均已读）
- ✅ 两个 skip 都带理由（delegate：90 行文件不值得；commit staging：用户未要求）
- ❌ how/why 未加载（与 kimi T1 相同的中层丢链）；无 todolist 工件（无工具也未用 todo.md fallback）
- 实现：3/3 修复，diff 4 增 3 删，与 kimi T1 的 diff 行数完全一致

**三方对比（同题）：**

| 维度 | kimi T1（桌面，skill 加载） | kimi-cli（skill 缺席） | codex（skill 加载） |
|---|---|---|---|
| 实现结果 | 3/3，diff 4+/3- | 3/3，diff 4+/3- | 3/3，diff 4+/3- |
| playbook 匹配 | 隐式（todolist 步骤体现） | 无 | **显式声明** |
| 步骤工件 | ✅ TodoList 6 步 | 无 | 无 |
| skip 理由 | 1/3 处（step 6） | n/a | 2/2 处 |
| principle 加载 | 4 个 | 0 | 1 个 |
| how/why 中层 | ❌ | n/a | ❌ |

**归因。** 对三个一行 bug 这种规模，skill 加成的体现在过程严谨性（证据链、skip 声明、可审计工件）而非结果质量——三轮实现结果完全相同。中层链（how/why/tdd）在两个 harness 里都没被加载，是模型对 trivial 任务的稳定裁量，不是 harness 差异。codex 的回复显式度最高（匹配声明 + skip 段），kimi 的工件保真度最高（todolist 逐字步骤）。下一轮拉开差距需要更复杂的题目（跨文件 bug、需要真 delegate 的规模）。
