# Manual check list for knowledge-base sources

The weekly `kb-refresh` job only fetches sources whose `tos_status` in `kb/seed/sources.json`
is `allowed`. For every other source the job makes **no request** and records
`manual_check_required` in the item's `last_check`. A person must open the page and confirm the
quote. Terms were reviewed on 2026-10-03 (not legal advice; some terms were read from search
summaries only, see `tos_url` per source).

## How to check one item

Verification is bound to the exact text that was reviewed. An item is only `verified` if
`verified_by`, `verified_at` and `verified_content_hash` are all set and `verified_content_hash`
equals the item's current content hash (over `claim_en`, `quote`, `source_url`, `applies_when`,
`next_step_en`). If any of those fields is edited later, the item is treated as `needs_review`
everywhere (validator, seeding, kb-refresh) until it is reviewed again.

**The reviewer must be a second person**, not the author of the claim and quote. Do not set
`verified_by` to yourself for text you wrote.

1. Open `source_url` in a normal browser.
2. Find the `quote` word for word (case, quotes and dashes may differ) and read the surrounding text.
3. Check that `claim_en` says no more than the quote and the page support (no added numbers, dates,
   conditions or advice).
4. If correct, in the seed file set `status` to `verified`, `verified_by` to your name and `verified_at`
   to the current time as a full ISO timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`; a date-only value counts as
   the start of that day), then compute the hash and paste it into `verified_content_hash`:

   ```
   python kb/validate.py --hash <item id>
   ```

   (Set status, name and date first; the hash does not depend on them, only on the five content fields.)
5. Run `python kb/validate.py` (must print `errors=0`), commit, then re-seed with
   `python scripts/seed_firestore.py` (use `--dry-run` first).
6. If the wording changed or the page moved: leave `status` as `needs_review` or `draft`, correct `quote`
   and `claim_en` from the new original text, and let a second person verify as above.
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

## Items that need manual checks

| Source (`tos_status`) | Items | Why manual | Cadence |
|---|---|---|---|
| `src_settled_*` settled.govt.nz (unclear) | kb_auction_unconditional, kb_leaky_build_era, kb_monolithic_cladding, kb_flood_insurance_limit, kb_insurance_condition, kb_title_freehold, kb_title_cross_lease, kb_title_unit_title | Real Estate Authority copyright: "personal or in-house use only"; no automation clause, so unclear | every 2 weeks |
| `rbnz_lvr`, `rbnz_dti` rbnz.govt.nz (prohibited) | kb_rbnz_lvr, kb_rbnz_dti, kb_fhl_lvr_not_restricted, kb_fhl_dti_not_restricted | Terms forbid robots and scrapers | every 2 weeks; also after each RBNZ Financial Policy Committee announcement |
| `src_akl_rating_valuation` Auckland Council (unclear) | kb_cv_not_market | Council terms unclear on automated access | every 2 weeks |
| `bank_rates_snapshot` banks (unclear) | not a KB item | See `jobs/rates-refresh/config/banks.yaml` | per that file |

## Items checked automatically, with a manual fallback

| Source (`tos_status`) | Items | Fallback |
|---|---|---|
| `src_nhc_about` naturalhazards.govt.nz (allowed, CC BY-NC 4.0) | kb_nhc_building_cap, kb_nhc_levy, kb_nhc_building_excess | The site sits behind a bot challenge (Incapsula). If a run records `blocked` or `quote_found: null`, check by hand. Cadence: monthly, and whenever there is news about the NHC levy or building cap (Treasury review of the levy and cap was reported in 2025; not verified). |
| `src_unittitles_guide` unittitles.govt.nz (allowed) | kb_unit_title_body_corporate_insurance | If blocked or the PDF URL changed, check by hand every 2 weeks until fixed. |
| `kainga_ora_fhl`, `kainga_ora_fhl_brochure` kaingaora.govt.nz (allowed, tentative) | kb_fhl_premium, kb_fhl_govt_not_funding_premium, kb_fhl_deposit_5pct, kb_fhl_income_cap_individual, kb_fhl_income_cap_multiple, kb_fhl_income_basis | Terms were read from a search summary only; re-read the copyright statement in a browser. If blocked, check by hand every 2 weeks. |

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
- `job_runs/{timestamp}` (or `out/kb_refresh_report.json` locally) counts `checked`, `manual_check_required`,
  `changed` and `errors`.

## Changing a source's `tos_status`

Only after re-reading the site's terms and robots.txt. Update `tos_status`, `tos_url`,
`tos_checked_at` and `attribution` in `kb/seed/sources.json`, run `python kb/validate.py`, then re-seed
with `scripts/seed_firestore.py`.
