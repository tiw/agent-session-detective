/* agent session detective web ui */
(function () {
  "use strict";

  var statusLine = document.getElementById("status-line");
  var sessionList = document.getElementById("session-list");
  var viewSessions = document.getElementById("view-sessions");
  var viewAudit = document.getElementById("view-audit");
  var auditTitle = document.getElementById("audit-title");
  var auditForm = document.getElementById("audit-form");
  var progress = document.getElementById("progress");
  var progressText = document.getElementById("progress-text");
  var report = document.getElementById("report");

  var currentSession = null;
  var pollTimer = null;

  // ------------------------------------------------------------------
  // section help: what each block means and how to read it
  // ------------------------------------------------------------------

  var HELP = {
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
      "[subagent:x] 标记表示事件来自子代理。"
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
  // sessions
  // ------------------------------------------------------------------

  function loadSessions() {
    setStatus("scanning sessions…");
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (data) {
      var rows = data.sessions.map(function (s, i) {
        var delay = Math.min(i, 12) * 40; // stagger 40ms, capped
        var el = document.createElement("div");
        el.className = "session-row";
        el.style.animationDelay = delay + "ms";
        el.innerHTML =
          '<div class="session-path">' + esc(s.path) + "</div>" +
          '<div>' + badge("dim", s.workspace) + "</div>" +
          '<div class="session-meta">' +
          badge("dim", "turns " + (s.turns == null ? "?" : s.turns)) +
          badge("dim", "events " + (s.events == null ? "?" : s.events)) +
          badge("fact", "loaded " + (s.skills_loaded == null ? "?" : s.skills_loaded)) +
          badge(s.skill_files_read ? "warn" : "dim", "file-read " + (s.skill_files_read == null ? "?" : s.skill_files_read)) +
          '<span>' + esc(fmtTs(s.mtime)) + "</span>" +
          "</div>";
        el.addEventListener("click", function () { openAudit(s); });
        return el;
      });
      sessionList.innerHTML = "";
      rows.forEach(function (r) { sessionList.appendChild(r); });
      setStatus(rows.length + " sessions scanned. pick one.");
    }).catch(function (e) {
      setStatus("error: " + e.message);
    });
  }

  // ------------------------------------------------------------------
  // audit view
  // ------------------------------------------------------------------

  function openAudit(session) {
    currentSession = session;
    viewSessions.hidden = true;
    viewAudit.hidden = false;
    report.innerHTML = "";
    progress.hidden = true;
    auditTitle.textContent = session.id;
    setStatus("ready. " + session.path);
    loadPlaybooks();
  }

  function loadPlaybooks() {
    var select = auditForm.elements.playbook;
    fetch("/api/playbooks").then(function (r) { return r.json(); }).then(function (data) {
      select.innerHTML = '<option value="">none</option>';
      data.playbooks.forEach(function (p) {
        var o = document.createElement("option");
        o.value = p;
        o.textContent = p.split("/").slice(-2).join("/");
        select.appendChild(o);
      });
    });
  }

  document.getElementById("back").addEventListener("click", function () {
    viewAudit.hidden = true;
    viewSessions.hidden = false;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    setStatus("pick one.");
  });

  document.getElementById("refresh").addEventListener("click", loadSessions);

  auditForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (!currentSession) return;
    var params = {
      path: currentSession.path,
      expect: (auditForm.elements.expect.value || "").split(",").map(function (s) {
        return s.trim();
      }).filter(Boolean),
      steps: auditForm.elements.playbook.value ? [auditForm.elements.playbook.value] : [],
      judge_triggers: auditForm.elements.judge.checked,
    };
    report.innerHTML = "";
    progress.hidden = false;
    progressText.textContent = "queued";
    setStatus("audit running…");
    fetch("/api/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }).then(function (r) { return r.json(); }).then(function (data) {
      pollJob(data.job_id, 0);
    }).catch(function (e) {
      progress.hidden = true;
      setStatus("error: " + e.message);
    });
  });

  function pollJob(jobId, attempt) {
    fetch("/api/job/" + jobId).then(function (r) { return r.json(); }).then(function (job) {
      progressText.textContent = job.status + (job.status === "running" ? " (" + Math.round(attempt * 2) + "s)" : "");
      if (job.status === "done") {
        progress.hidden = true;
        renderReport(job.result);
        setStatus(job.result.status_line + " · " + job.result.duration_s + "s");
        return;
      }
      if (job.status === "error") {
        progress.hidden = true;
        setStatus("audit error: " + job.error);
        return;
      }
      pollTimer = setTimeout(function () { pollJob(jobId, attempt + 1); }, 2000);
    });
  }

  // ------------------------------------------------------------------
  // report rendering
  // ------------------------------------------------------------------

  function renderReport(r) {
    var parts = [];
    parts.push(
      "<section><h2>summary " + helpDot("summary") + "</h2>" + "<div class='session-meta'>" +
      badge("dim", "turns " + r.session.turns) +
      badge("dim", "events " + r.session.events) +
      badge("fact", "loaded " + r.timeline.loads.length) +
      badge("warn", "file-read " + r.timeline.file_reads.length) +
      badge("dim", "catalog " + r.catalog_size) +
      badge(r.judge_enabled ? "ok" : "dim", "judge " + (r.judge_enabled ? "on" : "off")) +
      "</div></section>"
    );

    if (r.expectations.length) {
      var rows = r.expectations.map(function (e) {
        var cls = e.status === "loaded" ? "ok" : e.status === "file-read" ? "warn" : "bad";
        return "<tr><td>" + esc(e.name) + "</td><td>" + badge(cls, e.status) + "</td></tr>";
      }).join("");
      parts.push("<section><h2>expectations " + helpDot("expect") + "</h2>" + "<table><tr><th>skill</th><th>status</th></tr>" + rows + "</table></section>");
    }

    var missed = r.judgments.missed;
    if (missed.length || r.judge_enabled) {
      var body = missed.length
        ? missed.map(function (j) {
            return "<details><summary>" + badge("bad", "missed") + " " + esc(j.skill_name) +
              " <span class='dim'>turn " + esc(j.turn) + " · conf " + j.confidence.toFixed(2) + "</span></summary>" +
              "<p>" + esc(j.rationale) + "</p><blockquote>" + esc(j.evidence) + "</blockquote></details>";
          }).join("")
        : '<p class="dim">no missed triggers among judged skills.</p>';
      if (r.judgments.errors.length) {
        body += '<p class="dim">defects: ' + esc(r.judgments.errors.join(" · ")) + "</p>";
      }
      parts.push("<section><h2>findings " + helpDot("findings") + "</h2>" + "" + body + "</section>");
    }

    if (r.if_results.length) {
      r.if_results.forEach(function (res) {
        if (res.not_applicable) {
          parts.push("<section><h2>instruction following " + helpDot("if") + "</h2>" + "<p class='dim'>" + esc(res.playbook) + ": no enumerable steps, n/a.</p></section>");
          return;
        }
        var cls = res.passed ? "ok" : "bad";
        var vrows = res.verdicts.map(function (v, i) {
          var vcls = v.status === "covered" ? "ok" : v.status === "partial" ? "warn" : "bad";
          return "<tr><td>" + (i + 1) + "</td><td>" + esc(v.step.slice(0, 120)) + "</td><td>" +
            badge(vcls, v.status) + "</td><td>" + esc((v.evidence || v.rationale || "").slice(0, 140)) + "</td></tr>";
        }).join("");
        parts.push(
          "<section><h2>instruction following · " + esc(res.playbook) + " " + helpDot("if") + "</h2>" +
          "<p>" + badge(cls, res.coverage.toFixed(2) + " / gate " + res.gate.toFixed(2)) + "</p>" +
          "<table><tr><th>#</th><th>step</th><th>status</th><th>evidence</th></tr>" + vrows + "</table></section>"
        );
      });
    }

    if (r.timeline.loads.length || r.timeline.file_reads.length) {
      var items = r.timeline.loads.map(function (l) {
        return "<details><summary>" + badge("fact", "loaded") + " " + esc(l.skill_name) +
          " <span class='dim'>" + esc(fmtTs(l.ts)) + " · ~" + l.tokens_est + " tokens" +
          (l.evicted_by != null ? " · evicted?" : "") + "</span></summary>" +
          "<pre>" + esc(l.content_head) + "</pre></details>";
      }).join("");
      items += r.timeline.file_reads.map(function (f) {
        return "<details><summary>" + badge("warn", "file-read") + " " + esc(f.skill_name) +
          " <span class='dim'>" + esc(f.origin) + "</span></summary><pre>" + esc(f.snippet_head) + "</pre></details>";
      }).join("");
      parts.push("<section><h2>skill lifecycle " + helpDot("lifecycle") + "</h2>" + "" + (items || '<p class="dim">none detected.</p>') + "</section>");
    }

    var KIND_CLS = { user: "user", think: "think", say: "say", tool: "tool",
                     result: "dim", compact: "bad", status: "dim", sys: "dim" };
    var feed = r.event_feed.map(function (e) {
      var origin = e.origin !== "main" ? ' <span class="event-origin">[' + esc(e.origin) + "]</span>" : "";
      var kind = '<span class="kind-badge kind-' + (KIND_CLS[e.kind] || "dim") + '">' + esc(e.kind) + "</span>";
      return '<div class="event-row"><div class="event-ts">' + esc(fmtTs(e.ts)) + "</div><div>" +
        kind + " " + esc(e.text) + origin + "</div></div>";
    }).join("");
    parts.push("<section><h2>event feed " + helpDot("feed") + "</h2>" + "<div class='event-feed'>" + feed + "</div>" +
      '<div class="never-ship">never ship: judgments without verbatim evidence · ' +
      "plans counted as actions · model-reported arithmetic</div></section>");

    report.innerHTML = parts.join("");
  }

  loadSessions();
})();
