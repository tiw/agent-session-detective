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
      "<section><h2>summary</h2><div class='session-meta'>" +
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
      parts.push("<section><h2>expectations</h2><table><tr><th>skill</th><th>status</th></tr>" + rows + "</table></section>");
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
      parts.push("<section><h2>findings</h2>" + body + "</section>");
    }

    if (r.if_results.length) {
      r.if_results.forEach(function (res) {
        if (res.not_applicable) {
          parts.push("<section><h2>instruction following</h2><p class='dim'>" + esc(res.playbook) + ": no enumerable steps, n/a.</p></section>");
          return;
        }
        var cls = res.passed ? "ok" : "bad";
        var vrows = res.verdicts.map(function (v, i) {
          var vcls = v.status === "covered" ? "ok" : v.status === "partial" ? "warn" : "bad";
          return "<tr><td>" + (i + 1) + "</td><td>" + esc(v.step.slice(0, 120)) + "</td><td>" +
            badge(vcls, v.status) + "</td><td>" + esc((v.evidence || v.rationale || "").slice(0, 140)) + "</td></tr>";
        }).join("");
        parts.push(
          "<section><h2>instruction following · " + esc(res.playbook) + "</h2>" +
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
      parts.push("<section><h2>skill lifecycle</h2>" + (items || '<p class="dim">none detected.</p>') + "</section>");
    }

    var feed = r.event_feed.map(function (e) {
      var origin = e.origin !== "main" ? ' <span class="event-origin">[' + esc(e.origin) + "]</span>" : "";
      return '<div class="event-row"><div class="event-ts">' + esc(fmtTs(e.ts)) + "</div><div>" +
        esc(e.text) + origin + "</div></div>";
    }).join("");
    parts.push("<section><h2>event feed</h2><div class='event-feed'>" + feed + "</div>" +
      '<div class="never-ship">never ship: judgments without verbatim evidence · ' +
      "plans counted as actions · model-reported arithmetic</div></section>");

    report.innerHTML = parts.join("");
  }

  loadSessions();
})();
