"""The bill summary (P0-1): what did this session cost, and did the
right things load.

A pure function over already-serialized blocks — ``token_stats``,
``skill_loads``, ``subagent_returns``, the expectation/judgment/IF
summaries, and the optional billed series. Imports nothing from
``ir/`` or ``billing``; the web layer owns every join.

Honesty rules (spec):
- EST/observed numbers are never divided by billed denominators;
  shares are computed only within the EST subtotal.
- A row with no evidence keeps ``tokens_est: None`` — never
  zero-filled; the UI renders the unavailable marker.
- The billed block is session-scoped and never mixed into EST math.
"""

from __future__ import annotations

EST_ROW_KEYS = ("context_growth", "repeat_tax", "skill_load_cost",
                "subagent_returns")

GRADE_FACT = "事实"
GRADE_EVIDENCE = "证据"
GRADE_EST = "推断"
GRADE_OBSERVED = "观测"
GRADE_JUDGMENT = "判断"


def _round_share(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole, 4)


def _pct(rate):
    if rate is None:
        return "unknown"
    return "%d%%" % round(rate * 100)


def _billed_block(billed_series):
    if not billed_series:
        return None
    prompt_total = sum(r.get("prompt") or 0 for r in billed_series)
    completion_total = sum(r.get("completion") or 0 for r in billed_series)
    cached_total = sum(r.get("cached") or 0 for r in billed_series)
    return {
        "requests": len(billed_series),
        "prompt_total": prompt_total,
        "completion_total": completion_total,
        "cached_total": cached_total,
        "cached_share": _round_share(cached_total, prompt_total),
        "note": "会话级账单口径（SharedClientCache），不与日志侧 EST 混算",
    }


def _routing(expectations, judgments, if_results, file_read_count,
             judge_enabled):
    lines = []
    if expectations:
        missing = sum(1 for e in expectations
                      if e.get("status") == "missing")
        lines.append({"label": "expect 缺失",
                      "value": "%d/%d" % (missing, len(expectations)),
                      "grade": GRADE_JUDGMENT,
                      "anchor": "#expectations"})
    if judge_enabled:
        missed = len((judgments or {}).get("missed") or [])
        lines.append({"label": "judge 命中", "value": str(missed),
                      "grade": GRADE_JUDGMENT, "anchor": "#findings"})
    applicable = [r for r in (if_results or [])
                  if not r.get("not_applicable")]
    if applicable:
        coverage = (sum(r.get("coverage") or 0.0 for r in applicable)
                    / len(applicable))
        lines.append({"label": "IF 覆盖", "value": "%.2f" % coverage,
                      "grade": GRADE_JUDGMENT, "anchor": "#if"})
    if file_read_count:
        lines.append({"label": "SKILL.md 直读",
                      "value": "%d 次" % file_read_count,
                      "grade": GRADE_FACT, "anchor": "#loads"})
    return {"lines": lines, "enabled": judge_enabled}


def _cost_rows(token_stats, skill_loads_block, returns_block,
               billed_series):
    rows = []
    buckets = token_stats.get("bucket_totals") or {}
    growth = sum(buckets.values())
    if growth:
        residual = buckets.get("system") or 0
        attributed = growth - residual
        detail = "attribution %s · 压缩 %s" % (
            _pct(_round_share(attributed, growth)),
            "%d 次(%s)" % (len(token_stats.get("compaction_points") or []),
                           token_stats.get("compaction_source"))
            if token_stats.get("compaction_source") else "无证据源")
        if residual * 2 > growth:
            detail += " · 残差主导：技能直读类成本可能藏在 system 残差里"
        rows.append({"key": "context_growth", "label": "上下文增长",
                     "tokens_est": growth, "share": None,
                     "detail": detail, "anchor": "#tokens",
                     "grade": GRADE_EST})
    extra = token_stats.get("repeat_extra_tokens") or 0
    if extra:
        post = ((token_stats.get("repeat_class_totals") or {})
                .get("post_compaction") or 0)
        rows.append({"key": "repeat_tax", "label": "重复注入税",
                     "tokens_est": extra, "share": None,
                     "detail": "压缩恢复 ~%d · 其余 ~%d"
                               "（extra 总量是重读税下界）"
                               % (post, max(0, extra - post)),
                     "anchor": "#tokens", "grade": GRADE_EST})
    load_totals = (skill_loads_block or {}).get("totals") or {}
    if load_totals.get("loads"):
        cost = load_totals.get("cost_tokens_est") or 0
        rows.append({"key": "skill_load_cost", "label": "技能加载成本",
                     "tokens_est": cost or None, "share": None,
                     "detail": "%d loads · %d unavailable · %d reloads"
                               % (load_totals.get("loads"),
                                  load_totals.get("unavailable"),
                                  load_totals.get("reloads")),
                     "anchor": "#loads", "grade": GRADE_EVIDENCE})
    return_totals = (returns_block or {}).get("totals") or {}
    if return_totals.get("dispatches"):
        total = return_totals.get("return_tokens_total") or 0
        rows.append({"key": "subagent_returns", "label": "subagent 返回",
                     "tokens_est": total or None, "share": None,
                     "detail": "%d 次派发 · %d 次臃肿（判定）"
                               " · %d 次过程量不可用"
                               % (return_totals.get("dispatches"),
                                  return_totals.get("flagged"),
                                  return_totals.get(
                                      "process_unavailable")),
                     "anchor": "#tokens", "grade": GRADE_FACT})
    if token_stats.get("usage_record_count"):
        output_tokens = token_stats.get("output_total")
        output_detail = "usage 遥测（%d 条记录）" % (
            token_stats["usage_record_count"])
    else:
        output_tokens = None
        output_detail = "无输出遥测，unavailable"
    rows.append({"key": "output", "label": "输出",
                 "tokens_est": output_tokens, "share": None,
                 "detail": output_detail, "anchor": "#tokens",
                 "grade": GRADE_OBSERVED})
    billed = _billed_block(billed_series)
    if token_stats.get("cache_hit_rate") is not None:
        cache_detail = "cache 命中 %s（日志 usage 口径）" % _pct(
            token_stats["cache_hit_rate"])
    elif billed is not None:
        cache_detail = "cache 命中 %s（账单口径）" % _pct(
            billed["cached_share"])
    else:
        cache_detail = "无缓存信号"
    rows.append({"key": "cache_signal", "label": "缓存信号",
                 "tokens_est": None, "share": None,
                 "detail": cache_detail, "anchor": "#tokens",
                 "grade": GRADE_OBSERVED})
    return rows


def build_bill(token_stats, skill_loads, returns, expectations, judgments,
               if_results, billed_series, file_read_count, adapter_id,
               judge_enabled):
    """The bill: cost rows + routing lines + billed block + notes."""
    rows = _cost_rows(token_stats, skill_loads, returns, billed_series)
    by_key = {row["key"]: row for row in rows}
    est_subtotal = sum(by_key[key]["tokens_est"] or 0
                       for key in EST_ROW_KEYS if key in by_key)
    for key in EST_ROW_KEYS:
        row = by_key.get(key)
        if row is None or row["tokens_est"] is None:
            continue
        row["share"] = _round_share(row["tokens_est"], est_subtotal)
    volume = [by_key[key] for key in EST_ROW_KEYS if key in by_key]
    volume.sort(key=lambda row: -(row["tokens_est"] or 0))
    ordered = volume + [row for row in rows
                        if row["key"] not in EST_ROW_KEYS]
    notes = []
    if not billed_series and (adapter_id or "").startswith("qoder"):
        notes.append(
            "账单通道未接入：勾选 billed usage（web）/ "
            "--billed-usage（CLI）后重跑可接入 Qoder 账单；"
            "已勾选仍见此提示，则是账单库本次不可用")
    return {
        "cost_rows": ordered,
        "est_subtotal": est_subtotal,
        "routing": _routing(expectations, judgments, if_results,
                            file_read_count, judge_enabled),
        "billed": _billed_block(billed_series),
        "notes": notes,
    }
