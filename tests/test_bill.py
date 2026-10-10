"""Bill summary (P0-1) — pure-function tests over dict fixtures.

The honesty rules under test: shares only within the EST subtotal,
no volume → row omitted (never zero-filled), None means no evidence,
billed totals kept out of EST math.
"""

import unittest

from agent_session_detective.bill import build_bill


def ts(**overrides):
    base = {
        "cache_hit_rate": 0.5,
        "input_total": 1000,
        "output_total": 500,
        "cache_read_total": 500,
        "growth_verdict": "linear",
        "turn_growth": [],
        "bucket_totals": {"system": 1000, "history": 0, "inject": 2000,
                          "skill": 3000, "tool": 4000, "output": 0},
        "bucket_shares": {},
        "hash_runs": [],
        "hash_flips": None,
        "repeats": [],
        "repeat_extra_tokens": 1000,
        "repeat_class_totals": {"post_compaction": 400},
        "compaction_source": "transcript",
        "compaction_points": [{"ts": "t1"}, {"ts": "t2"}],
        "usage_record_count": 3,
    }
    base.update(overrides)
    return base


LOADS = {"rows": [], "totals": {"loads": 2, "reloads": 1,
                                "unavailable": 1, "redundant_bodies": 0,
                                "cost_tokens_est": 500}, "channels": {}}


def returns(**overrides):
    base = {"rows": [], "totals": {"dispatches": 2, "linked": 1,
                                   "return_tokens_total": 3000,
                                   "flagged": 1,
                                   "process_unavailable": 1}}
    base.update(overrides)
    return base


def bill(**overrides):
    params = {
        "token_stats": ts(),
        "skill_loads": LOADS,
        "returns": returns(),
        "expectations": [],
        "judgments": {"missed": [], "errors": []},
        "if_results": [],
        "billed_series": [],
        "file_read_count": 0,
        "adapter_id": "qoder",
        "judge_enabled": False,
    }
    params.update(overrides)
    return build_bill(**params)


class BuildBillTest(unittest.TestCase):
    def test_all_six_rows_sorted_by_est_volume_with_shares(self):
        out = bill()
        self.assertEqual([row["key"] for row in out["cost_rows"]],
                         ["context_growth", "subagent_returns",
                          "repeat_tax", "skill_load_cost", "output",
                          "cache_signal"])
        by_key = {row["key"]: row for row in out["cost_rows"]}
        # four round(..., 4) shares of 14500 drift up to ~1.0001 in total
        self.assertAlmostEqual(
            sum(row["share"] for row in out["cost_rows"]
                if row["share"] is not None), 1.0, delta=0.0003)
        self.assertEqual(out["est_subtotal"], 14500)
        self.assertEqual(by_key["context_growth"]["tokens_est"], 10000)
        self.assertEqual(by_key["context_growth"]["share"],
                         round(10000 / 14500, 4))
        self.assertEqual(by_key["context_growth"]["grade"], "推断")
        self.assertIn("attribution 90%", by_key["context_growth"]["detail"])
        self.assertIn("压缩 2 次(transcript)",
                      by_key["context_growth"]["detail"])
        self.assertEqual(by_key["cache_signal"]["grade"], "观测")
        self.assertIn("50%", by_key["cache_signal"]["detail"])
        self.assertIsNone(out["billed"])

    def test_rows_without_evidence_keep_null_numbers(self):
        out = bill(token_stats=ts(cache_hit_rate=None, usage_record_count=0,
                                  compaction_points=[],
                                  compaction_source=None))
        by_key = {row["key"]: row for row in out["cost_rows"]}
        self.assertIsNone(by_key["output"]["tokens_est"])
        self.assertIsNone(by_key["output"]["share"])
        self.assertIn("unavailable", by_key["output"]["detail"])
        self.assertIn("无缓存信号", by_key["cache_signal"]["detail"])
        self.assertIn("无证据源", by_key["context_growth"]["detail"])

    def test_residual_heavy_growth_and_billed_block(self):
        out = bill(
            token_stats=ts(
                cache_hit_rate=None,
                bucket_totals={"system": 9000, "history": 0, "inject": 1000,
                               "skill": 0, "tool": 0, "output": 0}),
            skill_loads={"rows": [], "totals": {
                "loads": 1, "reloads": 0, "unavailable": 1,
                "redundant_bodies": 0, "cost_tokens_est": 0},
                "channels": {}},
            billed_series=[
                {"ts": "t1", "prompt": 100, "completion": 20, "cached": 60},
                {"ts": "t2", "prompt": 200, "completion": 30, "cached": 100},
            ])
        by_key = {row["key"]: row for row in out["cost_rows"]}
        self.assertIn("attribution 10%", by_key["context_growth"]["detail"])
        self.assertIn("残差主导", by_key["context_growth"]["detail"])
        self.assertIsNone(by_key["skill_load_cost"]["tokens_est"])
        self.assertIn("1 unavailable", by_key["skill_load_cost"]["detail"])
        self.assertEqual(out["est_subtotal"], 14000)
        self.assertEqual(out["billed"]["requests"], 2)
        self.assertEqual(out["billed"]["prompt_total"], 300)
        self.assertEqual(out["billed"]["cached_share"], 0.5333)
        self.assertIn("账单口径", by_key["cache_signal"]["detail"])

    def test_qoder_without_billed_series_gets_the_howto_note(self):
        out = bill(billed_series=[])
        self.assertEqual(len(out["cost_rows"]), 6)
        self.assertEqual(out["notes"], [
            "账单通道未接入：勾选 billed usage（web）/ "
            "--billed-usage（CLI）后重跑可接入 Qoder 账单；"
            "已勾选仍见此提示，则是账单库本次不可用"])

    def test_non_qoder_adapter_gets_no_billed_note(self):
        out = bill(adapter_id="kimi-cli")
        self.assertEqual(out["notes"], [])

    def test_no_dispatches_omits_the_subagent_row(self):
        out = bill(returns=returns(rows=[], totals={
            "dispatches": 0, "linked": 0, "return_tokens_total": 0,
            "flagged": 0, "process_unavailable": 0}))
        self.assertEqual([row["key"] for row in out["cost_rows"]],
                         ["context_growth", "repeat_tax",
                          "skill_load_cost", "output", "cache_signal"])
        self.assertEqual(out["est_subtotal"], 11500)


EXPECT = [
    {"name": "a", "status": "loaded"},
    {"name": "b", "status": "missing"},
    {"name": "c", "status": "missing"},
    {"name": "d", "status": "file-read"},
]

IFS = [
    {"playbook": "p1", "coverage": 0.5, "gate": 0.8, "passed": False,
     "not_applicable": False, "verdicts": []},
    {"playbook": "p2", "coverage": 1.0, "gate": 0.8, "passed": True,
     "not_applicable": True, "verdicts": []},
]

JUDGMENTS = {"missed": [{"skill_name": "ghost"}], "errors": []}


class RoutingTest(unittest.TestCase):
    def test_judge_on_renders_all_four_lines(self):
        routing = bill(expectations=EXPECT, judgments=JUDGMENTS,
                       if_results=IFS, file_read_count=4,
                       judge_enabled=True)["routing"]
        self.assertEqual(
            [(l["label"], l["value"], l["grade"], l["anchor"])
             for l in routing["lines"]],
            [("expect 缺失", "2/4", "判断", "#expectations"),
             ("judge 命中", "1", "判断", "#findings"),
             ("IF 覆盖", "0.50", "判断", "#if"),
             ("SKILL.md 直读", "4 次", "事实", "#loads")])
        self.assertTrue(routing["enabled"])

    def test_judge_off_keeps_fact_lines_only(self):
        routing = bill(expectations=EXPECT, file_read_count=4,
                       judge_enabled=False)["routing"]
        self.assertEqual([l["label"] for l in routing["lines"]],
                         ["expect 缺失", "SKILL.md 直读"])
        self.assertFalse(routing["enabled"])

    def test_no_signals_renders_no_lines(self):
        routing = bill()["routing"]
        self.assertEqual(routing["lines"], [])
        self.assertFalse(routing["enabled"])


if __name__ == "__main__":
    unittest.main()
