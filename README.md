# SEO, AEO, GEO, AIO Optimization Agent System

A multi agent system that audits a website for classic search (SEO), answer engines (AEO), generative engines such as ChatGPT, Perplexity, Gemini and Copilot (GEO) and Google's AI Overviews and AI Mode (AIO), then fixes what it finds, verifies every change on the live site, rolls back anything that regresses, and learns from the measured outcomes. It runs on a schedule without a person in the loop. Built for YuvGro, an AI search and marketing consultancy, on Claude.

The package is called `sago` (SEO, AEO, GEO, AIO Optimization).

## Why this exists

Getting a site to PageSpeed 100 and keeping it there, fixing titles that rank but do not get clicked, keeping one clean entity in the structured data, and making sure answer engines can actually read the site is a long list of small, fiddly, risky changes. Most of them break something else when done carelessly: a script delayed to speed up the page stops the contact form working; switching off one schema plugin quietly turns another one back on. This system does that work continuously, and treats every change as an experiment with a rollback.

## Proven on a real site

Built from the playbook used to take yuvgro.com (WordPress, custom theme, Yoast, LiteSpeed Cache, Forminator) from PageSpeed **60 mobile / 49 desktop** to **100 / 100 / 100 / 100 with Agentic Browsing 3/3 on both devices**, confirmed over three consecutive PSI runs. Every failure mode met along the way is now a detector, a fix, a guard or a test in this repository. The desktop CLS of 1.013 is the best example: a hero sized at `min-h-screen` stretched to 13,650 pixels when Lighthouse resized the viewport for its full page screenshot. The Speed Doctor finds that pattern from the geometry PSI reports, and the Engineer's `vh_hero_fix` recipe fixes it.

## Pipeline

```
 03:30 daily (site's time zone)
      │
  CONDUCTOR ─── go / no go: global halt, write pause, site health (down, or is it just our IP?)
      │
   SCOUT ────── paced crawl as a guest, CMS inventory, PSI (median of 3 per device),
      │         Search Console (API or UI export), AI crawler access
      │
      ├── SPEED DOCTOR ──── PageSpeed and Core Web Vitals root causes
      ├── TECH AUDITOR ──── status, robots, canonicals, schema conflicts, copy rules
      ├── SEARCH ANALYST ── CTR gaps at page one positions, query clusters, content gaps
      └── AI VISIBILITY ─── crawler access, entity consistency, answerability
      │
  STRATEGIST ── ranks every finding into capped actions with risk tiers and a hypothesis
      │
  COPYWRITER ◄─┐   titles and descriptions          ENGINEER: theme changes, built and
      │        │   (revise, at most 2 rounds)       tested in a scratch copy, packaged
  GATEKEEPER ──┘   policy in code, then an independent review: pass / revise / kill
      │
   OPERATOR ─── applies through the WordPress connector, stores before and after, purges cache
      │
   VERIFIER ─── checks the live page as a guest, compared with the baseline
      │   └──► failure: OPERATOR rolls back, incident recorded, kind quarantined
      ▼
   COACH (daily outcomes, weekly tuning) ─ measures each change against control pages,
            reverts losers, calibrates estimates, turns incidents into rules
```

## The agents

| Agent | Kind | Receives | Decides | Hands on |
|---|---|---|---|---|
| Conductor | code | the schedule and state | whether to run; routes every error type to one response | the run, the report |
| Scout | code | the run brief | reachability (down, or just us), which sources are usable | one snapshot per source |
| Speed Doctor | code + Claude | PSI runs with culprit geometry | root cause and fix class per failing audit | performance findings |
| Tech Auditor | code | the crawl and CMS inventory | broken invariants; site wide or per page | technical findings |
| Search Analyst | code + Claude | Search Console data and titles | CTR gaps in striking distance; query clusters and intent | rewrite and content brief findings |
| AI Visibility | code + Claude | crawler probes, schema, headings | crawler access, entity consistency, answerability | AEO, GEO, AIO findings |
| Strategist | code | all findings and the change history | tier, priority, caps, what not to touch yet, the hypothesis | planned actions |
| Copywriter | Claude | page, queries, policy, exemplars | the wording | drafted titles and descriptions |
| Engineer | code + Claude | theme recipes and briefs | the code change; runs build, tests and smoke render | a tested, versioned zip |
| Gatekeeper | code + independent Claude | a drafted action | pass, revise or kill | the verdict |
| Operator | code | an approved action | nothing about content; executes and records before and after | the applied action |
| Verifier | code | the applied action and the baseline | pass or fail by tier specific checks | verified, or rolled back |
| Coach | code + Claude | outcomes, incidents, tuning history | bounded tuning, reverts, new rules | tunables, rules, exemplars, digest |

Each module's docstring states its role, inputs, decisions and handoff. [`docs/architecture.md`](docs/architecture.md) has the full contracts, the state machine and the error routing.

## Risk tiers and autonomy

| Tier | Examples | Verified by |
|---|---|---|
| T0 | report only: content briefs, host issues | nothing to verify |
| T1 | SEO title, meta description | the guest page renders exactly the new copy; no dashes; HTTP 200 |
| T2 | site settings through the connector: form fonts, delayed scripts, schema owner, title separator | site invariants against the baseline; every form initialises |
| T3 | theme deploy | all of the above, PSI shows no regression, one schema owner |

By default every tier runs unattended (`autopilot.approval_required: []`). Any tier can be made to wait for a human with one setting. T3 deploys only happen inside the deploy window, at most once per run and three times a week, and freeze automatically after repeated failures.

## When things go wrong

| Fault | Response |
|---|---|
| Site unreachable from us but fine from external checks | pause writes 24 hours and alert: our IP is blocked by the host, the site is fine |
| Site down everywhere | skip the run, critical alert |
| CMS, PSI or Search Console credentials rejected | open that adapter's breaker for 12 hours; the rest of the run continues |
| Claude unavailable or the run budget spent | stop generating; code only fixes and already reviewed actions still ship |
| A single refusal or policy violation | reject that action only |
| Transient network or server error | retry with backoff; after the maximum attempts the action is dead lettered and reported |
| Verification fails | automatic rollback, incident recorded, the kind quarantined for 14 days, deploy failures count toward the T3 freeze |
| Rollback fails | global halt and a critical alert: the one situation that needs a person |
| Nothing has run for 36 hours | dead man's switch alert |

## How it improves over time

1. **Every change is an experiment.** Each action carries a hypothesis: which metric, from what baseline, measured when. Performance changes are measured at verification; title changes after a full Search Console window.
2. **Outcomes against control pages.** A title's CTR change is compared with the median change on pages that were not touched, so seasonality is not mistaken for impact. Losing titles are reverted automatically (or rewritten, if the old copy breaks current policy), and the page cools down for one window.
3. **Calibrated priorities.** Realised impact divided by estimated impact, per action kind, becomes the Strategist's prior. Bounded to 20% change a week and reverted if results drop.
4. **Incidents become rules.** A rollback's exact change becomes a blocking rule the Gatekeeper enforces from then on.
5. **Exemplars.** Titles that measurably won are shown to the Copywriter as style references; losers as warnings.
6. **Limits on learning.** The Coach can write `tunables.json`, `learned_rules.json` and the exemplar banks. It has no write path to `policy.json` or `settings.json`, so it cannot learn its way out of the non negotiables.

## Try it without keys

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd engine && ../.venv/bin/python -m sago demo
```

The demo runs the whole loop against a simulated WordPress site that has the same faults as the real one (viewport sized hero, third party fonts, form plugin fonts, captcha and tag manager on load, duplicate schema, a dashed title template, GPTBot blocked):

```
Before   mobile: perf 59, BP 96, CLS 0.037  desktop: perf 41, BP 96, CLS 1.013
Day 1    ok: {'verified': 9}
Day 2    ok: {'verified': 1}
After    mobile: perf 100, BP 100, CLS 0.0  desktop: perf 100, BP 100, CLS 0.0
```

Day 2 exists because switching off one schema plugin turned another's output back on; the next run found and fixed that, as happened on the real site.

Tests (37, no network):

```bash
cd engine && PYTHONPATH=.:tests ../.venv/bin/python -m unittest discover -s tests
```

Connector smoke test inside real WordPress (PHP in WebAssembly via WordPress Playground, needs Node):

```bash
python3 connector/tests/run_smoke.py
```

## Running it for real

See [`docs/setup.md`](docs/setup.md): install the connector plugin, create an Application Password, add a PSI key and a Search Console service account, write a site profile, do a dry run, then deploy (a `railway.json` is included).

## Layout

```
engine/sago/            the agents, adapters and state
engine/sago/agents/     one module per agent
engine/sago/io/         PSI, Search Console, crawler, CMS connector client, fakes
engine/sago/knowledge.py  known patterns and fix recipes
engine/prompts/         the house prompt and one per model using role
engine/policy.json      non negotiables (the Coach cannot write here)
engine/settings.json    schedule, caps, models, budgets
engine/tunables.json    what the Coach may tune, within bounds
connector/sago-connector/  the WordPress plugin
```

## Design decisions

* **Code decides what is allowed; Claude decides what is good.** Policy, allowlists, tiers and caps are code. Models write copy, diagnose unknown audits, cluster queries and review.
* **Compare with the baseline, never with perfection.** A change fails verification only if it made something worse, so existing problems never block unrelated fixes.
* **One schema owner, one change at a time per page, one deploy per run.** Every outcome stays attributable.
* **A connector, not a browser.** The server side plugin enforces the allowlist, snapshots before every change and swaps themes atomically, so the engine never needs admin passwords or screen scraping.
* **Content is for people.** The system rewrites metadata and ships technical fixes; content gaps and answer blocks go to the report as briefs.

## License

AGPL 3.0. See [LICENSE](LICENSE).
