# 人工注入 skill 原文 —— 设计草案

> 输入方把「会话涉及的 skill 的原文 / 目标」提供给 asd。
> 这份输入同时解掉三个当前的能力缺口，是整个设计里性价比最高的一块。

## 1. 为什么这份输入价值高

它不是一个 feature，是**一把钥匙**——同时打开三扇门：

| 缺口 | 现状 | 注入原文后|
|---|---|---|
| **触发依据失真** | `catalog.py` 解析出 `'>-'`，judge 拿到空路由依据 | judge 拿到真实描述，反事实判断有依据 |
| **catalog 覆盖不到运行时 skill** | 13/13 运行时可见 skill 不在 catalog 里 | 按会话实际涉及的 skill 精确补齐 |
| **context 混淆无证据** | 报告只能推测「68 个 description 可能分散注意力」 | 能问「本次会话里，这批同时在线的 description 是否互相竞争」 |

第三条是报告 P2「引入信噪比量化指标」落地的**唯一现实路径**——
asd 拿不到 harness 注入了什么，但可以拿到「注入了哪些 skill 的什么描述」。

## 2. 接入点

已有三个位置可以直接吃这份输入，都不需要改调用链：

```
catalog.py: load_skill_file()          → 覆盖 description / trigger_excerpt
judge.py:   PROMPT_TEMPLATE{trigger}   → 反事实判断的路由依据
report.py:  expectations / findings    → 证据链来源
```

最小实现是让 `Skill` 支持一个 `source` 字段，标明这条记录是
`"catalog"`（本地扫到的）还是 `"injected"`（人工提供的），让报告能区分两者。

## 3. 三种注入形态（按成本递增，建议按需选）

### 形态1：覆盖（override）—— 最低成本

一份 JSON，按 skill 名覆盖 catalog 里的 description：

```json
{
  "library": {
    "description": "Route here when users create, generate, beautify, upload, edit, share, organize, publish, download, search or retrieve HTML pages, docs, tables/CSV, drive files, web links, images, attachments or audio transcripts."
  }
}
```

- **解决**：触发依据失真（`'>-'` 那类）
- **不解决**：catalog 里根本没有的 skill

### 形态 2：补充（supplement）—— 覆盖 catalog 缺失的 skill

同一份 JSON，未在 catalog 中的 key 直接**新增**一条 `Skill`：

```json
{
  "cloud-service": {
    "description": "Sites and mini-programs need a backend, service or database...",
    "body_path": "~/.workbuddy/.../cloud-service/SKILL.md"
  }
}
```

- **解决**：catalog 覆盖缺口。`body_path` 可选——没有正文就只有描述，
  仍可用于路由判断，但不参与体量统计（不伪造 activation token）。

### 形态 3：目标（intent）—— 最强但需要人工判断

除了原文，再补一句「这个 skill 是为了解决什么」：

```json
{
  "wechat-mp-publish": {
    "description": "...",
    "intent": "把 Markdown 发布到微信公众号草稿箱，自动排版"
  }
}
```

`intent` 只进judge 的prompt，**不进 report 的事实区块**——
它是人的判断，必须与日志事实分开标注。

## 4. 一条硬约束：注入内容不能冒充事实

这条必须写进实现，否则会破坏项目最核心的设计：

```
✓ description / body_path  → 可以当 fact用（有原文出处）
✓ intent                → 是人的判断，report 里必须标为 inference
✗ 不得把注入的 description 反写成「日志显示 agent 看到了这段」
```

项目已有先例：`calibration.md` 里的live oracle同样不被当作运行时依赖。
注入数据是**外部输入**，来源必须始终可见。

实现上：`Skill` 加 `source: str = "catalog"`，报告里凡是 `source="injected"`
的记录，渲染时带上「人工注入」标记，与扫到的 catalog 记录视觉区分。

## 5. 与 catalog-audit 的关系

两者互补，且注入数据能**反向体检 catalog**：

```
asd catalog-audit --injected truth.json
```

对同一批skill同时展示「catalog 解析出来的 description」和「人工提供的原文」，
不一致的地方高亮。这就把上一轮发现的解析 bug 变成了**常驻可查的对照表**——
比一次性手查可靠得多。

## 6. 实现状态

第1–3 步已落地（`catalog.py` + `cli.py` + `judge.py`），507 tests 通过。

| 步骤 | 状态 | 说明 |
|---|---|---|
| 1 `Skill` 加 `source` / `intent` | ✅ | 另加 `description_parse_failed`，把解析缺陷变成可查询状态 |
| 2 `load_injected` + `merge_catalog` | ✅ | 同名覆盖、大小写不敏感、畸形条目跳过而非抛错 |
| 3 `--injected PATH` 进 CLI | ✅ | web 侧（上传/表单）尚未接 |
| 4 报告里视觉区分 injected | ⬜ | 字段已就位，渲染未做 |
| 5 `catalog-audit --injected` 对照高亮 | ⬜ | 待catalog-audit 本体 |

实测（116 扫描 + 2 注入）：

```
make-bot-ui   description '>-'  →  注入的真实路由文案
              parse_failed 3 → 2（补掉1 个，剩 2 个未注入）
cloud-service catalog 里没有  →  新增进来，source=injected
```

### 已定的两个语义（实现中确认）

- **覆盖优先于并列**：注入条目替换同名 catalog 条目，理由是提供原文的目的
  就是纠正解析器，而不是和解析结果并排显示。
- **畸形输入降级而非报错**：JSON 坏掉、条目既无 description 又无 body_path、
  `body_path` 指向不存在的文件——都只丢弃该条目，审计继续。
  缺席会表现为「少了一个skill」，本身可见。

---

## 待确认

1. web 侧要不要接（上传JSON / 表单粘贴）？
2. 报告里 injected 条目的视觉标记，跟哪种风格一致？
3. 剩下 2 个解析失败的 skill 要不要一并注入（或者干脆修 `_parse_frontmatter`）？