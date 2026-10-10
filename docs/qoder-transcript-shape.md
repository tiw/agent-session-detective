# Qoder Transcript 形状参考

本页是 Qoder agent session 落盘文件的逐字段形状参考，2026-10-08 ~ 2026-10-09
对本工作区及跨工作区样本全量实测归纳（本工作区 6 会话 11263 行；跨工作区
25 会话 / 304 文件）。用途：动 `wire.py`、IR adapter 或任何读取 Qoder
transcript 的代码前先对照本页，避免重新逆向；也是验证 IR spec 字段口径
（FACT 列表、`prefix_hashes=null`、seq 链前提）的依据。

## 文件布局

- 主轨迹（富格式）：`~/.qoder/projects/<workspace 路径编码>/<session-id>.jsonl`，
  每行一个 JSON 对象。
- 伴随目录 `<session-id>/`：`state.json`、`compression-v2/`（压缩快照）。
- 排除：`~/.qoder/logs/sessions/**/segments`（运行时诊断，非审计输入）。

## 三种落盘格式

终端 CLI 与 IDE 是两个产品面，写不同格式。终端 CLI 二进制是
`~/.qoder/entry/qoder`（v1.1.62），Qoder IDE 是独立 Electron app
（transcript `version` 1.1.64）。

| 格式 | 位置 | 写入方 | 特征 |
|---|---|---|---|
| **富格式**（本页主体） | `~/.qoder/projects/<escaped-path>/<uuid>.jsonl` | IDE agent 运行时 | 完整信封 + usage 15 键 + requestTokenAnchor + compact_boundary |
| **中量级** | `~/.qoder/projects/<escaped-path>/transcript/<uuid>.jsonl` | 终端 CLI | type 仅 session_meta/progress/assistant/user；有 sessionId/uuid/cwd；**entrypoint/version/usage 全为 0 条** |
| **薄镜像** | `~/.qoder/cache/projects/<name>-<hash>/conversation-history/<short-id>/<short-id>.jsonl` | 终端 CLI | 每行仅 `{role, message}` 两键 |

同一终端会话双写：cache 薄镜像（112 行）+ `transcript/` 中量级
（1245 行，684 progress / 382 assistant / 168 user / 11 session_meta）；
session_meta 带 `data.meta_type:"slash_command"` 与 skill filePath。

注意两点：

- `entrypoint:"cli"` 指的是 agent harness 运行时而非终端 CLI —— IDE 会话
  照样写它（本工作区 8685 条记录全是该值）。
- 「transcript 在 cache/projects/ 而非顶层 projects/」的说法仅对终端 CLI
  会话成立；IDE 会话直接写顶层富格式。ASD 只读富格式（唯一有遥测的位置），
  终端 CLI 会话喂不满 IR 的 FACT 列。

## 薄镜像 vs 中量级：不同投影，非子集关系

b1d65022 会话精确比对（薄 112 行 vs 中量级 1245 行）：薄镜像 112 个文本块
105 块在中量级里，**7 块独有** —— 全部是运行时注入（slash command 展开
`<command-message>`、召回记忆 `<system-reminder>` UsageGuidance、Stop hook
反馈、current-time/current_open_file）；反向中量级有 163 对
tool_use/tool_result + 114 thinking + 684 progress 是薄镜像没有的。

两者定位：

- **薄镜像 ≈ 喂给模型的 context 投影**（role/message 两键，含注入）。
- **中量级 ≈ harness 事件日志**（工具调用/thinking/hook 台账，无注入文本）。

薄镜像是 CLI 会话唯一记录 context 注入的位置 —— 富格式对应物是 attachment
记录。

### 中量级全量盘点（60 个文件，CLI 内容审计价值）

| 内容 | 数量 | 审计价值 |
|---|---|---|
| tool_use/tool_result 对 | 1881 | 工具调用序列可审计 |
| thinking 块 | 540 | 推理回放 |
| hook_progress 记录 | 5989 | hook 执行台账（PreToolUse guard 等，data.type 全为 hook_progress） |
| session_meta | 80（70 session_info / 8 rules / 2 slash_command） | slash_command 带 skill name+filePath，是 skill 分派证据 |

故 CLI adapter 理论产出 = 对话回放 + 工具调用审计 + hook 台账，但零
「花了多少」—— usage 遥测 0 条，token 治理/context 增长/cache 经济全空。
ASD 否决 CLI adapter 的真正理由是测量层定位错位（喂不进需要遥测的评估），
不是「只有回放」；薄镜像「不进」的理由是盘上仅 4 个（vs 60 中量级，覆盖
6.7%）且为 cache 层易失数据（IDE 把会话历史归为运行时派生缓存非用户资产）。

### 格式判别判据

87 富文件 + 60 CLI 文件全量验证（2026-10-09）。中量级有两种变体 —— 新式
（2026-10 起）带 session_meta + `data` dict 头记录；旧式（2026-05~08）无
session_meta，直接以 assistant 记录开头。

可靠判据：

- 富格式 user/assistant 记录 87/87 带 `parentUuid`/`origin` 信封字段
  （最早见于第 0/2/5 行）。
- CLI 记录 60/60 零信封字段，且含 `progress` 记录（55% 记录数）；富格式
  progress 为 0 条。
- session_meta 的 `data`(CLI) vs `payload`(codex) 区分两种 session_meta 头。

ASD wire 层已按此实现 `_detect_format` 的多记录扫描 + `load_session` 对
qoder-cli 显式拒绝（ValueError）；web 发现层 codex glob 已收紧为
`rollout-*` 前缀（旧 `*/*/*/*.jsonl` 会把 qoder 根下 247 个 subagent 文件
误标成 codex 会话）。

## 富格式逐记录形状

### 信封字段（会话记录共有）

`type, sessionId, timestamp(ISO), uuid, parentUuid, isSidechain, cwd,
userType, entrypoint, version, gitBranch`

uuid+parentUuid 与 isSidechain 在 user/assistant 上 100% 覆盖 → 分别充当
seq 链与子代理归属证据。

### 顶层记录类型（开放枚举）

`user / assistant / attachment / system(subtype=compact_boundary) /
runtime-config / active-leaf / last-prompt / file-history-snapshot /
worktree-state / workspace-directories / relocated`

**勿把种数写死进代码或文档** —— attachment 子类型已经 11→16→18 错过两次。

### user 记录

- `message.content[]`：`text`（真实用户输入；可带顶层 `isMeta: true`）|
  `tool_result`。
- tool_result 记录特有：`sourceToolAssistantUUID`（关联 assistant
  tool_use 的键）、`promptId`、`toolUseResult`（结构化结果）；偶发
  `toolDenialKind / mcpMeta / forkedFrom`。
- 真实人类输入的 user 记录还带：`permissionMode`（如 "auto"）、
  `origin:{kind:"human"}`、`promptId`、`humanInput:{text, mode:"prompt"}`、
  `requestSetId`。`origin.kind` 可作「真人输入 vs 注入」的直接判据，
  `humanInput.text` 是未经 content 块包装的原始输入。

### assistant 记录

- `message.content[]`：`text | thinking | redacted_thinking | tool_use`。
- tool_use 块：`{type, id("fc_" 前缀), name, input:dict}`。
- `message.usage`：15 键全有或全无（本工作区 1660/1660 条）——
  `input_tokens, output_tokens, cache_read_input_tokens,
  cache_creation_input_tokens, cache_creation{ephemeral_1h_input_tokens,
  ephemeral_5m_input_tokens}, server_tool_use{web_search_requests,
  web_fetch_requests}, service_tier, inference_geo, iterations, speed,
  credits, original_credits, billable, request_id, context_usage_ratio`。
- 顶层 `requestTokenAnchor{request, response, requestId}`：与 usage 同现
  （4064/4064）—— 请求/响应内容指纹（全载荷哈希）+ 请求 id。全载荷哈希
  推不出 prefix 身份（IR spec 的 `prefix_hashes=null` 口径仍成立），但可作
  请求级指纹与 request/response 配对证据。

### attachment 记录

`{type:"attachment", attachment:{type, filename?, snippet?, content?,
mtime?}}` + 信封。`attachment.type` 为开放枚举（2026-10-08 全量 304 文件
复测为 18 种），携带真实 skill 正文/压缩/文件活动/MCP 指令增量/记忆正文，
是注入内容的离线证据通道。

首轮实测 11 种：

| attachment.type | 覆盖 | 单次体量 | 对审计的意义 |
|---|---|---|---|
| `skill_listing` | 8/8 | 平均 18,107 字符 ≈ 4,526 token | 可用 skill 目录注入；单会话最多注入 37 次但只有 2 个不同版本 → 重复注入 ≈16 万 token/会话 |
| `invoked_skills` | 4/8 | 带**完整 SKILL.md 正文**，上限截断在 20,000 字符 | 近 25 会话共 264 条 ≈ 50 万 token；真实加载成本可测 |
| `critical_system_reminder` | 8/8 | 平均 6,429 字符 ≈ 1,607 token，单会话最多 18 次 | inject 桶主体 |
| `task_reminder` | 7/8 | 平均 1,564 字符 | inject 桶 |
| `hook_output` | 8/8 | SessionStart hook 会**内联整个 SKILL.md 正文** | 既非 Skill 调用也非 SKILL.md 直读的第 3 条加载通道 |
| `post_compact_restored_files` | 4/8 | 带压缩后恢复的文件路径+内容 | Qoder 压缩是有记录的，压缩遥测可从离线重建 |
| `edited_text_file` | 2/8 | 文件名 + snippet | File Activity 白送 |
| `agent_listing_delta` | 8/8 | 子代理类型清单 | agent network |
| `goal_state` / `auto_mode` / `queued_command` | 少量 | 极小 | mode 事件 |

复测补上的 7 种（全量 304 文件计数）：

| attachment.type | 条数 | 对审计的意义 |
|---|---|---|
| `hook_non_blocking_error` | 80 | hook 执行错误（stderr/exitCode），走 attachment 通道，量级超过 `invoked_skills` |
| `file` | 11 | 带完整文件正文（如 okr_goal.md），file snapshot |
| `relevant_memories` | 5 | 带 memory 文件真实正文（AGENTS.md）—— live 视图 `memory` 分类的离线对应通道 |
| `date_change` | 5 | 极小 mode 事件 |
| `directory` | 3 | `ls` 输出正文 |
| `mcp_instructions_delta` | 2 | 带 MCP 工具说明文本（addedBlocks）—— tools 桶的部分记录来源 |
| `auto_mode_exit` | 1 | mode 事件 |

### system 记录

`subtype: "compact_boundary"` → `compactMetadata{trigger, preTokens,
postTokens, messagesSummarized, durationMs}` + `logicalParentUuid` +
`compactBoundaryUuid`。另有带 `isCompactSummary` 标记的压缩摘要记录
（本工作区 128 条）。

### runtime-config 记录

`{type, sessionId, model, reasoningEffort, contextWindow, generation,
timestamp(int)}` —— contextWindow 可为 null（实测值 128k/200k/272k）。

### active-leaf 记录

`{leafUuid, explicit}` —— 会话树分支/重定位。

## 维护约定

- 顶层记录类型与 `attachment.type` 均按**开放枚举**处理，计数不写死进
  代码或文档。
- 新出现的字段（如 requestTokenAnchor 类）先实测覆盖率，再决定进
  FACT/EST 口径。
- 本页数字是 2026-10-08/09 的实测快照，用于说明形状与量级，不是不变量；
  复测时更新数字与日期。
