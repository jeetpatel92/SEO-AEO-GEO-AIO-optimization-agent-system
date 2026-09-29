"""End to end behaviour on the simulated site: the happy path and every error route."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from helpers import Harness
from sago import policy
from sago.agents import coach, conductor
from sago.core import halted, writes_paused
from sago.errors import TransientError
from sago.llm import BudgetExceeded, LLMUnavailable


class Base(unittest.TestCase):
    def setUp(self):
        self.h = None

    def tearDown(self):
        if self.h:
            self.h.close()


class HappyPath(Base):
    def test_two_runs_take_the_site_to_100(self):
        self.h = Harness()
        before = self.h.psi()
        self.assertLess(before["mobile"]["scores"]["performance"], 0.7)
        self.assertGreater(before["desktop"]["metrics"]["cls"], 1)
        self.assertEqual(conductor.daily_run(self.h.ctx)["status"], "ok")
        self.h.advance(days=1)
        conductor.daily_run(self.h.ctx)
        after = self.h.psi()
        for s in ("mobile", "desktop"):
            self.assertEqual(after[s]["scores"]["performance"], 1.0)
            self.assertEqual(after[s]["scores"]["best-practices"], 1.0)
            self.assertLess(after[s]["metrics"]["cls"], 0.1)
        self.assertTrue(self.h.site.forms_ok())
        statuses = {a["status"] for a in self.h.actions()}
        self.assertEqual(statuses, {"verified"})

    def test_titles_are_policy_clean_and_rendered(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        seo = [a for a in self.h.actions() if a["kind"] == "set_seo"]
        self.assertTrue(seo)
        for a in seo:
            page = self.h.ctx.crawler.fetch(a["payload"]["url"])
            self.assertEqual(page["title"], a["payload"]["new_title"])
            self.assertFalse(page["title_has_dash"] or page["description_has_dash"])
            self.assertEqual(policy.lint_copy(page["title"], "title", self.h.ctx.cfg, [a["payload"]["current_title"], page["h1"][0]]), [])

    def test_second_run_same_day_does_nothing(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        self.assertEqual(conductor.daily_run(self.h.ctx)["status"], "already_ran")

    def test_page_is_not_rewritten_while_its_outcome_is_pending(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        n = len([a for a in self.h.actions() if a["kind"] == "set_seo"])
        self.h.advance(days=1)
        conductor.daily_run(self.h.ctx)
        self.assertEqual(len([a for a in self.h.actions() if a["kind"] == "set_seo"]), n)

    def test_theme_source_is_promoted_after_a_verified_deploy(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        src = self.h.ctx.builder.source
        self.assertNotIn("min-h-screen", (src / "front-page.php").read_text())
        self.assertNotIn("fonts.googleapis.com", (src / "inc/assets.php").read_text())

    def test_report_lists_content_briefs_for_people(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        report = next((self.h.tmp / "data" / "reports").glob("run-*.md")).read_text()
        self.assertIn("For people", report)
        self.assertIn("AIO_CRAWLER_BLOCKED", report)
        self.assertIn("SEARCH_CONTENT_GAP", report)


class Failures(Base):
    def _breaking_engineer(self, payload):
        return {"edits": [{"path": "front-page.php", "find": "<?php get_header(); ?>",
                           "replace": "<?php get_header(); /* BREAK_FORMS */ ?>", "why": "test"}], "summary": "breaks forms"}

    def test_failed_verification_rolls_back_quarantines_and_learns(self):
        self.h = Harness({"engineer": self._breaking_engineer})
        conductor.daily_run(self.h.ctx)
        dep = [a for a in self.h.actions() if a["kind"] == "deploy_theme"][0]
        self.assertEqual(dep["status"], "rolled_back")
        self.assertTrue(self.h.site.forms_ok(), "rollback must restore working forms")
        self.assertEqual(self.h.site.theme["version"], "3.0.0")
        self.assertTrue(self.h.ctx.store.quarantined("deploy_theme"))
        self.assertIsNone(halted(self.h.ctx.store))
        rules = coach.learn_from_incidents(self.h.ctx)
        self.assertEqual(len(rules), 1)
        self.assertTrue(policy.check_action({**dep, "payload": dep["payload"]}, self.h.ctx.cfg))

    def test_rollback_failure_trips_the_global_halt(self):
        self.h = Harness({"engineer": self._breaking_engineer})

        def boom(sid):
            raise RuntimeError("restore endpoint returned 500")
        self.h.ctx.cms.restore = boom
        res = conductor.daily_run(self.h.ctx)
        self.assertEqual(res["status"], "error")
        self.assertIsNotNone(halted(self.h.ctx.store))
        self.assertTrue(any(n["level"] == "critical" for n in self.h.ctx.notify.sent))
        self.h.advance(days=1)
        self.assertEqual(conductor.daily_run(self.h.ctx)["status"], "halted")

    def test_ip_block_pauses_writes_instead_of_touching_the_site(self):
        self.h = Harness()
        self.h.site.up = False            # we cannot reach it...
        self.h.ctx.vantage.reachable = True  # ...but everyone else can
        self.assertEqual(conductor.daily_run(self.h.ctx)["status"], "blocked")
        self.assertIsNotNone(writes_paused(self.h.ctx))
        self.assertEqual(self.h.actions(), [])

    def test_site_down_alerts_and_skips(self):
        self.h = Harness()
        self.h.site.up = False
        self.h.ctx.vantage.reachable = False
        self.assertEqual(conductor.daily_run(self.h.ctx)["status"], "site_down")
        self.assertTrue(any(n["level"] == "critical" for n in self.h.ctx.notify.sent))

    def test_model_outage_still_ships_the_code_only_fixes(self):
        self.h = Harness({r: LLMUnavailable("credit balance is too low") for r in
                          ("copywriter", "gatekeeper", "engineer", "search_analyst", "ai_visibility", "speed_doctor", "coach")})
        res = conductor.daily_run(self.h.ctx)
        self.assertEqual(res["status"], "partial")
        done = {a["kind"] for a in self.h.actions(statuses=("verified",))}
        self.assertIn("set_option", done)
        self.assertNotIn("set_seo", done)

    def test_budget_exhaustion_behaves_like_an_outage(self):
        self.h = Harness({"copywriter": BudgetExceeded("run budget reached")})
        res = conductor.daily_run(self.h.ctx)
        self.assertEqual(res["status"], "partial")

    def test_cms_credentials_rejected_opens_the_breaker(self):
        self.h = Harness()
        self.h.ctx.cms.fail_auth = True
        conductor.daily_run(self.h.ctx)
        self.assertTrue(self.h.ctx.store.kv_get("breaker:adapter:cms"))
        self.assertTrue(self.h.site.options["sago.form_fonts"] == "custom", "nothing was written")

    def test_transient_failures_are_retried_then_dead_lettered(self):
        self.h = Harness(settings={"retries.max_attempts": 2, "retries.base_seconds": 0})
        self.h.ctx.cms.fail_next = [TransientError("503")] * 50
        conductor.daily_run(self.h.ctx)
        dead = self.h.actions(statuses=("dead",))
        self.assertTrue(dead)
        self.assertTrue(any("gave up" in n["title"] for n in self.h.ctx.notify.sent))

    def test_deploys_wait_for_the_deploy_window(self):
        noon = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)  # 12:00 in Asia/Kolkata
        self.h = Harness(clock=noon)
        conductor.daily_run(self.h.ctx)
        dep = [a for a in self.h.actions() if a["kind"] == "deploy_theme"][0]
        self.assertEqual(dep["status"], "reviewed")
        self.h.clock["now"] = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)  # 03:30 next day
        conductor.daily_run(self.h.ctx)
        self.assertEqual(self.h.ctx.store.action(dep["id"])["status"], "verified")

    def test_tiers_can_require_a_human(self):
        self.h = Harness(settings={"autopilot.approval_required": ["T3"]})
        conductor.daily_run(self.h.ctx)
        dep = [a for a in self.h.actions() if a["kind"] == "deploy_theme"][0]
        self.assertEqual(dep["status"], "awaiting_approval")
        self.h.ctx.store.update_action(dep["id"], status="approved")
        self.h.advance(days=1)
        conductor.daily_run(self.h.ctx)
        self.assertEqual(self.h.ctx.store.action(dep["id"])["status"], "verified")

    def test_dry_run_writes_nothing(self):
        self.h = Harness()
        self.h.ctx.cfg.dry_run = True
        conductor.daily_run(self.h.ctx)
        self.assertEqual(self.h.site.options["sago.form_fonts"], "custom")
        self.assertEqual(self.h.site.theme["version"], "3.0.0")


class Learning(Base):
    def test_coach_measures_against_control_and_reverts_a_losing_title(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        geo = [a for a in self.h.actions() if a["kind"] == "set_seo" and a["target"] == "/services/geo/"][0]
        for row in self.h.gsc.data["pages"]:
            if row["key"].endswith("/services/geo/"):
                row.update(impressions=40, ctr=0.0)
            if row["key"].endswith(("/about/", "/contact-us/", "/")) and row["key"].count("/") <= 4:
                row.update(ctr=row["ctr"] + 0.02)  # the rest of the site improved: control moved up
        self.h.advance(days=31)
        conductor.daily_run(self.h.ctx)
        out = coach.evaluate_outcomes(self.h.ctx)
        verdict = next(o for o in out if o["id"] == geo["id"])["verdict"]
        self.assertEqual(verdict, "loss")
        # The old description is shorter than today's policy allows, so the Coach asks for a fresh rewrite
        # that avoids the losing title instead of restoring non compliant copy.
        follow = [a for a in self.h.actions() if (a["payload"] or {}).get("revert_of") == geo["id"]
                  or a["fingerprint"] == f"rewrite-after-loss-{geo['id']}"]
        self.assertEqual(len(follow), 1)
        self.assertIn(geo["payload"]["new_title"], follow[0]["payload"]["avoid"])
        self.h.advance(days=1)
        conductor.daily_run(self.h.ctx)
        self.assertEqual(self.h.ctx.store.action(follow[0]["id"])["status"], "verified")
        copy_call = [p for r, p in self.h.llm.calls if r == "copywriter" and p.get("avoid_titles")]
        self.assertTrue(copy_call, "the Copywriter was told which title lost")

    def test_losing_title_with_compliant_old_copy_is_reverted(self):
        self.h = Harness()
        conductor.daily_run(self.h.ctx)
        rev = [a for a in self.h.actions() if a["kind"] == "set_seo" and a["target"] == "/handle-negative-google-reviews/"][0]
        for row in self.h.gsc.data["pages"]:
            if row["key"].endswith(("/about/", "/contact-us/")):
                row.update(ctr=row["ctr"] + 0.03)
        self.h.advance(days=31)
        conductor.daily_run(self.h.ctx)
        coach.evaluate_outcomes(self.h.ctx)
        revert = [a for a in self.h.actions() if (a["payload"] or {}).get("revert_of") == rev["id"]]
        self.assertEqual(len(revert), 1)
        self.assertEqual(revert[0]["payload"]["new_title"], rev["before"]["seo_title"])
        self.h.advance(days=1)
        conductor.daily_run(self.h.ctx)
        self.assertEqual(self.h.ctx.store.action(revert[0]["id"])["status"], "verified")
        self.assertEqual(self.h.ctx.crawler.fetch(rev["target"])["title"], rev["before"]["seo_title"])

    def test_calibration_moves_priors_at_most_twenty_percent(self):
        self.h = Harness()
        store = self.h.ctx.store
        for i in range(5):
            aid = store.add_action(1, {"kind": "set_option", "tier": "T2", "target": f"site{i}", "payload": {"key": "k", "i": i},
                                       "status": "verified"})
            store.update_action(aid, outcome={"verdict": "win", "realised": 30.0, "estimated": 5.0})
        changes = coach.calibrate(self.h.ctx)
        self.assertEqual(changes[0]["new"], 1.2)

    def test_learned_rule_blocks_the_combination_not_each_part(self):
        self.h = Harness()
        store, cfg = self.h.ctx.store, self.h.ctx.cfg
        store.add_incident(1, "set_option", "set_option:forms_initialise",
                           {"payload": {"key": "sago.delay_scripts", "add": ["recaptcha/api.js", "lazysizes.min.js"]}})
        coach.learn_from_incidents(self.h.ctx)
        same = {"kind": "set_option", "tier": "T2", "payload": {"key": "sago.delay_scripts", "add": ["recaptcha/api.js", "lazysizes.min.js"]}}
        part = {"kind": "set_option", "tier": "T2", "payload": {"key": "sago.delay_scripts", "add": ["recaptcha/api.js"]}}
        self.assertTrue(any(v["code"] == "learned_rule" for v in policy.check_action(same, cfg)))
        self.assertFalse(any(v["code"] == "learned_rule" for v in policy.check_action(part, cfg)))


if __name__ == "__main__":
    unittest.main()
