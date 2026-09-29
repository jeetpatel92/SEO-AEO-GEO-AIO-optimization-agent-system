"""Units: policy, knowledge patterns, recipes, parsers."""
from __future__ import annotations

import io
import unittest
import zipfile

from helpers import Harness
from sago import knowledge, policy
from sago.io.gsc import parse_export
from sago.io.page import parse_page
from sago.io.psi import median_run, normalise


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.cfg = cls.h.ctx.cfg

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def codes(self, text, field="title", sources=()):
        return {v["code"] for v in policy.lint_copy(text, field, self.cfg, list(sources))}

    def test_dashes_are_rejected(self):
        self.assertIn("dash", self.codes("Multi-Location SEO Services for Brands | Northwind Growth"))
        self.assertIn("dash", self.codes("Local SEO Services for Growing Brands — Northwind Growth"))

    def test_lengths(self):
        self.assertIn("length", self.codes("Short | Northwind Growth"))
        self.assertIn("length", self.codes("x " * 40, "description"))

    def test_invented_numbers_are_rejected_but_page_numbers_pass(self):
        self.assertIn("new_numbers", self.codes("Grow Traffic 250% With Local SEO Services | Northwind Growth"))
        self.assertNotIn("new_numbers", self.codes("Grow Traffic 250% With Local SEO Services | Northwind Growth",
                                                   sources=["We grew traffic 250% for a retailer"]))

    def test_brand_suffix_and_banned_phrases(self):
        self.assertIn("brand", self.codes("Generative Engine Optimization Services for Brands"))
        self.assertIn("banned_phrase", self.codes("Guaranteed rankings for Local SEO Services | Northwind Growth"))

    def test_forbidden_and_allowlists(self):
        c = lambda a: {v["code"] for v in policy.check_action(a, self.cfg)}  # noqa: E731
        self.assertIn("forbidden", c({"kind": "delete_user", "tier": "T3", "payload": {}}))
        self.assertIn("option_key", c({"kind": "set_option", "tier": "T2", "payload": {"key": "siteurl", "value": "x"}}))
        self.assertIn("plugin", c({"kind": "toggle_plugin", "tier": "T2", "payload": {"slug": "wordfence", "active": False}}))
        self.assertIn("never_delay", c({"kind": "set_option", "tier": "T2",
                                        "payload": {"key": "sago.delay_scripts", "add": ["jquery.min.js"]}}))
        self.assertIn("checks", c({"kind": "deploy_theme", "tier": "T3", "payload": {"checks": {"ok": False}}}))
        self.assertIn("dangerous_code", c({"kind": "deploy_theme", "tier": "T3",
                                           "payload": {"checks": {"ok": True}, "diff_text": "+ wp_delete_user(1);"}}))
        self.assertIn("tier", c({"kind": "deploy_theme", "tier": "T1", "payload": {"checks": {"ok": True}}}))


class KnowledgeTests(unittest.TestCase):
    def test_perf_patterns_on_the_simulated_site(self):
        h = Harness()
        try:
            codes = {f["code"] for f in knowledge.perf_patterns(h.psi(), h.ctx.cfg.site)}
        finally:
            h.close()
        self.assertTrue({"PERF_VH_STRETCH", "PERF_EXTERNAL_FONTS", "PERF_FORM_PLUGIN_FONTS",
                         "PERF_CAPTCHA_ON_LOAD", "PERF_TAGS_ON_LOAD", "PERF_FONT_SWAP_SHIFT"} <= codes)

    def test_vh_recipe(self):
        src = '<section class="relative min-h-screen flex"><div style="min-height:55vh;padding:1rem">'
        out = knowledge.recipe_vh_hero_fix(src)
        self.assertNotIn("min-h-screen", out)
        self.assertIn("min-height:32rem", out)
        self.assertEqual(knowledge.recipe_vh_hero_fix(out), out, "recipes must be idempotent")

    def test_font_display_recipe(self):
        out = knowledge.recipe_font_display_swap("@font-face{font-family:X;src:url(x.woff2)}")
        self.assertIn("font-display:swap", out)

    def test_dashed_separator_detection(self):
        pages = [{"title": f"Page {i} - Acme"} for i in range(4)]
        self.assertTrue(knowledge.dashed_separator(pages, "Acme"))
        self.assertFalse(knowledge.dashed_separator([{"title": "Page | Acme"}] * 4, "Acme"))


class ParserTests(unittest.TestCase):
    def test_parse_page(self):
        html = ('<html><head><title>A &amp; B | X</title><meta name="description" content="D">'
                '<meta name="robots" content="index"><meta name="robots" content="index">'
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
                '<link rel="stylesheet" href="/f.css" media="print" onload="this.media=\'all\'">'
                '<script type="application/ld+json">{"@graph":[{"@type":"Organization","@id":"#o"},{"@type":"Organization","@id":"#o"}]}</script>'
                '<script type="application/ld+json">{broken</script></head>'
                '<body><section class="min-h-screen"><h1>Hi</h1></section><h2>Q?</h2><p>Answer here.</p>'
                '<form></form></body></html>')
        p = parse_page(html, "https://x/")
        self.assertEqual(p["title"], "A & B | X")
        self.assertEqual(len(p["robots"]), 2)
        self.assertEqual(p["blocking_css"], ["https://fonts.googleapis.com/css2?family=Inter"])
        self.assertEqual([n["type"] for n in p["jsonld_nodes"]], ["Organization", "Organization"])
        self.assertEqual(p["jsonld_bad"], 1)
        self.assertEqual(p["external_fonts"], ["fonts.googleapis.com"])
        self.assertEqual(p["h2"][0], {"text": "Q?", "next": "Answer here."})
        self.assertEqual(p["forms"], 1)

    def test_psi_normalise_maps_culprit_geometry(self):
        lh = {"categories": {"performance": {"score": 0.49}},
              "configSettings": {"screenEmulation": {"width": 1350, "height": 940}},
              "fullPageScreenshot": {"nodes": {"page-0-SECTION": {"top": 0, "height": 13650, "width": 1335}}},
              "audits": {"cumulative-layout-shift": {"score": 0, "numericValue": 1.013},
                         "cls-culprits-insight": {"score": 0, "scoreDisplayMode": "metricSavings", "details": {"items": [
                             {"node": {"lhId": "page-0-SECTION", "selector": "main > section",
                                       "snippet": '<section class="min-h-screen">'}}]}}}}
        run = normalise(lh, "desktop")
        item = next(a for a in run["failing"] if a["id"] == "cls-culprits-insight")["items"][0]
        self.assertEqual(item["node"]["rect"]["height"], 13650)
        self.assertEqual(run["metrics"]["cls"], 1.013)
        self.assertEqual(run["viewport"]["height"], 940)
        codes = {f["code"] for f in knowledge.perf_patterns({"desktop": run}, {})}
        self.assertIn("PERF_VH_STRETCH", codes)

    def test_median_run(self):
        runs = [{"scores": {"performance": s}, "metrics": {"cls": c}} for s, c in ((0.5, 1.0), (0.9, 0.0), (0.7, 0.1))]
        m = median_run(runs)
        self.assertEqual(m["scores"]["performance"], 0.7)
        self.assertEqual(m["metrics"]["cls"], 0.1)

    def test_gsc_export_parsing(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Queries.csv", "Top queries,Clicks,Impressions,CTR,Position\nexample query,0,412,0%,27.5\n")
            z.writestr("Pages.csv", "Top pages,Clicks,Impressions,CTR,Position\nhttps://x/,21,84,25%,6.1\n")
        buf.seek(0)
        path = "/tmp/_sago_test_export.zip"
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        d = parse_export(path)
        self.assertEqual(d["kind"], "performance")
        self.assertEqual(d["queries"][0], {"key": "example query", "clicks": 0.0, "impressions": 412.0, "ctr": 0.0, "position": 27.5})
        self.assertAlmostEqual(d["pages"][0]["ctr"], 0.25)


if __name__ == "__main__":
    unittest.main()


class ClaudeClientTests(unittest.TestCase):
    """The real ClaudeLLM against a stub client: request shape and typed failures."""

    def _llm(self, response=None, error=None):
        from types import SimpleNamespace as NS
        from sago.llm import ClaudeLLM

        calls = []

        class Messages:
            def create(self, **kw):
                calls.append(kw)
                if error:
                    raise error
                return response

        client = NS(beta=NS(messages=Messages()), messages=Messages())
        h = Harness()
        self.addCleanup(h.close)
        return ClaudeLLM(h.ctx.cfg, client=client), calls

    @staticmethod
    def _resp(text='{"verdict": "pass", "reasons": [], "risk": "low"}', stop="end_turn"):
        from types import SimpleNamespace as NS
        return NS(stop_reason=stop, stop_details=NS(category="cyber"), content=[NS(type="text", text=text)],
                  usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))

    def test_request_shape(self):
        from sago.agents.gatekeeper import SCHEMA
        llm, calls = self._llm(self._resp())
        out = llm.structured("gatekeeper", {"x": 1}, SCHEMA)
        self.assertEqual(out["verdict"], "pass")
        kw = calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"]["effort"], "high")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["fallbacks"], "default")

    def test_refusal_and_truncation_are_typed(self):
        from sago.agents.gatekeeper import SCHEMA
        from sago.llm import LLMError, LLMRefusal
        llm, _ = self._llm(self._resp(stop="refusal"))
        with self.assertRaises(LLMRefusal):
            llm.structured("gatekeeper", {}, SCHEMA)
        llm, _ = self._llm(self._resp(stop="max_tokens"))
        with self.assertRaises(LLMError):
            llm.structured("gatekeeper", {}, SCHEMA)

    def test_house_pack_is_byte_stable(self):
        h = Harness()
        self.addCleanup(h.close)
        self.assertEqual(h.ctx.cfg.house_pack(), h.ctx.cfg.house_pack())
