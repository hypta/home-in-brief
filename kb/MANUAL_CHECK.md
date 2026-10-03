# Manual check list for knowledge-base sources

The weekly `kb-refresh` job only fetches sources whose `tos_status` in `kb/seed/sources.json`
is `allowed`. For every other source the job makes **no request** and records
`manual_check_required` in the item's `last_check`. A person must open the page and confirm the
quote. Terms were reviewed on 2026-10-03 (not legal advice; some terms were read from search
summaries only, see `tos_url` per source).

## How to check one item

1. Open `source_url` in a normal browser.
2. Find the `quote` word for word (case, quotes and dashes may differ).
3. If it is present and `claim_en` still matches the page: set `status` to `verified`, `verified_by`
   to your name and `verified_at` to today (this stays a human action; the job never edits item text).
4. If the wording changed or the page moved: set `status` to `needs_review`, update `quote` and
   `claim_en` from the new original text, then verify as above.
5. Keep the attribution from the source's `attribution` field wherever the quote is shown.

## Items that need manual checks

| Source (`tos_status`) | Items | Why manual | Cadence |
|---|---|---|---|
| `src_settled_*` settled.govt.nz (unclear) | kb_auction_unconditional, kb_leaky_build_era, kb_monolithic_cladding, kb_flood_insurance_limit, kb_insurance_condition, kb_title_freehold, kb_title_cross_lease, kb_title_unit_title | Real Estate Authority copyright: "personal or in-house use only"; no automation clause, so unclear | every 2 weeks |
| `rbnz_lvr`, `rbnz_dti` rbnz.govt.nz (prohibited) | kb_rbnz_lvr, kb_rbnz_dti | Terms forbid robots and scrapers | every 2 weeks; also after each RBNZ Financial Policy Committee announcement |
| `src_akl_rating_valuation` Auckland Council (unclear) | kb_cv_not_market | Council terms unclear on automated access | every 2 weeks |
| `bank_rates_snapshot` banks (unclear) | not a KB item | See `jobs/rates-refresh/config/banks.yaml` | per that file |

## Items checked automatically, with a manual fallback

| Source (`tos_status`) | Items | Fallback |
|---|---|---|
| `src_nhc_about` naturalhazards.govt.nz (allowed, CC BY-NC 4.0) | kb_nhc_building_cap, kb_nhc_levy, kb_nhc_building_excess | The site sits behind a bot challenge (Incapsula). If a run records `blocked` or `quote_found: null`, check by hand. Cadence: monthly, and whenever there is news about the NHC levy or building cap (Treasury review of the levy and cap was reported in 2025; not verified). |
| `src_unittitles_guide` unittitles.govt.nz (allowed) | kb_unit_title_body_corporate_insurance | If blocked or the PDF URL changed, check by hand every 2 weeks until fixed. |
| `kainga_ora_fhl` kaingaora.govt.nz (allowed, tentative) | kb_fhl_premium | Terms were read from a search summary only; re-read the copyright statement in a browser. If blocked, check by hand every 2 weeks. |

## Reading the job output

- `quote_found: true` / `false`: the page was fetched and the quote was / was not found. A `verified` item
  whose quote is gone becomes `needs_review` automatically.
- `quote_found: null` with `blocked: true`: robots.txt disallowed, HTTP 401/403/406/429/503, or a challenge
  page. Nothing was worked around and the status is unchanged. Check by hand.
- `manual_check_required: true`: the source is not `allowed`; no request was sent.
- `job_runs/{timestamp}` (or `out/kb_refresh_report.json` locally) counts `checked`, `manual_check_required`,
  `changed` and `errors`.

## Changing a source's `tos_status`

Only after re-reading the site's terms and robots.txt. Update `tos_status`, `tos_url`,
`tos_checked_at` and `attribution` in `kb/seed/sources.json`, run `python kb/validate.py`, then re-seed
with `scripts/seed_firestore.py`.
