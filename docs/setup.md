# Setup

## 1. Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd engine && ../.venv/bin/python -m sago demo    # sanity check, no keys needed
```

## 2. WordPress connector

1. Zip `connector/sago-connector/` and upload it at Plugins, Add New, Upload Plugin. Activate it.
2. Create a dedicated administrator user for the engine (not your own account).
3. As that user: Users, Profile, Application Passwords. Create one called SAGO and copy it.
4. Set `WP_USER` and `WP_APP_PASSWORD`.

Optional check against a throwaway WordPress in your terminal: `python3 connector/tests/run_smoke.py`.

## 3. Google

* **PageSpeed Insights:** create an API key for the PageSpeed Insights API in Google Cloud and set `PSI_API_KEY`. Without a key the shared quota runs out quickly.
* **Search Console:** create a service account, download its JSON key, set `GSC_SERVICE_ACCOUNT_FILE`, and add the service account's email as a user on the property (Settings, Users and permissions).
* **Generative AI features report:** the API does not expose it. Export it from Search Console and run `python -m sago import-gsc export.zip`, or drop the zip into `data/inbox/`.

## 4. Claude

Set `ANTHROPIC_API_KEY`, or run `ant auth login` once. The model and effort per role are in `engine/settings.json`.

## 5. Site profile

Copy `engine/sites/example.json` to `engine/sites/<yoursite>.json` and set `SAGO_SITE` to it.

| Field | Meaning |
|---|---|
| `base_url`, `brand`, `brand_terms` | used for branded query detection and the title suffix |
| `approved_facts` | the only figures copy may state beyond what the page itself says |
| `schema_owner` | `theme` if the theme emits the `JSON-LD`; plugin schema is then switched off when it duplicates |
| `contact_pages`, `form_selectors` | where the Verifier checks that forms still initialise |
| `theme.source_dir` | the theme's source directory; the Engineer works on a scratch copy |
| `theme.build_cmd`, `test_cmd`, `smoke_cmd` | run inside the scratch copy before any deploy |
| `theme.allow_freeform_patches` | lets Claude write theme edits beyond the built in recipes |
| `urls` | optional fixed list; otherwise the sitemap is used |

For better form checks, install Playwright (`pip install playwright && playwright install chromium`); without it the Verifier checks the delivered markup statically.

## 6. First runs

```bash
cd engine
../.venv/bin/python -m sago run --dry-run --force   # plans, drafts and reviews; writes nothing
../.venv/bin/python -m sago status
```

Read the report in `data/reports/`. To keep a person in the loop at first, set `autopilot.approval_required` to `["T2", "T3"]` in `settings.json` and approve with `python -m sago approve ID`. Empty the list when you trust it.

## 7. Deploy

The included `railway.json` runs `python -m sago serve`: the scheduler plus `/health` (and `/status` behind `SAGO_STATUS_TOKEN`). Mount a persistent volume and set `SAGO_DATA` to it. Set the notification variables (Slack or Telegram) to receive run summaries, rollbacks, halts and the weekly digest.

## 8. If it halts

A global halt only happens when a rollback fails. Check the site, fix what is broken, then `python -m sago resume`.
