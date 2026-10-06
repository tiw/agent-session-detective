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
      "[subagent:x] 标记表示事件来自子代理。点筛选条上的角色只显示该类，再点一次恢复全部。",
    "tokens":
      "token 治理四指标，全部从日志确定性抽取：缓存命中率（usage.record / token_usage，" +
      "全 agent 合计）；每轮增量形状（linear = 每轮加恒定 token，sublinear = 增量在缩，" +
      "accelerating = 增量在涨）；四桶分解（system 桶是残差推断，其余是按事件内容估的）；" +
      "prompt 哈希翻转（llm.request 的 systemPromptHash/toolsHash 中途变化 = 前缀缓存失效）。"
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
        return;
      }
      auditForm.elements.expect.value = t.expect;
      var select = auditForm.elements.playbook;
      for (var i = 0; i < select.options.length; i++) {
        if (select.options[i].value.indexOf(t.playbook) !== -1) {
          select.value = select.options[i].value;
          break;
        }
      }
    });
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
  // sessions sidebar, always visible
  // ------------------------------------------------------------------

  function loadSessions() {
    setStatus("scanning sessions…");
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (data) {
      // sidebar is discovery-only: turns/skills/token stats are computed
      // on demand by the audit job, not here. fleet stays hidden until
      // the fleet button asks for it.
      document.getElementById("fleet").hidden = true;
      sessionList.innerHTML = "";
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
          (s.cached ? badge("ok", "audited") : "") +
          "</div>";
        el.addEventListener("click", function () { openAudit(s, el); });
        sessionList.appendChild(el);
      });
      setStatus(data.sessions.length + " recent sessions (of " + (data.total || data.sessions.length) + "). pick one.");
    }).catch(function (e) {
      setStatus("error: " + e.message);
    });
  }

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
            "repeat +" + fleet.total_repeat_extra_tokens);
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
      expect: (auditForm.elements.expect.value || "").split(",").map(function (s) {
        return s.trim();
      }).filter(Boolean),
      steps: auditForm.elements.playbook.value ? [auditForm.elements.playbook.value] : [],
      judge_triggers: auditForm.elements.judge.checked,
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
      : t.growth_verdict.indexOf("insufficient") === 0 ? "dim" : "bad";
    badges.push(badge(vcls, "growth: " + t.growth_verdict));
    if (t.hash_runs && t.hash_runs.length) {
      badges.push(badge(t.hash_flips ? "bad" : "ok", "prompt flips " + t.hash_flips));
    }
    if (t.repeats && t.repeats.length) {
      badges.push(badge("warn", "repeat injection ~" + t.repeat_extra_tokens + " tok"));
    }
    parts.push("<div class='session-meta'>" + badges.join("") + "</div>");

    if (t.bucket_shares && Object.keys(t.bucket_shares).length) {
      var order = ["system", "history", "injected", "output"];
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
        var maxAdded = Math.max.apply(null, grew.map(function (r) { return r.added; })) || 1;
        var bars = grew.map(function (r) {
          var observed = r.system_added + r.history_added + r.injected_added + r.output_added;
          var scale = observed ? Math.min(1, r.added / observed) : 1;
          var segs = order.map(function (k) {
            var v = (r[k + "_added"] || 0) * scale;
            return v > 0 ? '<span class="bar-seg seg-' + k + '" style="height:' +
              Math.max(2, Math.round(v / maxAdded * 56)) + 'px"></span>' : "";
          }).join("");
          return '<div class="bar" title="T' + r.turn + " +" + r.added + '">' + segs + "</div>";
        }).join("");
        var legend = order.map(function (k) {
          return '<span><span class="seg-' + k + '"></span>' + esc(k) + "</span>";
        }).join("");
        parts.push('<div class="bar-chart">' + bars + '</div>');
        parts.push('<div class="bar-legend">' + legend +
          '<span class="dim">system 为残差推断 · 被压缩跨越的轮次已剔除</span></div>');
      }
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
      var rrows = t.repeats.map(function (r) {
        return "<tr><td>" + r.occurrences + "×</td><td>~" + r.tokens_each + "</td><td>~" +
          r.extra_tokens + '</td><td class="dim">' + esc(r.preview.slice(0, 100)) + "</td></tr>";
      }).join("");
      parts.push("<details><summary>" + badge("warn", "repeat injection") + " " + t.repeats.length +
        " 组 · ~" + t.repeat_extra_tokens + " extra tokens</summary>" +
        "<table><tr><th>次数</th><th>每次~tokens</th><th>多付</th><th>内容</th></tr>" + rrows + "</table>" +
        '<p class="dim">同一份工具结果（≥200 字符）被多次读入 = 状态未外置的可见信号。</p></details>');
    }
    return "<section><h2>token governance " + helpDot("tokens") + "</h2>" + parts.join("") + "</section>";
  }

  function renderReport(r) {
    var parts = [];
    parts.push(
      "<section><h2>summary " + helpDot("summary") + "</h2><div class='session-meta'>" +
      badge("dim", "turns " + r.session.turns) +
      badge("dim", "events " + r.session.events) +
      badge("fact", "loaded " + r.timeline.loads.length) +
      badge("warn", "file-read " + r.timeline.file_reads.length) +
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

    if (r.expectations.length) {
      var rows = r.expectations.map(function (e) {
        var cls = e.status === "loaded" ? "ok" : e.status === "file-read" ? "warn" : "bad";
        return "<tr><td>" + esc(e.name) + "</td><td>" + badge(cls, e.status) + "</td></tr>";
      }).join("");
      parts.push("<section><h2>expectations " + helpDot("expect") + "</h2><table><tr><th>skill</th><th>status</th></tr>" + rows + "</table></section>");
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
      parts.push("<section><h2>findings " + helpDot("findings") + "</h2>" + body + "</section>");
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
          "<section><h2>instruction following · " + esc(res.playbook) + " " + helpDot("if") + "</h2>" +
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
        return "<details><summary>" + badge("fact", "loaded") + " " + esc(l.skill_name) +
          " <span class='dim'>" + esc(fmtTs(l.ts)) + " · ~" + l.tokens_est + " tokens" +
          (l.evicted_by != null ? " · evicted?" : "") + "</span></summary>" +
          "<pre>" + esc(l.content_head) + "</pre>" + (ns ? "<p>" + ns + "</p>" : "") + "</details>";
      }).join("");
      items += r.timeline.file_reads.map(function (f) {
        return "<details><summary>" + badge("warn", "file-read") + " " + esc(f.skill_name) +
          " <span class='dim'>" + esc(f.origin) + "</span></summary><pre>" + esc(f.snippet_head) + "</pre></details>";
      }).join("");
      parts.push("<section><h2>skill lifecycle " + helpDot("lifecycle") + "</h2>" + (items || '<p class="dim">none detected.</p>') + "</section>");
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
