/* agent session detective web ui */
(function () {
  "use strict";

  var statusLine = document.getElementById("status-line");
  var sessionList = document.getElementById("session-list");
  var auditTitle = document.getElementById("audit-title");
  var auditForm = document.getElementById("audit-form");
  var progress = document.getElementById("progress");
  var stepsBox = document.getElementById("steps");
  var report = document.getElementById("report");
  var treeLink = document.getElementById("tree-link");

  var currentSession = null;
  var pollTimer = null;

  // ------------------------------------------------------------------
  // section help: what each block means and how to read it
  // ------------------------------------------------------------------

  var HELP = {
    "inject":
      "手工提供 skill 的真实原文，JSON 格式。用于两种情况：一是修正解析失真（多行 YAML " +
      "标量、缺 description），二是纳入扫描目录里根本找不到的 harness 内置 skill。" +
      "同名条目会覆盖 catalog 里的解析结果——提供原文的目的就是纠正它。" +
      "intent 是你对「这个 skill 是干什么」的说法，会给 judge 做参考，但报告里会标成" +
      "人工提供，不会冒充日志事实。",
    "dirs":
      "追加扫描的 skill 目录，每行一个。用于把运行时真实可用的 skill 纳入审计范围——" +
      "默认只扫 ~/.agents/skills 和 ~/.kimi/skills，很多 harness 内置 skill 不在其中。" +
      "每个目录下的 */SKILL.md 会被自动发现，catalog 里重名的以先到者为准。" +
      "留空则只用默认目录。",
    "expect":
      "列出这个场景按路由规则应该被消费的 skill。loaded = 经 Skill 工具正式加载；" +
      "file-read = SKILL.md 被当文件读过（kimi-code 下属于绕过机制，codex 下是正规加载方式）；" +
      "missing = 两者都没有，是路由没走的第一信号。",
    "playbook":
      "选一个 playbook 文件做指令遵循度（IF）评估。逐步对照会话轨迹判 covered / partial / skipped，" +
      "只认实际行动和结果，不认计划或声称。覆盖率 = 加权均值（1.0 / 0.5 / 0.0），0.75 为门槛。",
    "judge":
      "让 LLM 扫描全部未消费的 skill，反事实回答哪些本该触发却没触发。每条结论必须附原文逐字证据，" +
      "约 10 分钟。判定是判断不是事实，证据才是可核对的部分。",
    "billed":
      "接入 Qoder 本地 SharedClientCache 的账单 token 序列（只读）。CLI 形会话的 transcript 无遥测，" +
      "勾选后每轮上下文柱状图从账单 prompt 序列重建，压缩点也从账单锯齿识别；" +
      "账单数字是会话级口径，不与日志侧 token 混算。",
    "summary":
      "这次审计的硬数字：会话轮数、事件总数、skill 正式加载与直读次数、本地 catalog 规模、" +
      "judge 是否可用、审计耗时。",
    "findings":
      "本该触发而从未加载的 skill。判定 = LLM 反事实推理（标注为判断）；证据 = 从会话原文逐字引用（可核对）。" +
      "无证据或证据非原文的判定会被丢弃并计为工具缺陷。缺陷多时先怀疑解析器，而不是 agent。",
    "if":
      "所选 playbook 每个编号步骤的执行判定。covered = 轨迹中有实际行动或结果；" +
      "partial = 只做了一部分；skipped = 未执行。注意：带 skip 理由的跳过仍然计 skipped，" +
      "它度量步骤执行度，不度量裁量是否合规——后者看 expectations。覆盖率由判定重算，不信模型自报。",
    "lifecycle":
      "skill 的完整旅程。loaded = 经 Skill 工具正式加载，展开可见加载时捕获的内容快照，" +
      "之后 skill 文件再改也不影响本报告。file-read = SKILL.md 被当普通文件读，未走 Skill 机制。" +
      "evicted? = 压缩事件之后加载的内容可能被挤出上下文（推断，日志从不记录这件事）。",
    "feed":
      "原始事件流，最近 400 条，按时间排序。角色 badge：user = 人的输入，think = 模型思考，" +
      "say = 模型给你的回复，tool = 工具调用，result = 工具返回，compact = 上下文压缩。" +
      "[subagent:x] 标记表示事件来自子代理。点筛选条上的角色只显示该类，再点一次恢复全部。",
    "tokens":
      "token 治理四指标，全部从日志确定性抽取：缓存命中率（usage.record / token_usage，" +
      "全 agent 合计）；每轮增量形状（linear = 每轮加恒定 token，sublinear = 增量在缩，" +
      "accelerating = 增量在涨）；六桶分解（system 桶是残差推断；skill = 技能正文，" +
      "inject = harness 包裹进你输入里的内容，tool = 其余工具结果，都按事件内容估）；" +
      "prompt 哈希翻转（llm.request 的 systemPromptHash/toolsHash 中途变化 = 前缀缓存失效）。" +
      "点击柱子的柱子可以下钻：该轮每个桶各加了多少、注入的具体是什么内容。",
    "loads":
      "IR 1.1 证据化加载台账：stub（ Skill 工具的返回占位）与随后的正文出现按序配对，" +
      "配上的行才给出成本 ~N (EST)；没有等到正文的行记 unavailable，只留 marker 不留猜测。" +
      "reload = 压缩后正文重新出现；redundant bodies 是连续重复正文，只计数不产生行。" +
      "这里是全报告唯一出现加载成本数字的地方。",
    "library":
      "声明 vs 观测：catalog 的 requires 声明和日志里的真实共加载对比。共加载 = 同一 agent 作用下" +
      "两个 skill 都被挂载（body / stub / execution；仅 listing 不算），权重 w = 出现该组合的 agent 数。" +
      "一致 = 声明且观测到；过度声明 = 声明了但从未观测到；隐式 = 观测到但没声明，w 达到阈值才计入" +
      "（单次出现视为巧合）。观测 id 常带命名空间前缀（superpowers:brainstorming），仅当能唯一归一到" +
      "声明名时才参与对比——歧义的不猜，保持原样并单独列出。",
    "bill":
      "双账单：成本行回答 tokens 废在哪，路由行回答该加载的加载了吗。" +
      "行等级：事实 = 渠道直接测量；观测 = provider 遥测；证据 = 配对台账；推断 = 估算。" +
      "share 只在 EST 口径小计内计算（上下文增长/重复注入税/技能加载成本/subagent 返回），" +
      "输出与缓存率不入分母；无分母则不显示百分比。subagent 行可展开查看臃肿派发明细。",
    "gaps":
      "SKILL.md 期望 × IR 实际的差距。missed triggers = LLM 反事实判定「该触发却没触发」" +
      "（是判断不是事实，证据须逐字可核对）；loaded but never executed = 正文进了上下文却从未执行；" +
      "IR-only = 会话里出现但磁盘 catalog 找不到的 skill（版本或命名漂移信号）。" +
      "unjudged 计数保持透明：预过滤剔除与超限未判的都不冒充已判。"
  };

  var TEMPLATES = {
    "bugfix": {
      expect: "Poteto Mode,how,why,architect,principle-fix-root-causes,principle-model-the-domain,tdd,principle-sequence-verifiable-units,principle-prove-it-works,unslop",
      playbook: "poteto-mode/playbooks/bug-fix.md"
    },
    "feature": {
      expect: "Poteto Mode,how,architect,arena,principle-model-the-domain,principle-sequence-verifiable-units,principle-prove-it-works,unslop",
      playbook: "poteto-mode/playbooks/feature.md"
    }
  };

  function helpDot(key) {
    if (!HELP[key]) return "";
    return '<span class="help-wrap"><button class="help-dot" type="button" data-help="' +
      key + '">?</button><div class="help-pop">' + esc(HELP[key]) + "</div></span>";
  }

  // delegated toggle: one popover open at a time
  document.addEventListener("click", function (ev) {
    var dot = ev.target.closest ? ev.target.closest(".help-dot") : null;
    var wraps = document.querySelectorAll(".help-wrap.open");
    if (!dot) {
      wraps.forEach(function (w) { w.classList.remove("open"); });
      return;
    }
    ev.stopPropagation();
    var wrap = dot.parentNode;
    var wasOpen = wrap.classList.contains("open");
    wraps.forEach(function (w) { w.classList.remove("open"); });
    if (!wasOpen) wrap.classList.add("open");
  });

  // fill static help dots (audit form) from the same HELP table
  document.querySelectorAll(".help-dot[data-help]").forEach(function (dot) {
    var key = dot.getAttribute("data-help");
    var pop = dot.parentNode.querySelector(".help-pop");
    if (pop && HELP[key]) pop.textContent = HELP[key];
  });

  // template chips: one-line intent, form stays editable
  document.querySelectorAll(".chip[data-template]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var t = TEMPLATES[chip.getAttribute("data-template")];
      if (!t) { // clear
        auditForm.elements.expect.value = "";
        auditForm.elements.playbook.value = "";
        auditForm.elements.dirs.value = "";
        auditForm.elements.inject.value = "";
        scanDirs();
        checkInjected();
        return;
      }
      auditForm.elements.expect.value = t.expect;
      auditForm.elements.dirs.value = t.dirs || "";
      scanDirs();
      var select = auditForm.elements.playbook;
      for (var i = 0; i < select.options.length; i++) {
        if (select.options[i].value.indexOf(t.playbook) !== -1) {
          select.value = select.options[i].value;
          break;
        }
      }
    });
  });

  // skill dirs: report what each path contributes before the run is committed,
  // so a typo is distinguishable from a directory that simply has no skills.
  var dirTimer = null;
  function scanDirs() {
    var box = document.getElementById("dir-status");
    if (!box) return;
    var dirs = collectDirs();
    if (!dirs.length) { box.textContent = ""; box.className = "dir-status dim"; return; }
    box.textContent = "扫描中…";
    box.className = "dir-status dim";
    fetch("/api/scan-dirs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dirs: dirs }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      var entries = (data && data.dirs) || [];
      var bad = entries.filter(function (e) { return !e.exists; });
      var empty = entries.filter(function (e) { return e.exists && !e.skills; });
      var parts = entries.map(function (e) {
        return e.exists
          ? e.path.replace(/^\/Users\/[^/]+/, "~") + " → " + e.skills + " 个 skill"
          : e.path + "（不存在）";
      });
      if (bad.length) parts.push("⚠ " + bad.length + " 个目录不存在");
      box.textContent = parts.join(" · ");
      box.className = "dir-status" + (bad.length || empty.length ? " warn" : " ok");
    }).catch(function () {
      box.textContent = "";
      box.className = "dir-status dim";
    });
  }

  auditForm.elements.dirs.addEventListener("input", function () {
    clearTimeout(dirTimer);
    dirTimer = setTimeout(scanDirs, 400);
  });

  auditForm.elements.inject.addEventListener("input", checkInjected);

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function fmtTs(ts) {
    if (!ts) return "?";
    if (ts > 1e12) ts = ts / 1000;
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function badge(cls, text) {
    return '<span class="badge ' + cls + '">' + esc(text) + "</span>";
  }

  function setStatus(text) {
    statusLine.textContent = text;
  }

  // ------------------------------------------------------------------
  // sessions sidebar, always visible
  // ------------------------------------------------------------------

  function loadSessions(query) {
    var q = (query || "").trim();
    setStatus(q ? "searching all sessions for “" + q + "”…" : "scanning sessions…");
    var url = "/api/sessions" + (q ? "?q=" + encodeURIComponent(q) : "");
    return fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      // sidebar is discovery-only: turns/skills/token stats are computed
      // on demand by the audit job, not here. fleet stays hidden until
      // the fleet button asks for it.
      document.getElementById("fleet").hidden = true;
      sessionList.innerHTML = "";
      if (!data.sessions.length) {
        var empty = document.createElement("div");
        empty.className = "session-empty";
        empty.textContent = q
          ? "no session matches “" + q + "” (of " + data.total + " discovered)"
          : "no sessions discovered";
        sessionList.appendChild(empty);
        setStatus(data.sessions.length + " matches (of " + (data.total || 0) + " discovered).");
        return;
      }
      data.sessions.forEach(function (s, i) {
        var el = document.createElement("div");
        el.className = "session-row";
        el.style.animationDelay = Math.min(i, 12) * 40 + "ms";
        el.dataset.path = s.path;
        el.innerHTML =
          '<div class="session-path">' + esc(s.workspace.replace(/^wd_/, "")) + "</div>" +
          '<div class="session-id">' + esc(s.id.slice(0, 8)) + "</div>" +
          '<div class="session-meta">' +
          badge("dim", fmtTs(s.mtime).slice(5, 16)) +
          (s.uploaded ? badge("ok", "upload") : "") +
          (s.cached ? badge("ok", "audited") : "") +
          "</div>";
        el.title = s.path;
        if (currentSession && s.path === currentSession.path) el.classList.add("active");
        if (s.uploaded) {
          var del = document.createElement("button");
          del.className = "session-del";
          del.type = "button";
          del.title = "删除这份上传（磁盘文件一并删除）";
          del.setAttribute("aria-label", "删除这份上传");
          del.textContent = "×";
          del.addEventListener("click", function (ev) {
            ev.stopPropagation();
            deleteUploaded(s);
          });
          el.appendChild(del);
        }
        el.addEventListener("click", function () { openAudit(s, el); });
        sessionList.appendChild(el);
      });
      setStatus(q
        ? data.sessions.length + " matches for “" + q + "” (of " + data.total + " discovered)."
        : data.sessions.length + " recent sessions (of " + (data.total || data.sessions.length) + "). pick one.");
    }).catch(function (e) {
      setStatus("error: " + e.message);
    });
  }

  var searchInput = document.getElementById("session-search");
  var searchTimer = null;
  searchInput.addEventListener("input", function () {
    clearTimeout(searchTimer);
    var value = searchInput.value;
    searchTimer = setTimeout(function () { loadSessions(value); }, 250);
  });

  document.getElementById("fleet-btn").addEventListener("click", function () {
    setStatus("computing fleet stats over all sessions (parses every log)…");
    fetch("/api/fleet").then(function (r) { return r.json(); }).then(function (data) {
      renderFleet(data.fleet);
      var f = data.fleet || {};
      setStatus("fleet: " + f.sessions + " sessions parsed");
    }).catch(function (e) {
      setStatus("error: " + e.message);
    });
  });

  function renderFleet(fleet) {
    var box = document.getElementById("fleet");
    if (!fleet || !fleet.sessions) { box.hidden = true; return; }
    box.hidden = false;
    var shapes = Object.keys(fleet.growth_shapes || {}).map(function (k) {
      return badge("dim", k + "×" + fleet.growth_shapes[k]);
    }).join("");
    box.innerHTML =
      badge("dim", fleet.sessions + " sessions") +
      badge("dim", "avg " + fleet.avg_turns + " turns") +
      (fleet.avg_cache_hit_rate != null
        ? badge(fleet.avg_cache_hit_rate >= 0.5 ? "ok" : "warn",
                "avg cache " + Math.round(fleet.avg_cache_hit_rate * 100) + "% (" + fleet.with_usage + " with usage)")
        : badge("dim", "no usage data")) +
      '<span class="dim" style="flex-basis:100%">growth</span>' + shapes +
      badge(fleet.total_repeat_extra_tokens >= 5000 ? "warn" : "dim",
            "repeat +" + fleet.total_repeat_extra_tokens +
            (fleet.total_repeat_extra_tokens
              ? " (压缩恢复 " + (((fleet.repeat_class_totals || {}).post_compaction) || 0) +
                " / 其它 " + Math.max(0, fleet.total_repeat_extra_tokens -
                  (((fleet.repeat_class_totals || {}).post_compaction) || 0)) + ")"
              : ""));
  }

  // ------------------------------------------------------------------
  // audit panel
  // ------------------------------------------------------------------

  function openAudit(session, rowEl) {
    currentSession = session;
    document.querySelectorAll(".session-row.active").forEach(function (r) {
      r.classList.remove("active");
    });
    if (rowEl) rowEl.classList.add("active");
    auditForm.hidden = false;
    report.innerHTML = "";
    progress.hidden = true;
    auditTitle.textContent = session.workspace.replace(/^wd_/, "") + " · " + session.id.slice(0, 8);
    treeLink.hidden = false;
    // always ask for the IDE DB subagent join; a missing DB degrades to plain view
    treeLink.href = "/api/tree?path=" + encodeURIComponent(session.path) + "&ide_db=1";
    setStatus("ready. " + session.path);
    loadPlaybooks();
  }

  function loadPlaybooks() {
    var select = auditForm.elements.playbook;
    fetch("/api/playbooks").then(function (r) { return r.json(); }).then(function (data) {
      var current = select.value;
      select.innerHTML = '<option value="">none</option>';
      data.playbooks.forEach(function (p) {
        var o = document.createElement("option");
        o.value = p;
        o.textContent = p.split("/").slice(-2).join("/");
        select.appendChild(o);
      });
      select.value = current;
    });
  }

  document.getElementById("refresh").addEventListener("click", function () {
    loadSessions(searchInput.value);
  });

  // ------------------------------------------------------------------
  // upload: a session file from anywhere (typically an export from
  // another machine) persists in the managed uploads dir and is then
  // just a session with a path — the audit flow treats it identically.
  // ------------------------------------------------------------------

  var uploadInput = document.getElementById("upload-input");
  var uploadBtn = document.getElementById("upload-btn");
  var sidebar = document.querySelector(".sidebar");
  var uploadBusy = false;

  function uploadFile(file, note) {
    if (!file) return;
    if (uploadBusy) {
      setStatus("upload already in progress…");
      return;
    }
    uploadBusy = true;
    setStatus("uploading " + file.name + (note || "") + "…");
    fetch("/api/upload?name=" + encodeURIComponent(file.name), {
      method: "POST",
      body: file,
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      uploadBusy = false;
      if (!res.ok) {
        setStatus("upload failed: " + (res.data.error || "unknown error"));
        return;
      }
      loadSessions(searchInput.value).then(function () {
        setStatus("uploaded " + res.data.session.id +
          (res.data.dedup ? " (already uploaded — reused)" : "") +
          " · pick params and run audit");
      });
      openAudit(res.data.session, null);
    }).catch(function (e) {
      uploadBusy = false;
      setStatus("upload failed: " + e.message);
    });
  }

  if (uploadBtn) {
    uploadBtn.addEventListener("click", function () { uploadInput.click(); });
  }
  if (uploadInput) {
    uploadInput.addEventListener("change", function () {
      uploadFile(uploadInput.files[0]);
      uploadInput.value = "";
    });
  }
  if (sidebar) {
    sidebar.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      sidebar.classList.add("drop");
    });
    sidebar.addEventListener("dragleave", function () {
      sidebar.classList.remove("drop");
    });
    sidebar.addEventListener("drop", function (ev) {
      sidebar.classList.remove("drop");
      if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      uploadFile(ev.dataTransfer.files[0],
        ev.dataTransfer.files.length > 1
          ? " (only the first of " + ev.dataTransfer.files.length + " dropped files)"
          : "");
    });
  }

  function deleteUploaded(session) {
    if (!confirm("删除上传的会话 " + session.id + "？磁盘文件会一并删除。")) return;
    setStatus("deleting " + session.id + "…");
    fetch("/api/upload?path=" + encodeURIComponent(session.path), {
      method: "DELETE",
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function () {
      loadSessions(searchInput.value).then(function () {
        setStatus("deleted upload " + session.id);
      });
    }).catch(function (e) {
      setStatus("delete failed: " + e.message);
    });
  }

  function collectDirs() {
    return (auditForm.elements.dirs.value || "").split("\n").map(function (s) {
      return s.trim();
    }).filter(Boolean);
  }

  function collectInjected() {
    return (auditForm.elements.inject.value || "").trim();
  }

  // Validate the pasted JSON before the run: a typo that only surfaces as a
  // mysteriously smaller catalog after ten minutes is the failure mode worth
  // designing against.
  function checkInjected() {
    var box = document.getElementById("inject-status");
    if (!box) return true;
    var text = collectInjected();
    if (!text) { box.textContent = ""; box.className = "dir-status dim"; return true; }
    var raw;
    try {
      raw = JSON.parse(text);
    } catch (e) {
      box.textContent = "JSON 解析失败：" + e.message;
      box.className = "dir-status warn";
      return false;
    }
    var entries = (raw && typeof raw === "object" && raw.skills &&
                   typeof raw.skills === "object") ? raw.skills : raw;
    var names = (entries && typeof entries === "object") ? Object.keys(entries) : [];
    if (!names.length) {
      box.textContent = "没解析出任何条目";
      box.className = "dir-status warn";
      return true;
    }
    var thin = names.filter(function (n) {
      var e = entries[n];
      return !e || typeof e !== "object" ||
        (!(e.description || "").trim() && !(e.body_path || ""));
    });
    box.textContent = names.length + " 个条目" +
      (thin.length ? " · ⚠ " + thin.length + " 个缺 description 且无 body_path（会被跳过）" : "");
    box.className = "dir-status" + (thin.length ? " warn" : " ok");
    return true;
  }

  // ------------------------------------------------------------------
  // catalog audit: static check of the catalog itself. Deliberately not
  // part of the session audit — it needs no session, no judge, and says
  // nothing about how any agent behaved.
  // ------------------------------------------------------------------

  var catalogBtn = document.getElementById("catalog-audit-btn");
  var catalogPanel = document.getElementById("catalog-audit");
  var catalogBody = document.getElementById("catalog-audit-body");
  var catalogClose = document.getElementById("catalog-audit-close");

  if (catalogClose) {
    catalogClose.addEventListener("click", function () {
      catalogPanel.hidden = true;
    });
  }

  var TIER_TEXT = {
    oversize: "超标", large: "偏大", ok: "OK",
    verbose: "冗余", terse: "过短", unusable: "解析失败",
  };

  function kfmt(n) {
    return n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }

  function auditRows(entries, cols) {
    if (!entries.length) return '<p class="dim">无</p>';
    var out = "<table class='audit-table'><thead><tr>";
    cols.forEach(function (c) { out += "<th>" + c.label + "</th>"; });
    out += "</tr></thead><tbody>";
    entries.forEach(function (e) {
      out += "<tr>";
      cols.forEach(function (c) {
        var v = c.get(e);
        out += "<td class='" + (c.cls ? c.cls(e) : "") + "'>" + v + "</td>";
      });
      out += "</tr>";
    });
    return out + "</tbody></table>";
  }

  function tierBadge(tier) {
    var cls = tier === "ok" ? "ok" : (tier === "large" || tier === "terse" ? "warn" : "bad");
    return "<span class='tier " + cls + "'>" + (TIER_TEXT[tier] || tier) + "</span>";
  }

  function injectedMark(e) {
    return e.source === "injected" ? " <span class='dim'>（注入）</span>" : "";
  }

  function renderCatalogAudit(d) {
    var h = "";
    h += "<div class='catalog-summary'>";
    h += "<div><span class='k'>" + d.total + "</span><span class='l'>skills</span></div>";
    h += "<div><span class='k'>" + d.body_count + "</span><span class='l'>有正文</span></div>";
    if (d.no_body && d.no_body.length) {
      h += "<div><span class='k'>" + d.no_body.length +
           "</span><span class='l'>无正文，未计 activation</span></div>";
    }
    if (d.injected_count) {
      h += "<div><span class='k'>" + d.injected_count +
           "</span><span class='l'>人工注入</span></div>";
    }
    h += "</div>";

    // Layering ratio: the headline number — what disclosure costs, and what
    // it buys.
    h += "<section class='catalog-block'>";
    h += "<h3>分层有效性</h3>";
    if (d.ratio === null || d.ratio === undefined) {
      h += "<p class='dim'>没有可用的 description，无法计算比值。</p>";
    } else {
      h += "<div class='ratio'>";
      h += "<div class='ratio-col'><span class='k'>" + kfmt(d.discovery_tokens) +
           "</span><span class='l'>discovery 常驻（Σ description）</span></div>";
      h += "<div class='ratio-col'><span class='k'>" + kfmt(d.activation_tokens) +
           "</span><span class='l'>activation 潜在（Σ 整份）</span></div>";
      h += "<div class='ratio-col'><span class='k'>" + d.ratio.toFixed(1) +
           ":1</span><span class='l'>比值越小，分层越有效</span></div>";
      h += "</div>";
    }
    h += "</section>";

    // Parse health first among the findings: a skill audited on a broken
    // description is silently mis-scored, so it outranks a size complaint.
    h += "<section class='catalog-block'>";
    h += "<h3>解析健康度</h3>";
    if (!d.parse_failures.length) {
      h += "<p class='ok-text'>✓ 全部 description 均可解析</p>";
    } else {
      h += "<p class='bad-text'>✗ " + d.parse_failures.length +
           " 个 description 解析失败——审计时缺少路由依据</p>";
      h += auditRows(d.parse_failures, [
        { label: "skill", get: function (e) { return esc(e.name) + injectedMark(e); } },
        { label: "description", get: function (e) {
            return e.sample ? "<code>" + esc(e.sample) + "</code>" : "<span class='dim'>（空）</span>";
          } },
      ]);
    }
    h += "</section>";

    h += "<section class='catalog-block'>";
    h += "<h3>体量分级</h3>";
    h += "<p class='dim'>约定阈值：&gt;" + d.thresholds.max_lines + " 行超标 / " +
         d.thresholds.large_lines + "-" + d.thresholds.max_lines +
         " 偏大 —— 经验值，非规范要求</p>";
    h += "<p>超标 <b>" + d.size_tiers.oversize + "</b> · 偏大 <b>" + d.size_tiers.large +
         "</b> · OK <b>" + d.size_tiers.ok + "</b>（按 token 排序，最多显示 " +
         (d.top ? d.top.length : 0) + " 个）</p>";
    h += auditRows(d.top || [], [
      { label: "skill", get: function (e) { return esc(e.name) + injectedMark(e); } },
      { label: "行数", get: function (e) { return String(e.lines); },
        cls: function (e) { return e.tier === "oversize" ? "bad" : ""; } },
      { label: "token", get: function (e) { return kfmt(e.tokens); } },
      { label: "分级", get: function (e) { return tierBadge(e.tier); } },
    ]);
    h += "</section>";

    h += "<section class='catalog-block'>";
    h += "<h3>description 质量</h3>";
    h += "<p class='dim'>约定阈值：" + d.thresholds.min_desc_chars + "-" +
         d.thresholds.max_desc_chars + " 字符为佳 —— 经验值，非规范要求</p>";
    h += "<p>冗余 <b>" + d.desc_tiers.verbose + "</b> · 过短 <b>" + d.desc_tiers.terse +
         "</b> · 解析失败 <b>" + d.desc_tiers.unusable + "</b> · 合格 <b>" +
         d.desc_tiers.ok + "</b></p>";
    h += auditRows(d.desc_top || [], [
      { label: "skill", get: function (e) { return esc(e.name) + injectedMark(e); } },
      { label: "字符", get: function (e) { return String(e.chars); } },
      { label: "分级", get: function (e) { return tierBadge(e.tier); } },
      { label: "开头", get: function (e) {
          return "<span class='dim'>" + esc(e.sample.slice(0, 90)) + "</span>";
        } },
    ]);
    h += "</section>";

    // Duplication: body copies cost the activation layer, listing collisions
    // the resident one — the two counts never merge. Both are convention
    // thresholds, and the waste column is the smaller side only.
    h += "<section class='catalog-block'>";
    h += "<h3>重复度</h3>";
    h += "<p class='dim'>约定阈值：body containment ≥ " +
         d.thresholds.dup_containment.toFixed(2) + " · desc jaccard ≥ " +
         d.thresholds.desc_dup_containment.toFixed(2) + " · " +
         d.thresholds.shingle_k + " 重窗；合并可省取较小一侧 —— 经验值，非规范要求</p>";
    if (!d.duplicates.length) {
      h += "<p class='ok-text'>✓ 未发现近重复</p>";
    } else {
      var bodyPairs = 0;
      d.duplicates.forEach(function (p) { if (p.kind === "body") bodyPairs++; });
      var rate = (d.dup_rate === null || d.dup_rate === undefined)
        ? "n/a" : Math.round(d.dup_rate * 100) + "%";
      h += "<p>body 近重复 <b>" + bodyPairs + "</b> 对 · desc 近重复 <b>" +
           (d.duplicates.length - bodyPairs) + "</b> 对 · body 涉及占比 <b>" +
           rate + "</b></p>";
      var dupShown = d.duplicates.slice(0, 20);
      h += auditRows(dupShown, [
        { label: "口径", get: function (e) {
            return e.kind === "body" ? "body · 激活" : "desc · 常驻"; } },
        { label: "对", get: function (e) {
            return esc(e.a) + " ↔ " + esc(e.b) +
              (e.exact ? " <span class='dim'>完全相同</span>" : ""); } },
        { label: "containment", get: function (e) {
            return e.containment.toFixed(2); } },
        { label: "jaccard", get: function (e) {
            return e.jaccard.toFixed(2); } },
        { label: "合并可省", get: function (e) {
            return "~" + kfmt(e.waste_tokens_est); } },
      ]);
      if (d.duplicates.length > dupShown.length) {
        h += "<p class='dim'>…… 另有 " +
             (d.duplicates.length - dupShown.length) + " 对</p>";
      }
    }
    h += "</section>";

    // Closure: what one activation drags in, statically. Heaviest first;
    // unresolved requires and depth truncation stay visible per entry.
    var warn = d.thresholds.closure_warn_tokens;
    var depth = d.thresholds.closure_depth;
    h += "<section class='catalog-block'>";
    h += "<h3>加载闭包</h3>";
    h += "<p class='dim'>约定阈值：&gt;" + kfmt(warn) +
         " tokens 偏重 · requires 深度上限 " + depth +
         " 跳 —— 经验值，非规范要求</p>";
    if (!d.closures.length) {
      h += "<p class='dim'>没有可扫描正文的 skill，未产生闭包。</p>";
    } else {
      var heavy = 0, withDeps = 0;
      d.closures.forEach(function (e) {
        if (e.tokens > warn) heavy++;
        if (e.skills.length || e.files.length) withDeps++;
      });
      h += "<p>偏重 <b>" + heavy + "</b> · 带依赖 <b>" + withDeps +
           "</b> · 可扫描 <b>" + d.closures.length + "</b>（无正文 " +
           (d.no_body ? d.no_body.length : 0) + " 个未计入）</p>";
      var cloShown = d.closures.slice(0, 20);
      h += "<table class='audit-table'><thead><tr><th>skill</th><th>token</th>" +
           "<th>依赖树</th></tr></thead><tbody>";
      cloShown.forEach(function (e) {
        var tree = [];
        if (e.skills.length) tree.push("→ " + e.skills.map(esc).join(", "));
        if (e.files.length) tree.push("· " + e.files.map(esc).join(", "));
        var marks = "";
        if (e.unresolved.length) {
          marks += "<div class='dim'>未解析的 requires：" +
                   e.unresolved.map(esc).join(", ") + "</div>";
        }
        if (e.truncated) {
          marks += "<div class='dim'>深度截断：" + depth +
                   " 跳处仍有 requires 未展开</div>";
        }
        h += "<tr>";
        h += "<td>" + esc(e.name) +
             (e.tokens > warn ? " <span class='tier warn'>偏重</span>" : "") +
             marks + "</td>";
        h += "<td class='" + (e.tokens > warn ? "bad" : "") + "'>~" +
             kfmt(e.tokens) + "</td>";
        h += "<td>" + (tree.length
          ? "<span class='dim'>" + tree.join(" ") + "</span>"
          : "<span class='dim'>—</span>") + "</td>";
        h += "</tr>";
      });
      h += "</tbody></table>";
      if (d.closures.length > cloShown.length) {
        h += "<p class='dim'>…… 另有 " +
             (d.closures.length - cloShown.length) + " 个</p>";
      }
    }
    h += "</section>";

    h += "<p class='dim tiny'>token 估算器 " + esc(d.estimator) +
         " · 估算值，非 harness 真实计费 · 静态体检不评价 agent 表现</p>";
    return h;
  }

  function runCatalogAudit() {
    if (!catalogPanel || !catalogBody) return;
    catalogPanel.hidden = false;
    catalogBody.innerHTML = "<p class='dim'>体检中…</p>";
    fetch("/api/catalog-audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skills_dirs: collectDirs(),
        injected: collectInjected(),
      }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.error) {
        catalogBody.innerHTML = "<p class='bad-text'>体检失败：" + esc(data.error) + "</p>";
        return;
      }
      catalogBody.innerHTML = renderCatalogAudit(data);
    }).catch(function (e) {
      catalogBody.innerHTML = "<p class='bad-text'>体检失败：" + esc(e.message) + "</p>";
    });
  }

  if (catalogBtn) {
    catalogBtn.addEventListener("click", runCatalogAudit);
  }

  auditForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (!currentSession) return;
    if (!checkInjected()) return;
    var params = {
      path: currentSession.path,
      skills_dirs: collectDirs(),
      injected: collectInjected(),
      expect: (auditForm.elements.expect.value || "").split(",").map(function (s) {
        return s.trim();
      }).filter(Boolean),
      steps: auditForm.elements.playbook.value ? [auditForm.elements.playbook.value] : [],
      judge_triggers: auditForm.elements.judge.checked,
      billed: auditForm.elements.billed.checked,
    };
    report.innerHTML = "";
    progress.hidden = false;
    stepsBox.innerHTML = '<div class="step active">checking cache</div>';
    setStatus("audit running…");
    fetch("/api/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.cached) {
        progress.hidden = true;
        renderReport(data.result);
        setStatus(data.result.status_line + " · cached");
        return;
      }
      stepsBox.innerHTML = '<div class="step active">queued</div>';
      pollJob(data.job_id);
    }).catch(function (e) {
      progress.hidden = true;
      renderError(e.message);
    });
  });

  function rerunFresh() {
    if (!currentSession) return;
    var params = {
      path: currentSession.path,
      skills_dirs: collectDirs(),
      injected: collectInjected(),
      expect: (auditForm.elements.expect.value || "").split(",").map(function (s) {
        return s.trim();
      }).filter(Boolean),
      steps: auditForm.elements.playbook.value ? [auditForm.elements.playbook.value] : [],
      judge_triggers: auditForm.elements.judge.checked,
      billed: auditForm.elements.billed.checked,
      force: true,
    };
    report.innerHTML = "";
    progress.hidden = false;
    stepsBox.innerHTML = '<div class="step active">re-running fresh (cache bypassed)</div>';
    setStatus("audit running…");
    fetch("/api/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }).then(function (r) { return r.json(); }).then(function (data) {
      pollJob(data.job_id);
    }).catch(function (e) {
      progress.hidden = true;
      renderError(e.message);
    });
  }

  function renderSteps(steps, running) {
    var recent = steps.slice(-3);
    var older = steps.slice(0, -3);
    var html = "";
    if (older.length) {
      html += "<details class='step-history'><summary>" + older.length + " earlier steps</summary>" +
        older.map(function (s) { return '<div class="step">' + esc(s) + "</div>"; }).join("") + "</details>";
    }
    recent.forEach(function (s, i) {
      var cls = i === recent.length - 1 && running ? "step active" : "step";
      html += '<div class="' + cls + '">' + esc(s) + "</div>";
    });
    stepsBox.innerHTML = html;
  }

  function pollJob(jobId) {
    fetch("/api/job/" + jobId).then(function (r) { return r.json(); }).then(function (job) {
      renderSteps(job.steps || [], job.status === "running" || job.status === "queued");
      if (job.status === "done") {
        progress.hidden = true;
        renderReport(job.result);
        setStatus(job.result.status_line + " · " + job.result.duration_s + "s");
        return;
      }
      if (job.status === "error") {
        progress.hidden = true;
        renderError(job.error, job.steps || []);
        return;
      }
      pollTimer = setTimeout(function () { pollJob(jobId); }, 2000);
    });
  }

  // ------------------------------------------------------------------
  // failure: where it broke + recovery actions, not just red text
  // ------------------------------------------------------------------

  function renderError(err, steps) {
    var hint = "重试一次；若持续失败，检查会话目录是否包含 wire.jsonl。";
    var action = "retry";
    if (/no events parsed/.test(err)) {
      hint = "日志解析出 0 个事件: 确认目录里有 wire.jsonl，且不是第三种未支持的格式（如 codex rollout）。";
    } else if (/judge/i.test(err)) {
      hint = "judge 不可用: 需要 ASD_JUDGE_API_KEY 和 ASD_JUDGE_MODEL（或环境里有 DEEPSEEK_API_KEY）。";
    } else if (/rate limit|429|overload/i.test(err)) {
      hint = "推理引擎过载（429）: 等几分钟后重试。";
    }
    var stepHtml = (steps && steps.length)
      ? "<div class='steps'>" + steps.map(function (s) { return '<div class="step">' + esc(s) + "</div>"; }).join("") + "</div>"
      : "";
    report.innerHTML =
      "<section><h2>failed</h2>" +
      "<p>" + badge("bad", "error") + " " + esc(err) + "</p>" +
      stepHtml +
      "<p class='dim'>" + esc(hint) + "</p>" +
      '<button class="btn" id="retry-btn" type="button">' + esc(action) + "</button>" +
      "</section>";
    document.getElementById("retry-btn").addEventListener("click", function () {
      auditForm.dispatchEvent(new Event("submit", { cancelable: true }));
    });
    setStatus("failed: " + err);
  }

  // ------------------------------------------------------------------
  // report rendering
  // ------------------------------------------------------------------

  var currentTurnGrowth = [];

  // must match tokenstats.BUCKET_KEYS; the seg-<key> colors live in style.css
  var BUCKETS = ["system", "history", "inject", "skill", "tool", "output"];
  var BUCKET_NAMES = {
    system: "system（残差，推断）",
    history: "history（你的输入）",
    inject: "inject（harness 注入）",
    skill: "skill（技能正文）",
    tool: "tool（工具结果）",
    output: "output（模型产出）"
  };

  function turnDetailHtml(r) {
    var head = "<strong>T" + r.turn + "</strong> · +" + r.added + " tokens · 上下文至 " +
      r.context_at_start + (r.exact ? "" : "（插值）");
    var blocks = BUCKETS.map(function (k) {
      var sub = r[k + "_added"] || 0;
      if (k !== "system" && sub <= 0) return "";
      var share = r.added ? Math.round(sub / r.added * 100) : 0;
      var rows = (r.items || []).filter(function (it) { return it.bucket === k; });
      var body;
      if (k === "system") {
        body = '<p class="dim">残差 = 观测不到归属的增量（system prompt / 工具 schema / token 估算误差），日志里没有它的内容明细。</p>';
      } else if (!rows.length) {
        body = '<p class="dim">无明细（条目过小已被过滤）。</p>';
      } else {
        body = rows.map(function (it) {
          return '<div class="turn-item"><span class="dim">~' + it.tokens + "</span> " +
            esc((it.preview || "").slice(0, 120)) + "</div>";
        }).join("");
      }
      return "<details" + (k === "tool" ? " open" : "") + "><summary>" +
        '<span class="seg-' + k + '"></span>' + esc(BUCKET_NAMES[k]) + " · " + sub +
        "（" + share + "%）" + (rows.length ? " · " + rows.length + " 项" : "") +
        "</summary>" + body + "</details>";
    }).join("");
    return '<div class="turn-detail-head">' + head + "</div>" + blocks;
  }

  // bar click drill-down: show what each bucket added this turn; click the
  // same bar again (or another bar) to close/switch
  document.addEventListener("click", function (ev) {
    var bar = ev.target.closest ? ev.target.closest(".bar") : null;
    if (!bar) return;
    var box = document.getElementById("turn-detail");
    if (!box) return;
    var turnNo = bar.getAttribute("data-turn");
    if (box.getAttribute("data-turn") === turnNo && !box.hidden) {
      box.hidden = true;
      box.removeAttribute("data-turn");
      bar.classList.remove("active");
      return;
    }
    var row = null;
    for (var i = 0; i < currentTurnGrowth.length; i++) {
      if (String(currentTurnGrowth[i].turn) === turnNo) row = currentTurnGrowth[i];
    }
    if (!row) return;
    box.innerHTML = turnDetailHtml(row);
    box.hidden = false;
    box.setAttribute("data-turn", turnNo);
    bar.parentNode.querySelectorAll(".bar").forEach(function (b) {
      b.classList.toggle("active", b === bar);
    });
    box.scrollIntoView({ block: "nearest" });
  });

  function growthGapNote(t) {
    var rows = t.turn_growth || [];
    var head = '<p class="dim">每轮 context 增长图与 bucket 占比表未显示：';
    if (!rows.length) {
      return head + "该日志没有带 context 遥测的主 agent 轮次。</p>";
    }
    var plottable = rows.filter(function (r) {
      return r.added != null && r.added >= 0 && !r.crossed_compaction;
    });
    if (plottable.length) {
      return head + plottable.length + " 轮可绘，但可归因的 context 增量为 0。</p>";
    }
    var crossed = rows.filter(function (r) { return r.crossed_compaction; }).length;
    var last = rows.filter(function (r) { return r.added == null; }).length;
    var shrank = rows.filter(function (r) { return r.added != null && r.added < 0; }).length;
    var why = [];
    if (crossed) why.push(crossed + " 轮跨压缩（增量不可比，已剔除）");
    if (last) why.push(last + " 轮为末轮（无后继轮可差分）");
    if (shrank) why.push(shrank + " 轮 context 收缩（增量为负）");
    return head + rows.length + " 轮中 0 轮可绘 —— " +
      (why.join("、") || "无可归因的 context 增长") + "。</p>";
  }

  function renderTokenStats(t) {
    if (!t) return "";
    var parts = [];
    var badges = [];
    if (t.usage_record_count) {
      badges.push(badge("fact", "cache hit " + (t.cache_hit_rate != null
        ? Math.round(t.cache_hit_rate * 100) + "%" : "?")));
      badges.push(badge("dim", "input billed " + t.input_total));
      badges.push(badge("dim", "output " + t.output_total));
    } else {
      badges.push(badge("dim", "no usage records"));
    }
    var vcls = t.growth_verdict.indexOf("sublinear") === 0 ? "ok"
      : t.growth_verdict.indexOf("insufficient") === 0 || t.growth_verdict.indexOf("unavailable") === 0
        ? "dim" : "bad";
    badges.push(badge(vcls, "growth: " + t.growth_verdict));
    if (t.hash_flips == null) {
      badges.push(badge("dim", "prompt flips unavailable (no LLM request hashes)"));
    } else {
      badges.push(badge(t.hash_flips ? "bad" : "ok", "prompt flips " + t.hash_flips));
    }
    if (t.repeats && t.repeats.length) {
      badges.push(badge("warn", "repeat injection ~" + t.repeat_extra_tokens + " tok"));
    }
    parts.push("<div class='session-meta'>" + badges.join("") + "</div>");

    if (t.bucket_shares && Object.keys(t.bucket_shares).length) {
      var order = BUCKETS;
      var rows = order.map(function (k) {
        return "<tr><td>" + esc(k) + (k === "system" ? "（残差，推断）" : "") + "</td><td>" +
          (t.bucket_totals[k] || 0) + "</td><td>" +
          Math.round((t.bucket_shares[k] || 0) * 100) + "%</td></tr>";
      }).join("");
      parts.push("<table><tr><th>bucket</th><th>tokens added</th><th>share</th></tr>" + rows + "</table>");
      var grew = (t.turn_growth || []).filter(function (r) {
        return r.added != null && r.added >= 0 && !r.crossed_compaction;
      });
      if (grew.length) {
        currentTurnGrowth = grew;
        var maxAdded = Math.max.apply(null, grew.map(function (r) { return r.added; })) || 1;
        var bars = grew.map(function (r) {
          var observed = order.reduce(function (acc, k) {
            return acc + (r[k + "_added"] || 0);
          }, 0);
          var scale = observed ? Math.min(1, r.added / observed) : 1;
          var segs = order.map(function (k) {
            var v = (r[k + "_added"] || 0) * scale;
            return v > 0 ? '<span class="bar-seg seg-' + k + '" style="height:' +
              Math.max(2, Math.round(v / maxAdded * 56)) + 'px"></span>' : "";
          }).join("");
          return '<div class="bar" data-turn="' + r.turn + '" title="T' + r.turn + " +" + r.added +
            ' tokens · 点击查看构成">' + segs + "</div>";
        }).join("");
        var legend = order.map(function (k) {
          return '<span><span class="seg-' + k + '"></span>' + esc(k) + "</span>";
        }).join("");
        parts.push('<div class="bar-chart">' + bars + '</div>');
        parts.push('<div class="bar-legend">' + legend +
          '<span class="dim">system 为残差推断 · 被压缩跨越的轮次已剔除 · 点击柱子下钻</span></div>');
        parts.push('<div id="turn-detail" hidden></div>');
      }
    } else {
      parts.push(growthGapNote(t));
    }

    if (t.hash_runs && t.hash_runs.length) {
      var hrows = t.hash_runs.map(function (run) {
        return "<tr><td>" + esc(run.kind) + "</td><td><code>" + esc(run.hash) + "</code></td><td>" +
          run.requests + "</td><td>" + esc(fmtTs(run.first_ts)) + "</td><td>" +
          esc(fmtTs(run.last_ts)) + "</td></tr>";
      }).join("");
      parts.push("<table><tr><th>kind</th><th>hash</th><th>requests</th><th>first</th><th>last</th></tr>" +
        hrows + "</table>");
      if (t.hash_flips) {
        parts.push('<p class="dim">哈希翻转 ' + t.hash_flips +
          ' 次（日志事实）：每次翻转使 provider 前缀缓存失效、重付 prefill。</p>');
      }
    }

    if (t.repeats && t.repeats.length) {
      var classLabels = {post_compaction: "压缩恢复", poll: "轮询",
                         no_compaction: "无压缩", unclassified: "未分类"};
      var rrows = t.repeats.map(function (r) {
        var counts = {};
        (r.occurrence_classes || []).slice(1).forEach(function (c) {
          counts[c] = (counts[c] || 0) + 1;
        });
        var classes = Object.keys(counts).sort().map(function (c) {
          return (classLabels[c] || c) + " ×" + counts[c];
        }).join(" · ") || "-";
        return "<tr><td>" + r.occurrences + "×</td><td>~" + r.tokens_each + "</td><td>~" +
          r.extra_tokens + '</td><td class="dim">' + esc(classes) + '</td><td class="dim">' +
          esc(r.preview.slice(0, 100)) + "</td></tr>";
      }).join("");
      var post = ((t.repeat_class_totals || {}).post_compaction) || 0;
      var tax = Math.max(0, t.repeat_extra_tokens - post);
      var caption = t.compaction_source
        ? '<p class="dim">压缩点 ' + (t.compaction_points || []).length +
          ' 个（来源 ' + esc(t.compaction_source) + '）：其中压缩恢复 ~' + post +
          ' token 属压缩恢复协议内合法重读，其余 ~' + tax +
          ' 才是重读税。extra 总量是重读税下界。</p>'
        : '<p class="dim">无压缩证据源，未分类；--billed-usage（CLI）/ billed=1（web）可接入账单证据。</p>';
      parts.push("<details><summary>" + badge("warn", "repeat injection") + " " + t.repeats.length +
        " 组 · ~" + t.repeat_extra_tokens + " extra tokens</summary>" +
        "<table><tr><th>次数</th><th>每次~tokens</th><th>多付</th><th>分类</th><th>内容</th></tr>" +
        rrows + "</table>" + caption + "</details>");
    }
    return "<section id='tokens'><h2>token governance " + helpDot("tokens") + "</h2>" + parts.join("") + "</section>";
  }

  function renderBill(bill, returns) {
    var GRADE_CLS = {"事实": "fact", "观测": "ok", "证据": "warn",
                     "推断": "dim", "判断": "dim"};
    var rows = bill.cost_rows.map(function (row) {
      var tok = row.tokens_est == null
        ? '<span class="unavailable">unavailable</span>'
        : "~" + row.tokens_est + " (EST)";
      var share = row.share == null ? "—" : (row.share * 100).toFixed(1) + "%";
      var html = "<tr><td>" + esc(row.label) + "</td><td>" + tok +
        "</td><td>" + share + "</td><td>" + esc(row.detail) + "</td><td>" +
        badge(GRADE_CLS[row.grade] || "dim", row.grade) + "</td></tr>";
      if (row.key === "subagent_returns" && returns) {
        (returns.rows || []).filter(function (rr) {
          return (rr.flagged_reasons || []).length;
        }).slice(0, 3).forEach(function (rr) {
          var proc = rr.subagent_tokens_est == null
            ? "unavailable" : "~" + rr.subagent_tokens_est;
          var ret = rr.return_tokens_est == null
            ? "unavailable" : "~" + rr.return_tokens_est;
          html += '<tr class="bill-sub-row"><td colspan="5">' +
            badge("warn", "bloat") + " " + esc(rr.dispatch_id) +
            " · 返回 " + ret + " · 过程 " + proc +
            (rr.ratio == null ? "" : " · 比 " + rr.ratio) +
            " · " + esc((rr.flagged_reasons || []).join(" + ")) +
            (rr.description ? " · " + esc(rr.description) : "") +
            "</td></tr>";
        });
      }
      return html;
    }).join("");
    var h = '<section id="bill"><h2>bill · 成本与路由' + helpDot("bill") + "</h2>" +
      '<table class="bill-table"><thead><tr><th>成本项</th><th>tokens</th>' +
      "<th>share</th><th>detail</th><th>等级</th></tr></thead><tbody>" +
      rows +
      '<tr><td class="dim">EST 小计</td><td>~' + bill.est_subtotal +
      '</td><td colspan="3" class="dim">share 分母，不含 output / 缓存率</td></tr>' +
      "</tbody></table>";
    var routing = bill.routing || {lines: [], enabled: false};
    if (routing.lines.length) {
      h += '<div class="bill-routing">' +
        routing.lines.map(function (l) {
          return '<a href="' + esc(l.anchor) + '">' + esc(l.label) + " " +
            esc(l.value) + " " + badge("dim", l.grade) + "</a>";
        }).join("") +
        (routing.enabled ? ""
         : '<span class="dim">judge 未启用，路由账单不完整。</span>') +
        "</div>";
    } else {
      h += '<p class="dim">无路由信号。</p>';
    }
    if (bill.billed) {
      var cachePct = bill.billed.cached_share == null
        ? "unknown" : Math.round(bill.billed.cached_share * 100) + "%";
      h += '<p class="dim">billed（会话级，' + bill.billed.requests +
        " 请求 · prompt " + bill.billed.prompt_total +
        " · completion " + bill.billed.completion_total +
        " · cache " + cachePct + "）：" + esc(bill.billed.note) + "</p>";
    }
    (bill.notes || []).forEach(function (n) {
      h += '<p class="dim">' + esc(n) + "</p>";
    });
    return h + "</section>";
  }

  function renderReport(r) {
    var parts = [];
    if (r.bill) {
      parts.push(renderBill(r.bill, r.subagent_returns));
    }
    parts.push(
      "<section><h2>summary " + helpDot("summary") + "</h2><div class='session-meta'>" +
      badge("dim", "turns " + r.session.turns) +
      badge("dim", "events " + r.session.events) +
      badge("fact", "loaded " + r.timeline.loads.length) +
      badge("warn", "file-read " + r.timeline.file_reads.length) +
      badge("dim", "compactions " + (r.timeline.compaction_count == null
        ? "unavailable" : r.timeline.compaction_count)) +
      badge("dim", "catalog " + r.catalog_size) +
      badge(r.judge_enabled ? "ok" : "dim", "judge " + (r.judge_enabled ? "on" : "off")) +
      "</div></section>"
    );

    // cached banner: same log + same params, served from disk. judge
    // verdicts carry LLM variance even at temperature 0, so offer re-run.
    if (r.cached) {
      parts.push(
        "<section class='cached-banner'><div>" +
        badge("warn", "cached") +
        ' <span class="dim">同一日志指纹 + 同一参数的既有结果。事实层必然一致；judge 层有 LLM 方差（temperature=0 也不例外），重跑可能略有不同。</span>' +
        "</div>" +
        '<button class="btn small" id="rerun-btn" type="button">re-run fresh</button>' +
        "</section>"
      );
    }

    // next steps: summary + 1-3 suggested follow-ups, per delivery checklist
    if (r.suggestions && r.suggestions.length) {
      parts.push(
        "<section><h2>next steps</h2><ol class='suggestions'>" +
        r.suggestions.map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("") +
        "</ol></section>"
      );
    }

    // Supplied skill text, named before any finding that depends on it.
    var injectedList = r.injected_skills || [];
    var injectedNames = {};
    injectedList.forEach(function (s) { injectedNames[(s.name || "").toLowerCase()] = true; });
    function injectedMark(name) {
      if (!injectedNames[(name || "").toLowerCase()]) return "";
      return " " + badge("inject", "supplied") +
        '<span class="sr-only">：该 skill 原文为人工注入，非日志记录</span>';
    }
    if (injectedList.length) {
      var rows = injectedList.map(function (s) {
        return "<tr><td>" + esc(s.name) + injectedMark(s.name) + "</td>" +
          '<td class="dim">' + esc(s.source || "") + "</td>" +
          "<td>" + esc(s.description || "(空)") + "</td>" +
          "<td>" + (s.intent
            ? badge("warn", "stated, not observed") + " " + esc(s.intent)
            : '<span class="dim">none supplied</span>') + "</td></tr>";
      }).join("");
      parts.push(
        "<section id='injected'><h2>supplied skill text</h2>" +
        "<div class='injected-notice'>" +
        injectedList.length + " 个 skill 的原文由审计者提供，而非从扫描到的 SKILL.md 解析。" +
        "文本是真实的、出处明确，但<strong>会话日志从未记录过它</strong>——" +
        "对这些 skill 的结论同时是对所提供文本的结论，不能读成 agent 见过这段话。" +
        "</div>" +
        "<table><tr><th>skill</th><th>source</th><th>description</th>" +
        "<th>stated purpose</th></tr>" + rows + "</table></section>"
      );
    }

    if (r.expectations.length) {
      var rows = r.expectations.map(function (e) {
        var cls = e.status === "loaded" ? "ok" : e.status === "file-read" ? "warn" : "bad";
        return "<tr><td>" + esc(e.name) + injectedMark(e.name) + "</td><td>" + badge(cls, e.status) + "</td></tr>";
      }).join("");
      parts.push("<section id='expectations'><h2>expectations " + helpDot("expect") + "</h2><table><tr><th>skill</th><th>status</th></tr>" + rows + "</table></section>");
    }

    var missed = r.judgments.missed;
    if (missed.length || r.judge_enabled) {
      var body = missed.length
        ? missed.map(function (j) {
            // The verdict is only as sound as the wording it was judged
            // against, so supplied wording has to travel with the finding.
            var caveat = injectedNames[(j.skill_name || "").toLowerCase()]
              ? "<div class='injected-notice'>本判定依据的是<strong>人工提供</strong>的 skill 原文，" +
                "不是从 SKILL.md 解析的文本。它说明会话与上面那段文本不匹配，" +
                "但不能说明 harness 当时提供了什么。</div>"
              : "";
            return "<details><summary>" + badge("bad", "missed") + " " + esc(j.skill_name) +
              injectedMark(j.skill_name) +
              " <span class='dim'>turn " + esc(j.turn) + " · conf " + j.confidence.toFixed(2) + "</span></summary>" +
              caveat +
              "<p>" + esc(j.rationale) + "</p><blockquote>" + esc(j.evidence) + "</blockquote></details>";
          }).join("")
        : '<p class="dim">no missed triggers among judged skills.</p>';
      if (r.judgments.errors.length) {
        body += '<p class="dim">defects: ' + esc(r.judgments.errors.join(" · ")) + "</p>";
      }
      parts.push("<section id='findings'><h2>findings " + helpDot("findings") + "</h2>" + body + "</section>");
    }

    if (r.if_results.length) {
      r.if_results.forEach(function (res) {
        if (res.not_applicable) {
          parts.push("<section><h2>instruction following " + helpDot("if") + "</h2><p class='dim'>" + esc(res.playbook) + ": no enumerable steps, n/a.</p></section>");
          return;
        }
        var cls = res.passed ? "ok" : "bad";
        var vrows = res.verdicts.map(function (v, i) {
          var vcls = v.status === "covered" ? "ok" : v.status === "partial" ? "warn" : "bad";
          return "<tr><td>" + (i + 1) + "</td><td>" + esc(v.step.slice(0, 120)) + "</td><td>" +
            badge(vcls, v.status) + "</td><td>" + esc((v.evidence || v.rationale || "").slice(0, 140)) + "</td></tr>";
        }).join("");
        parts.push(
          "<section id='if'><h2>instruction following · " + esc(res.playbook) + " " + helpDot("if") + "</h2>" +
          "<p>" + badge(cls, res.coverage.toFixed(2) + " / gate " + res.gate.toFixed(2)) + "</p>" +
          "<table><tr><th>#</th><th>step</th><th>status</th><th>evidence</th></tr>" + vrows + "</table></section>"
        );
      });
    }

    if (r.timeline.loads.length || r.timeline.file_reads.length) {
      var noSelf = {};
      (r.no_self_invoke || []).forEach(function (n) { noSelf[n.toLowerCase()] = true; });
      var items = r.timeline.loads.map(function (l) {
        var ns = noSelf[l.skill_name.toLowerCase()]
          ? " " + badge("warn", "disable-model-invocation: 无法从日志区分点名/自调，需对照路由规则核对")
          : "";
        // render rule: the lifecycle view has no body evidence, so no cost
        // number is shown here at all (cost lives in the skill loads table)
        return "<details><summary>" + badge("fact", "loaded") + " " + esc(l.skill_name) +
          injectedMark(l.skill_name) +
          " <span class='dim'>" + esc(fmtTs(l.ts)) +
          (l.evicted_by != null ? " · evicted?" : "") + "</span></summary>" +
          "<pre>" + esc(l.content_head) + "</pre>" + (ns ? "<p>" + ns + "</p>" : "") + "</details>";
      }).join("");
      items += r.timeline.file_reads.map(function (f) {
        return "<details><summary>" + badge("warn", "file-read") + " " + esc(f.skill_name) +
          injectedMark(f.skill_name) +
          " <span class='dim'>" + esc(f.origin) + "</span></summary><pre>" + esc(f.snippet_head) + "</pre></details>";
      }).join("");
      parts.push("<section><h2>skill lifecycle " + helpDot("lifecycle") + "</h2>" + (items || '<p class="dim">none detected.</p>') + "</section>");
    }

    // IR 1.1 evidence-backed load ledger: the only place a load cost number
    // is rendered, always labeled EST or unavailable
    if (r.skill_loads) {
      var sl = r.skill_loads;
      var loadRows = sl.rows.map(function (row) {
        var cost = row.cost_basis === "body" && row.cost_tokens_est != null
          ? "~" + row.cost_tokens_est + " (EST)"
          : '<span class="unavailable">unavailable</span>';
        return "<tr><td>" + esc(row.load_id) + "</td><td>" + esc(row.skill_id) +
          "</td><td>" + esc(row.agent_id) + "</td><td>" + esc(fmtTs(row.ts)) +
          "</td><td>" + esc(row.channel || "—") + "</td><td>" +
          (row.kind === "reload" ? badge("warn", "reload") : esc(row.kind)) +
          "</td><td>" + cost + "</td></tr>";
      }).join("");
      var loadTotals = sl.totals;
      parts.push(
        "<section id='loads'><h2>skill loads (evidence-backed) " + helpDot("loads") + "</h2>" +
        (loadRows
          ? "<table><tr><th>load</th><th>skill</th><th>agent</th><th>time</th><th>channel</th><th>kind</th><th>cost</th></tr>" +
            loadRows + "</table>"
          : '<p class="dim">no load evidence rows.</p>') +
        "<p class='dim'>loads " + loadTotals.loads + " · reloads " + loadTotals.reloads +
        " · unavailable " + loadTotals.unavailable +
        " · redundant bodies (no row) " + loadTotals.redundant_bodies +
        " · cost ~" + loadTotals.cost_tokens_est + " (EST)" +
        (loadTotals.unavailable ? " · unavailable rows carry the marker, not a guess" : "") +
        "</p></section>"
      );
    }

    // declared requires vs observed co-loads: the library feedback loop.
    // Names are joined before pairs are compared (namespaced observed ids
    // do not equal catalog names), and the join refuses to guess.
    if (r.library) {
      var lib = r.library;
      var ldiff = lib.diff || {};
      var ljoin = lib.join || {};
      var ltotals = ldiff.totals || {};
      var overD = ldiff.over_declared || [];
      var impli = ldiff.implicit || [];
      var consi = ldiff.consistent || [];
      var lh = "";
      if (ltotals.observed === 0 && ltotals.declared === 0) {
        lh += "<p class='dim'>既无声明 requires 对，也无共加载观测（listing 之类不算加载）。</p>";
      } else {
        var odShown = overD.slice(0, 20);
        lh += "<p><b>一致</b>（声明且观测到）</p>" + auditRows(consi, [
          { label: "a", get: function (e) { return esc(e.a); } },
          { label: "b", get: function (e) { return esc(e.b); } },
          { label: "w", get: function (e) { return String(e.weight); } },
        ]);
        lh += "<p><b>过度声明</b>（声明了但从未观测到共载）</p>" + auditRows(odShown, [
          { label: "a", get: function (e) { return esc(e.a); } },
          { label: "b", get: function (e) { return esc(e.b); } },
        ]);
        if (overD.length > odShown.length) {
          lh += "<p class='dim'>…… 另有 " + (overD.length - odShown.length) + " 对</p>";
        }
        var imShown = impli.slice(0, 20);
        lh += "<p><b>隐式共载</b>（观测到但 catalog 未声明）</p>" + auditRows(imShown, [
          { label: "a", get: function (e) { return esc(e.a); } },
          { label: "b", get: function (e) { return esc(e.b); } },
          { label: "w", get: function (e) { return String(e.weight); } },
        ]);
        if (impli.length > imShown.length) {
          lh += "<p class='dim'>…… 另有 " + (impli.length - imShown.length) + " 对</p>";
        }
      }
      var renamedN = ljoin.renamed ? Object.keys(ljoin.renamed).length : 0;
      if (renamedN) {
        lh += "<p class='dim'>" + renamedN +
          " 个观测 id 按唯一命名空间后缀归一到声明名</p>";
      }
      if (ljoin.ambiguous && ljoin.ambiguous.length) {
        lh += "<p class='dim'>" + ljoin.ambiguous.length +
          " 个观测 id 无法唯一归属声明名（拒绝猜测），未参与对比：" +
          esc(ljoin.ambiguous.slice(0, 10).join(", ")) +
          (ljoin.ambiguous.length > 10 ? " …" : "") + "</p>";
      }
      parts.push("<section id='library'><h2>声明 vs 观测 · requires ↔ 共加载 " +
        helpDot("library") + "</h2>" +
        "<p class='dim'>声明 " + (ltotals.declared || 0) + " 对 · 观测 " +
        (ltotals.observed || 0) + " 对 · 隐式阈值 w≥" +
        (ltotals.implicit_min_weight || 2) +
        "（单次出现视为巧合）；w = 出现该组合的 agent 数</p>" +
        lh + "</section>");
    }

    // Skill GAP: SKILL.md expectation vs IR actual; judgments are LLM output
    // carried with verbatim evidence — never presented as facts
    if (r.skill_gaps) {
      var g = r.skill_gaps;
      var gapCards = g.missed_triggers.map(function (m) {
        return "<details><summary>" + esc(m.name) +
          " <span class='dim'>turn " + (m.turn == null ? "—" : m.turn) +
          " · confidence " + Number(m.confidence).toFixed(2) + "</span></summary>" +
          "<p>" + esc(m.rationale) + "</p><pre>" + esc(m.evidence) + "</pre></details>";
      }).join("");
      var unusedRows = g.loaded_unused.map(function (row) {
        return "<tr><td>" + esc(row.name) + "</td><td>" +
          (row.description ? esc(row.description) : "<span class='dim'>no catalog entry</span>") +
          "</td><td>" + row.bodies + "</td><td>" + row.stubs + "</td></tr>";
      }).join("");
      var irOnlyRows = g.join.ir_only.map(function (row) {
        return "<tr><td>" + esc(row.skill_id) + "</td><td>" + row.listed +
          "</td><td>" + row.loaded + "</td><td>" + row.executed + "</td></tr>";
      }).join("");
      var unj = g.unjudged;
      parts.push(
        "<section id='gaps'><h2>skill gaps " + helpDot("gaps") + "</h2>" +
        "<p class='dim'>catalog matched " + g.join.matched + " · IR-only " + g.join.ir_only.length +
        " · catalog-only " + g.join.catalog_only + " · listed " + g.sets.listed +
        " / loaded " + g.sets.loaded + " / executed " + g.sets.executed + "</p>" +
        (unj.judge_unconfigured
          ? "<p class='dim'>judge off — trigger GAPs not evaluated.</p>"
          : ((unj.count || unj.judge_errors)
            ? "<p class='dim'>unjudged " + unj.count + " (prefiltered " + unj.prefiltered +
              " · over limit " + unj.over_limit + ") · judge defects " + unj.judge_errors + "</p>"
            : "")) +
        (gapCards ? "<h3>missed triggers</h3>" + gapCards : "") +
        (unusedRows ? "<h3>loaded but never executed</h3><table><tr><th>skill</th><th>declared use</th><th>bodies</th><th>stubs</th></tr>" + unusedRows + "</table>" : "") +
        (irOnlyRows ? "<h3>IR-only (no catalog entry)</h3><table><tr><th>skill</th><th>listed</th><th>loaded</th><th>executed</th></tr>" + irOnlyRows + "</table>" : "") +
        g.notes.map(function (n) { return "<p class='dim'>" + esc(n) + "</p>"; }).join("") +
        "</section>");
    }

    parts.push(renderTokenStats(r.token_stats));

    var KIND_CLS = { user: "user", think: "think", say: "say", tool: "tool",
                     result: "dim", compact: "bad", status: "dim", sys: "dim" };
    var KIND_ORDER = ["user", "think", "say", "tool", "result", "compact", "status", "sys"];
    var feed = r.event_feed.map(function (e) {
      var origin = e.origin !== "main" ? ' <span class="event-origin">[' + esc(e.origin) + "]</span>" : "";
      var kind = '<span class="kind-badge kind-' + (KIND_CLS[e.kind] || "dim") + '">' + esc(e.kind) + "</span>";
      return '<div class="event-row" data-kind="' + esc(e.kind) + '"><div class="event-ts">' + esc(fmtTs(e.ts)) + "</div><div>" +
        kind + " " + esc(e.text) + origin + "</div></div>";
    }).join("");
    // filter bar: one toggle per kind present, with counts; reusing the
    // kind-badge palette so a filter chip matches the rows it selects
    var counts = {};
    r.event_feed.forEach(function (e) { counts[e.kind] = (counts[e.kind] || 0) + 1; });
    var chips = KIND_ORDER.filter(function (k) { return counts[k]; }).map(function (k) {
      return '<button type="button" class="kind-badge kind-' + (KIND_CLS[k] || "dim") +
        ' feed-filter-btn" data-kind="' + esc(k) + '" aria-pressed="false">' +
        esc(k) + " " + counts[k] + "</button>";
    }).join("");
    parts.push("<section><h2>event feed " + helpDot("feed") + "</h2>" +
      '<div class="feed-filter">' + chips + "</div>" +
      "<div class='event-feed'>" + feed + "</div>" +
      '<div class="never-ship">never ship: judgments without verbatim evidence · ' +
      "plans counted as actions · model-reported arithmetic</div></section>");

    report.innerHTML = parts.join("");
    var rerun = document.getElementById("rerun-btn");
    if (rerun) rerun.addEventListener("click", rerunFresh);
  }

  // delegated filter: click a kind chip to keep only those rows, click the
  // active chip again (or another chip) to switch; rows carry data-kind
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest(".feed-filter-btn") : null;
    if (!btn) return;
    var bar = btn.parentNode;
    var kind = btn.getAttribute("data-kind");
    var turningOff = btn.getAttribute("aria-pressed") === "true";
    var btns = bar.querySelectorAll(".feed-filter-btn");
    btns.forEach(function (b) { b.setAttribute("aria-pressed", "false"); b.classList.remove("active"); });
    var feedBox = bar.parentNode.querySelector(".event-feed");
    if (!feedBox) return;
    feedBox.querySelectorAll(".event-row").forEach(function (row) {
      var match = turningOff || row.getAttribute("data-kind") === kind;
      row.classList.toggle("hidden", !match);
    });
    if (!turningOff) {
      btn.setAttribute("aria-pressed", "true");
      btn.classList.add("active");
    }
  });

  loadSessions();
})();
