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
