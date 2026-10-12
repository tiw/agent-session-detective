# Upload session files — design

Date: 2026-10-12
Status: approved

## Problem

ASD 只能审计本机扫描发现的会话。拿到别人机器导出的 transcript（典型：`Downloads/53e08e97-…-木晓-20261010.jsonl`）只能走 CLI 指路径；web 应用没有入口。目标：web 应用支持上传 jsonl 文件，上传后的会话与扫描发现的会话**同构地被分析**（sidebar、审计表单、报告、skill tree 完全一致）。

上传文件通常是另一种落盘形状：**sidechain 记录内联在主文件里**，join key 是 snake_case 的 `parent_tool_use_id`（本机富格式拆 `subagents/*.jsonl`、无此键）。当前 wire 层只读 camelCase，导致这类文件 11 条 dispatch 全部 orphan。因此保真修复一并纳入本变更。

## Decisions（已与用户确认）

1. 存储：`~/.local/share/agent-session-detective/uploads/`，持久化，不与 `~/.cache` 结果缓存混放
2. 呈现：合并进 sidebar，带 `upload` 徽标 + 行内删除按钮
3. 范围：包含 dispatch→子代理 join 修复（上传文件正是该形状）

## 1. 存储与命名

- 常量 `web.UPLOADS_DIR = Path("~/.local/share/agent-session-detective/uploads").expanduser()`，测试 patch（同 `CACHE_DIR` 模式）
- 存储名 = 原文件名净化：去路径分隔符/控制字符，长度截断 ~120，强制 `.jsonl` 后缀。同名不同内容时追加 `-2` 等序号
- **保留原名而非内容寻址**：uuid 前缀留在文件名里，`session_uuid_from_source` 才可能命中本地 SharedClientCache（自己机器导出的会话重传时可接 billed/IDE-DB 侧通道）
- 清单 `uploads.json`（与文件同目录）：`{存储名: {original, sha1, uploaded_at, size}}`
  - 上传时流式写临时文件并算 sha1；清单中已有相同 sha1 → 去重命中，返回既有会话条目，不产生重复行/重复文件

## 2. API（stdlib http.server，不引 multipart）

- `POST /api/upload?name=<urlencoded 原文件名>`，body 为原始字节
  - 流式写临时文件（上限 1GB，超出 413）
  - `load_session` 试解析：0 事件或异常 → 400（删临时文件）。同时天然挡掉不属任何已知格式的垃圾文件
  - 成功 → 落位 uploads 目录 + 清单，响应 `{session: {id, path, workspace, mtime, uploaded: true}, dedup: bool, bytes: n}`
- `DELETE /api/upload?path=<...>`
  - 校验：resolve 后父目录 == `UPLOADS_DIR` 且后缀 `.jsonl`（防路径穿越），不在清单 → 404
  - 删文件 + 清单项。`~/.cache` 里该 path 的旧审计缓存不清：fingerprint 按路径+mtime+size，天然失配

## 3. 发现与 UI

- `discover_sessions` 末尾追加扫描 `UPLOADS_DIR/*.jsonl`：source_key `upload:<resolved path>`，条目带 `uploaded: true`，workspace 显示 `upload`；fleet 聚合同样包含
- sidebar 头部 upload 按钮（隐藏 `<input type="file" accept=".jsonl">`）+ 整个 sidebar 拖拽上传
- 上传行：`upload` 徽标 + × 删除按钮（`stopPropagation`，不触发 openAudit）；行 `title` 显示完整路径
- 上传成功：刷新 session 列表并直接 `openAudit` 选中该会话
- `/api/tree`、审计表单、报告、缓存机制零改动（全链路只认 path）

## 4. 分析保真修复

1. **dispatch join**：`wire.py _qoder_ref` 的 `parent_tool_use_id` 改为 camelCase 优先、snake_case 兜底双取。带内 join key（`Agent` tool_use id ↔ sidechain `parent_tool_use_id`）恢复，dispatch 不再 orphan
2. **uuid 前缀解析**：`billing.session_uuid_from_source` 接受「以 uuid 开头的 stem」（如 `53e08e97-…-木晓-20261010`），返回前导 uuid；`ide_db` 若有独立派生则改走同一 helper
3. **IR_VERSION 1.7 → 1.8**：join 结果变化，按既有机制（fingerprint 含 `ir<IR_VERSION>`）让 web 结果缓存自失效；web 侧 v11 计数不动

## 5. 错误处理

- 非法 jsonl / 0 事件 → 400 + 解析错误信息
- 文件名缺 `name` 参数 → 生成 `upload-<timestamp>.jsonl`
- 二进制/编码异常 → `load_session` 0 事件 → 400
- 上传目录按需创建

## 6. 测试（TDD）

- `test_wire`：`_qoder_ref` 双 casing 取值；内联 sidechain fixture 端到端（`dispatch.subagent_agent_id` 非 null）
- `test_billing`：木晓式名字的前导 uuid 解析
- `test_web`：上传落盘 + 清单、sha1 去重、文件名净化（含 `../` 穿越拒绝）、无效文件 400、DELETE 守卫（目录外路径 404/400）、发现合并 + `uploaded` 标记
- 手工：起 `--serve`，上传木晓文件，验证 sidebar 出现、审计可跑、tree 显示 11 条派发已 join

## 7. Out of scope

- kimi 会话目录上传（目录结构，需 zip）
- 删除时清缓存条目、上传鉴权（服务仅绑 127.0.0.1）
- 清单的编辑/重命名管理
