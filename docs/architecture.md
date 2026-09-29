# Architecture

## Principles

1. **Code decides what is allowed, Claude decides what is good.** Every rule that must never bend (forbidden actions, allowlists, the copy policy, tiers, caps, the deploy window) is enforced in code before any model sees the action.
2. **Every change is reversible.** The Operator records the exact value before and after; the connector snapshots before writing and swaps themes atomically. Rollback is a normal code path, tested like any other.
3. **Verify against the baseline.** The Verifier compares the live site after a change with the same site at the start of the run. Existing problems do not fail unrelated changes; new ones always do.
4. **Every change is an experiment.** A hypothesis (metric, baseline, measurement date) is attached when the action is planned, and the Coach judges it later against control pages.
5. **Nothing happens silently.** Every agent writes to the events table; every error type has exactly one response; a dead man's switch catches the case where nothing runs.

## Agent contracts

### Conductor (code)
* **Receives:** the schedule tick, the kv state.
* **Decides:** go or no go (global halt, write pause, site health); the order of agents; which actions to carry over (queued by the Coach, interrupted by an outage, approved by a human, deferred to the deploy window); the response to each error type.
* **Hands on:** the run summary and a Markdown report per run (actions with before and after copy, content and host briefs for people).

### Scout (code)
* **Receives:** the run brief.
* **Decides:** whether the site is reachable. If our request times out, it asks external vantage points (`check-host.net`). Down everywhere means the site is down; fine elsewhere means our IP is blocked, which gets a write pause, not a fix.
* **Collects:** the paced guest crawl (default 1.5 s between requests), the CMS inventory (every post, page and custom type with its SEO fields and URL), PSI (median of three runs per device), Search Console (API, or the UI export zips dropped into `data/inbox/`, which is also the only source for the Generative AI features report), robots.txt, llms.txt and the status each AI crawler gets.
* **Hands on:** one stored snapshot per source. Each source fails on its own.

### Speed Doctor (code + Claude, high effort)
* **Receives:** PSI runs with failing audits, culprit nodes and their rectangles from the full page screenshot.
* **Decides:** known causes in code (`knowledge.perf_patterns`): a viewport sized section stretching under Lighthouse's resize, web font swap shifts, third party font stylesheets, a form plugin's own fonts, a captcha loaded on every view, tag managers during load, console errors. Only audits no pattern explains go to Claude, which returns a root cause and a fix class.
* **Hands on:** findings with evidence, estimated points, confidence and a fix.

### Tech Auditor (code)
* **Receives:** every crawled page.
* **Decides:** broken invariants (status, PHP errors, robots tags, canonical, duplicate or conflicting Organization nodes and repeated `@id`, invalid `JSON-LD`, dashes against the copy rule, title and description lengths, third party fonts, H1 count). Whether a title problem is one template separator for the whole site or page by page.
* **Hands on:** findings, deduplicated so a site wide fault is one finding.

### Search Analyst (code + Claude, medium effort)
* **Receives:** Search Console pages, queries, query by page, the previous period, current titles.
* **Decides:** in code, branded versus non branded, and the pages in striking distance (positions 3.5 to 20 by default) whose CTR is under half of what the position should earn. With Claude, intent clusters and whether each needs a title rewrite, a content brief or nothing.
* **Hands on:** rewrite findings carrying the target queries and the page's baseline metrics; content brief findings for the report.

### AI Visibility Analyst (code + Claude, high effort)
* **Receives:** crawler probes, robots.txt, llms.txt, schema nodes, question headings and the text under them, the Generative AI export.
* **Decides:** in code, blocked AI crawlers (a 429 or 403 usually comes from the host's bot protection, not robots.txt), disallow rules, missing llms.txt, entity gaps (no Organization, no logo, no author Person), old URLs still cited. With Claude, which question headings lack a direct answer.
* **Hands on:** findings; answerability becomes briefs for writers.

### Strategist (code)
* **Receives:** all findings, the CMS inventory, Search Console and PSI baselines, the change history.
* **Decides:** the action kind and tier; priority = estimated points × confidence × learned prior for the kind × area weight ÷ effort of the tier; whether it may run (quarantined, reverted recently, waiting for an earlier change's outcome, same change made this week, T3 frozen or over the weekly cap, tier cap for the run). All theme changes are merged into a single deploy.
* **Hands on:** planned actions with a hypothesis each.

### Copywriter (Claude, high effort)
* **Receives:** the page's headings and current copy, its queries, position and CTR, the copy policy, winning exemplars, titles to avoid, and revision feedback.
* **Decides:** the wording.
* **Hands on:** the drafted title and description.

### Engineer (code + Claude, xhigh effort)
* **Receives:** recipes and briefs for one deploy, the theme source.
* **Decides:** applies deterministic recipes (`vh_hero_fix`, `font_display_swap`); sends anything else to Claude as exact find and replace edits, only if the site allows free form patches, each edit matching exactly once and only in theme files. Runs the site's build, test and smoke commands in a scratch copy, then packages a versioned zip.
* **Hands on:** the zip, changed files, the diff and check results. After a verified deploy the workspace is promoted back to the theme source, so the next build starts from what is live.

### Gatekeeper (code + independent Claude, high effort)
* **Receives:** a drafted action.
* **Decides:** first in code (`policy.check_action`, `policy.lint_copy`): forbidden kinds, option and plugin allowlists, scripts that must never be delayed, protected options, learned rules, failed builds, dangerous code, dashes, lengths, banned phrases, invented numbers, the brand suffix. A non revisable violation kills; a revisable one goes back to the Copywriter (two rounds at most). Then Claude reviews copy for intent and truthfulness, and code for scope and risk. High risk patches are never deployed unattended. No review available means no pass.
* **Hands on:** pass, revise or kill.

### Operator (code)
* **Receives:** a passed action.
* **Decides:** nothing about content. Theme deploys wait for the deploy window.
* **Does:** reads the current value, writes the new one through the connector or Yoast's bulk editor endpoints (batches of five, always resending the existing focus keyphrase), stores before and after, purges the cache for affected URLs. `rollback()` restores `before` or the connector snapshot.

### Verifier (code)
* **Receives:** the applied action and the run baseline.
* **Checks by tier:** `title_rendered`, `no_dashes`, `page_200` (T1); `site_invariants`, `forms_initialise` (T2); plus `psi_no_regression` and `schema_single_owner` (T3). PSI is also measured for any action whose hypothesis is performance, and each action is measured against the state just before it, so gains are attributed correctly within a run.
* **Hands on:** a verified action with its outcome due date, or a failure that becomes a rollback.

### Coach (code + Claude, high effort)
* **Receives:** verified actions whose windows have passed, incidents, the tuning history.
* **Decides:** win, neutral, loss or not enough data (CTR change minus the median change of untouched control pages; performance points per device); reverts or rewrites losing titles and cools the page down; calibrates priors (bounded 0.3 to 3.0, at most 20% a week); reverts its own tuning if win rates fall below 85% of the baseline; turns each rollback into a blocking rule for that exact change; keeps exemplars.
* **Hands on:** `tunables.json`, `learned_rules.json`, exemplar banks and a weekly digest.

## Action state machine

```
planned ─► drafted ─► reviewed ─► (awaiting_approval ─► approved) ─► applying ─► applied ─► verified
   │          │           │                                              │           │
   │          └─► rejected (killed by policy or review)                  │           └─► rolled_back
   └─► failed (build failed, model error)            transient errors ──┴─► reviewed (retry) ─► dead
```

The idempotency fingerprint (kind, target, payload) stops the same change being opened twice. SQLite in WAL mode holds all state, so a restart resumes where it stopped.

## Error routing

| Error | Raised by | Response |
|---|---|---|
| `LocalBlock` | Scout | pause writes 24 h, alert with the fix (new IP or host support) |
| `SiteUnreachable` | Scout | skip the run, critical alert |
| `AuthError` | any adapter | that adapter's breaker opens for 12 h; the run continues without it |
| `LLMUnavailable`, `BudgetExceeded` | Claude client | stop generating; carry on with code only fixes and reviewed actions |
| `LLMRefusal`, `PolicyViolation` | Claude client, connector | reject that action |
| `NotNow` | Operator | keep the action for the next deploy window |
| `TransientError`, `RateLimited` | adapters | exponential backoff with jitter; dead letter after the maximum attempts |
| `VerificationFailed` | Verifier | rollback, incident, quarantine (T1 per page, others per kind); T3 counts toward the freeze |
| `RollbackFailed` | Conductor | global halt, critical alert |
| anything else | any agent | logged with its traceback, alert, run marked error |

## Security model

* The engine holds an Application Password for a dedicated administrator, never the owner's password.
* The connector exposes only allowlisted abstract settings (`sago.form_fonts`, `sago.delay_scripts`, `sago.css_excludes`, `sago.disable_yoast_schema`, `sago.disable_saswp`, `sago.title_separator`) and maps each to the real plugin option on the server. Anything else returns 403.
* Theme zips must contain only the active theme's directory; path traversal is refused. The previous theme is copied to an access denied backup directory before the swap.
* `policy.json` lists forbidden actions (deleting users or content, changing roles or passwords, noindexing the homepage, blocking all crawling, self serving review schema, and more) and scripts that must never be delayed.
* The Coach cannot write `policy.json` or `settings.json`.

## Models

Every role uses `claude-opus-5-5` with structured outputs (a JSON schema per role), a shared cached system prefix (house prompt, site profile, policy, learned rules; one hour TTL, byte stable), the role prompt cached next, per role effort, and server side refusal fallbacks. A per run token budget stops generation, not the run.
