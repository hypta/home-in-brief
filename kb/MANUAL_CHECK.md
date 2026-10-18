# Manual check list for knowledge-base sources

The `kb-refresh` job (currently run manually) only fetches sources whose `tos_status` in `kb/seed/sources.json`
is `allowed`. For every other source the job makes **no request** and records
`manual_check_required` in the item's `last_check`. A person must open the page and confirm the
quote. Terms were reviewed on 2026-10-03 (not legal advice; some terms were read from search
summaries only, see `tos_url` per source).

## Quick way: mark reviewed items with one command

After checking items against their sources, run (reviewer name = the person who checked; this may be the author of the item):

```
.venv/bin/python kb/mark_verified.py --by "Reviewer Name" kb_auction_unconditional kb_nhc_building_cap
```

It sets `status`, `verified_by`, `verified_at` (current UTC time) and `verified_content_hash` for the listed
ids only, then runs `kb/validate.py`. Use `--dry-run` to preview. Commit the seed changes afterwards; the local
rules-api reads the seed files on every request, and the deployed one is re-seeded by the deploy workflow when
`kb/seed/` changes on `main` (step 5 below).

## How to check one item

Verification is bound to the exact text that was reviewed. An item is only `verified` if
`verified_by`, `verified_at` and `verified_content_hash` are all set and `verified_content_hash`
equals the item's current content hash (over `claim_en`, `quote`, `source_url`, `applies_when`,
`next_step_en`). If any of those fields is edited later, the item is treated as `needs_review`
everywhere (validator, seeding, kb-refresh) until it is reviewed again.

**The author may verify their own item**, but must open the source and check the quote word for word
(step 2 below). Set `verified_by` to the name of the person who actually did the check.

1. Open `source_url` in a normal browser.
2. Find the `quote` word for word (case, quotes and dashes may differ) and read the surrounding text.
3. Check that `claim_en` says no more than the quote and the page support (no added numbers, dates,
   conditions or advice).
4. If correct, in the seed file set `status` to `verified`, `verified_by` to your name and `verified_at`
   to the current time as a full ISO timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`; a date-only value counts as
   the start of that day), then compute the hash and paste it into `verified_content_hash`:

   ```
   .venv/bin/python kb/validate.py --hash <item id>
   ```

   (Set status, name and date first; the hash does not depend on them, only on the five content fields.)
5. Run `.venv/bin/python kb/validate.py` (must print `errors=0`), then commit and push to `main`: the deploy
   workflow re-seeds Firestore when `kb/seed/` changed. To seed by hand (use `--dry-run` first), see `DEPLOY.md`,
   section 3b.
6. If the wording changed or the page moved: leave `status` as `needs_review` or `draft`, correct `quote`
   and `claim_en` from the new original text, and verify again as above.
7. Keep the attribution from the source's `attribution` field wherever the quote is shown.

What re-seeding does with existing Firestore documents: the git seed wins for content. A verified
document stays verified only while its content hash still matches; a changed text becomes `needs_review`.
`last_check` is kept. Withdrawals are never undone by re-seeding:

- If kb-refresh moved an item to `needs_review` (it records `demoted_at` and `demoted_content_hash`), the
  item stays `needs_review` as long as the content is the same, even if the seed says `verified`. It is
  cleared only by a new review: the seed's `verified_at` must be strictly later than `demoted_at` and the
  hash must match. Re-verify after looking at the page again, with a fresh timestamp.
- A kb-refresh check that started before a newer review of the same content cannot demote that review.
- Ids removed from the seed become `retired` (with `retired_at`) and are no longer served. A retired id
  that reappears in the seed stays retired unless the seed item sets `"reinstate": true`, or is verified
  with a `verified_at` later than `retired_at`. A seed item with `"status": "retired"` always retires.

`published_or_updated` in `sources.json` is the date marked on the source page (publication date for
settled.govt.nz, "last updated" for RBNZ and Auckland Council, the document date for PDFs), as
`YYYY`, `YYYY-MM` or `YYYY-MM-DD`, or null if the page shows none. Re-check it when you re-verify.

## Verification status (as of 2026-10-13)

The knowledge base has 35 items: 35/35 `verified`, none `draft`.

Verified by Kevin, 2026-10-04 (8 items, settled.govt.nz):

| Item | Verified at (UTC) |
|---|---|
| kb_auction_unconditional | 2026-10-04 08:54:03Z |
| kb_leaky_build_era | 2026-10-04 08:54:34Z |
| kb_monolithic_cladding | 2026-10-04 08:55:02Z |
| kb_flood_insurance_limit | 2026-10-04 08:55:29Z |
| kb_insurance_condition | 2026-10-04 08:56:21Z |
| kb_title_freehold | 2026-10-04 08:56:34Z |
| kb_title_cross_lease | 2026-10-04 08:56:43Z |
| kb_title_unit_title | 2026-10-04 08:56:49Z |

Verified by Ting Wang, 2026-10-04 (17 items):

| Item | Verified at (UTC) |
|---|---|
| kb_nhc_building_cap | 2026-10-04 09:30:47Z |
| kb_nhc_levy | 2026-10-04 09:30:47Z |
| kb_nhc_building_excess | 2026-10-04 09:30:47Z |
| kb_fhl_premium | 2026-10-04 09:35:40Z |
| kb_fhl_govt_not_funding_premium | 2026-10-04 09:35:40Z |
| kb_fhl_deposit_5pct | 2026-10-04 09:35:40Z |
| kb_fhl_income_cap_individual | 2026-10-04 09:35:40Z |
| kb_fhl_income_cap_multiple | 2026-10-04 09:35:40Z |
| kb_fhl_income_basis | 2026-10-04 09:35:40Z |
| kb_rbnz_lvr | 2026-10-04 09:45:35Z |
| kb_rbnz_dti | 2026-10-04 09:51:18Z |
| kb_fhl_lvr_not_restricted | 2026-10-04 09:51:18Z |
| kb_fhl_dti_not_restricted | 2026-10-04 09:51:18Z |
| kb_unit_title_levies | 2026-10-04 10:02:39Z |
| kb_unit_title_body_corporate_insurance | 2026-10-04 10:02:39Z |
| kb_cv_not_market | 2026-10-04 10:02:39Z |
| kb_coastal_inundation_meaning | 2026-10-04 10:02:39Z |

Verified by Ting Wang, 2026-10-13 (10 items):

| Item | Verified at (UTC) |
|---|---|
| kb_dti_definition | 2026-10-13 06:48:30Z |
| kb_landslide_map_context | 2026-10-13 06:48:30Z |
| kb_lim_definition | 2026-10-13 06:48:30Z |
| kb_lim_may_include | 2026-10-13 06:48:30Z |
| kb_lim_next_step | 2026-10-13 06:48:30Z |
| kb_low_deposit_higher_rates | 2026-10-13 06:48:30Z |
| kb_lvr_definition | 2026-10-13 06:48:30Z |
| kb_nhc_land_cover_limits | 2026-10-13 06:48:30Z |
| kb_overland_flow_path | 2026-10-13 06:48:30Z |
| kb_title_cross_lease_flats_plan | 2026-10-13 06:48:30Z |

Six of these were verified before and were verified again after their claims were shortened to 25 words or fewer
(`kb_lim_next_step`, `kb_overland_flow_path`, `kb_landslide_map_context`, `kb_nhc_land_cover_limits`,
`kb_low_deposit_higher_rates`, `kb_title_cross_lease_flats_plan`). Four are new: `kb_lim_definition`,
`kb_lim_may_include`, `kb_lvr_definition` and `kb_dti_definition`.

While checking against the sources on 2026-10-04, Ting Wang narrowed the wording of `kb_rbnz_lvr` to what the source
says (added "for owner-occupiers").

The production rules-api returns `verified` items, so the page can cite all 35.

If you later edit the `claim_en`, `quote`, `source_url`, `applies_when` or `next_step_en` of any item, it goes back to `needs_review` automatically and must be reviewed again.

## Items that need manual checks

| Source (`tos_status`) | Items | Why manual | Cadence |
|---|---|---|---|
| `src_settled_*` settled.govt.nz (unclear) | kb_auction_unconditional, kb_leaky_build_era, kb_monolithic_cladding, kb_flood_insurance_limit, kb_insurance_condition, kb_title_freehold, kb_title_cross_lease, kb_title_cross_lease_flats_plan, kb_title_unit_title, kb_lim_definition | Real Estate Authority copyright: "personal or in-house use only"; no automation clause, so unclear. 8 verified 2026-10-04, 2 on 2026-10-13. | re-check every 2 weeks (verified 2026-10-04) |
| `rbnz_lvr`, `rbnz_dti` rbnz.govt.nz (prohibited) | kb_rbnz_lvr, kb_rbnz_dti, kb_fhl_lvr_not_restricted, kb_fhl_dti_not_restricted, kb_low_deposit_higher_rates, kb_lvr_definition, kb_dti_definition | Terms forbid robots and scrapers | every 2 weeks; also after each RBNZ Financial Policy Committee announcement |
| `src_akl_rating_valuation`, `src_akl_overland_flow`, `src_akl_home_search_tips`, `src_akl_landslide_maps`, `src_akl_lim` Auckland Council (unclear) | kb_cv_not_market, kb_overland_flow_path, kb_coastal_inundation_meaning, kb_landslide_map_context, kb_lim_may_include | Council terms unclear on automated access | every 2 weeks |
| `bank_rates_snapshot` banks (unclear) | not a KB item | See `jobs/rates-refresh/config/banks.yaml` | per that file |

## Items checked automatically, with a manual fallback

| Source (`tos_status`) | Items | Fallback |
|---|---|---|
| `src_nhc_about` naturalhazards.govt.nz (allowed, CC BY-NC 4.0) | kb_nhc_building_cap, kb_nhc_levy, kb_nhc_building_excess, kb_nhc_land_cover_limits | The site sits behind a bot challenge (Incapsula). If a run records `blocked` or `quote_found: null`, check by hand. Cadence: monthly, and whenever there is news about the NHC levy or building cap (Treasury review of the levy and cap was reported in 2025; not verified). |
| `src_unittitles_guide` unittitles.govt.nz (allowed) | kb_unit_title_body_corporate_insurance, kb_unit_title_levies | If blocked or the PDF URL changed, check by hand every 2 weeks until fixed. |
| `src_nhc_home_buyers` naturalhazards.govt.nz (allowed, CC BY-NC 4.0) | kb_lim_next_step | Same Incapsula bot challenge as src_nhc_about; if a run records `blocked` or `quote_found: null`, check by hand monthly. |
| `kainga_ora_fhl`, `kainga_ora_fhl_brochure` kaingaora.govt.nz (allowed) | kb_fhl_premium, kb_fhl_govt_not_funding_premium, kb_fhl_deposit_5pct, kb_fhl_income_cap_individual, kb_fhl_income_cap_multiple, kb_fhl_income_basis | If blocked, check by hand every 2 weeks. |

## Reading the job output

- `quote_found: true` / `false`: the page was fetched and the quote was / was not found. A `verified` item
  whose quote is gone becomes `needs_review` automatically.
- `quote_found: null` with `blocked: true`: robots.txt disallowed, HTTP 401/403/406/429/503, or a challenge
  page. Nothing was worked around and the status is unchanged. Check by hand.
- `manual_check_required: true`: the source is not `allowed` (or the item's `source_url` differs from the
  approved source URL); no request was sent.
- A `verified` item whose verification is unsound (content edited after review, or reviewer data missing) is
  moved to `needs_review` by the job. A result for a content version that was edited while the job ran is
  stored with `stale_content: true` and does not touch the item.
- Redirects are followed only to the same host over https; only https URLs are fetched; robots.txt problems
  (429, 401, 403, 5xx, timeout, an HTML page instead of robots.txt) stop the fetch.
- `job_runs/{run_id}` (run id `YYYYMMDDTHHMMSSZ-xxxx`; `out/kb_refresh_report.json` locally) has the counts
  `checked`, `manual_check_required` and `changed`, and the list `errors`.

## Changing a source's `tos_status`

Only after re-reading the site's terms and robots.txt. Update `tos_status`, `tos_url`,
`tos_checked_at` and `attribution` in `kb/seed/sources.json`, run `.venv/bin/python kb/validate.py`, then commit
and push to `main` (the deploy workflow re-seeds; by hand: `DEPLOY.md`, section 3b).
