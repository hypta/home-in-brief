// Draws the report blocks from PropertyContext, RulesOutput and knowledge-base items.
// Wording of flags and warnings follows CODES.md: "could not check" is never shown as "no risk".
export const $ = id => document.getElementById(id);
export const byId = items => Object.fromEntries(items.map(i => [i.id, i]));
export const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// noreferrer: DuckDuckGo only jumps to the first result when the click does not name the page it came from
export const link = (url, text) => `<a href="${/^https?:\/\//.test(url || "") ? esc(url) : "#"}" target="_blank" rel="noreferrer">${esc(text)}</a>`;
const LAYERS = {flood_plain:"Flood plains", flood_prone_area:"Flood prone areas", overland_flow_path:"Overland flow paths",
  coastal_inundation:"Coastal inundation (1% AEP)", landslide_shallow:"Landslide susceptibility - shallow",
  landslide_large_scale:"Landslide susceptibility - large scale", liquefaction:"Liquefaction vulnerability",
  tsunami_evacuation_zone:"Tsunami evacuation zone", coastal_erosion:"Coastal erosion (ASCIE 2130, RCP8.5+)"};
export const layerName = id => LAYERS[id] || id;

const STATUS = {intersects:"mapped here", none:"not mapped here", unavailable:"could not check", not_found:"not found", found:"found"};
const BASIS = {standard:"standard rate", special:"special rate", "special+lem":"special rate plus low-equity margin", unknown:"not published"};
const TOPIC_BLOCK = {hazard:"hazards", insurance:"hazards", lending:"loan", valuation:"loan"};  // any other topic: "notes"
const NO_RISK_CAVEAT = "This is not a statement that there is no risk.";

const money = v => "$" + Math.round(Number(v)).toLocaleString("en-NZ");
const pct = v => Number(v).toLocaleString("en-NZ", {maximumFractionDigits: 2}) + "%";
const m2 = v => Number(v).toLocaleString("en-NZ") + " m²";
const words = code => String(code || "no reason given").replace(/_/g, " ");
const missing = f => `${f.status === "unavailable" ? "Could not check" : "Not found"} (${esc(words(f.reason || f.status))}).`;
const span = (r, fmt) => !r ? "not available" : r[0] === r[1] ? fmt(r[0]) : `${fmt(r[0])} to ${fmt(r[1])}`;
const or = (v, fmt) => v == null ? "not published" : fmt(v);
const list = items => items.length ? `<ul class="plain">${items.map(i => `<li>${i}</li>`).join("")}</ul>` : "";
const row = (name, detail, tag) => `<div class="row"><b>${name}</b>${tag ? `<span class="tag ${tag}">${esc(STATUS[tag] || tag)}</span>` : ""}<div class="detail">${detail}</div></div>`;
const ratesText = r => r.status === "found" ? `${money(r.value)} for ${esc(r.rating_year)}` : null;

let sources, banks, cited;  // sources and bank names by id; the sources cited in this render, in order of first use
const bank = id => banks[id] || id;

// A numbered reference that opens in place: source name, extra lines (data date, quote), credit line, licence.
function cite(refs, lines = []) {
  return [].concat(refs ?? []).map(ref => {
    const s = typeof ref === "string" ? sources[ref] : ref;
    if (!s) return `<span class="muted">[source unavailable]</span>`;
    const n = cited.findIndex(c => c.id === s.id) + 1 || cited.push(s);
    const more = [...lines, s.attribution, s.licence && "Licence: " + s.licence, s.accessed_at && "Accessed " + s.accessed_at];
    return `<details class="cite"><summary aria-label="Source ${n}: ${esc(s.title)}">[${n}]</summary><div>${link(s.url, s.title)}<br>${more.filter(Boolean).map(esc).join("<br>")}</div></details>`;
  }).join(" ");
}

function kbCite(k) {
  const s = sources[k.source_id] || {id: k.source_id || k.id, title: k.source_title, url: k.source_url, licence: k.licence};
  return cite(s, [`Quoted: "${k.quote}"`, s.published_or_updated && "Published or updated " + s.published_or_updated]);
}

function notes({rules, kb}, block, heading) {
  const mine = (kb || []).filter(k => (TOPIC_BLOCK[k.topic] || "notes") === block), told = new Set(rules?.kb_items_from_user_input);
  if (!mine.length) return "";
  return `<h3>${heading}</h3>` + mine.map(k => `<div class="note">${esc(k.claim_en)} ${kbCite(k)}
      ${k.source_type === "secondary" ? `<span class="tag none">secondary source</span>` : ""}
      ${k.status === "verified" ? "" : `<span class="tag draft">draft, not yet checked</span>`}
      ${told.has(k.id) ? `<div class="muted">Based on what you told us.</div>` : ""}
      ${k.next_step_en ? `<div><b>Next step:</b> ${esc(k.next_step_en)}</div>` : ""}</div>`).join("");
}

// code: [block, text]. A code may carry a layer or bank id after a colon. CODES.md marks the hidden ones as not user-facing.
const HIDDEN = /^(kb_hash_mismatch|kb_verification_incomplete|hazard_status_missing|hazard_status_unknown|hazard_priority_unknown)$/;
const WARNINGS = {
  hazards_unavailable: ["hazards", "No flood layer could be checked. " + NO_RISK_CAVEAT],
  hazards_partial: ["hazards", layer => `${layerName(layer)} could not be checked. ${NO_RISK_CAVEAT}`],
  price_missing: ["loan", "There is no price to calculate with. Enter a price above."],
  deposit_missing: ["loan", "Enter a deposit to see the loan amount and repayments."],
  income_missing: ["loan", "Enter the household income to see the debt-to-income ratio and First Home Loan figures."],
  cash_purchase_no_loan: ["loan", "The deposit equals the price, so no loan is needed and no repayments are shown."],
  deposit_exceeds_price: ["loan", "The deposit covers the whole price, so no loan is needed and no repayments are shown. Check the price and the deposit above."],
  existing_debts_assumed_zero: ["loan", "The debt-to-income ratio assumes you have no other debts. Enter your existing debts above, or 0 if you have none."],
  fhl_skipped_income_missing: ["loan", "First Home Loan was not checked because no income was entered."],
  fhl_no_lenders: ["loan", "First Home Loan lender information is unavailable."],
  cv_unavailable: ["loan", "The council valuation could not be retrieved."],
  cv_invalid: ["loan", "The council valuation could not be used."],
  rates_unavailable: ["loan", "Bank rates could not be read, so bank and First Home Loan repayments are missing. Enter your own interest rate to see a repayment."],
  rates_snapshot_stale: ["loan", (_, r) => `The oldest bank rate used here is more than ${r.rates_stale_after_days} days old. Check current rates with the bank.`],
  bank_skipped: ["loan", id => `${bank(id)} is left out: it has no published rate that applies to this deposit. This does not mean it has no rate.`],
  lem_base_unverified: ["loan", id => `${bank(id)}'s rate here is approximate: the rate its low-equity margin is added to is not confirmed.`],
  title_type_unavailable: ["notes", "The title type could not be confirmed."],
  title_type_unresolved: ["notes", "LINZ lists more than one kind of title here, so no title notes are shown. The title type needs confirming on the record of title."],
  year_built_unavailable: ["notes", () => $("year-built").value.trim() ? null : "The year built could not be found. Check the listing for it, then enter it in the form above."],
  year_built_unparseable: ["notes", "The year built could not be read. Check the listing for it."],
  cladding_unavailable: ["notes", () => $("cladding").value ? null : "Cladding is not available from any free public source. Check the listing for it, then choose it in the form above."],
  chat_year_built_unparseable: ["notes", "The year built you entered could not be used. Enter a four-digit year or a decade such as 1990s."],
  kb_unavailable: ["notes", "Background notes are unavailable right now."],
};

function warnings(rules, block) {
  return list((rules?.warnings || []).map(w => {
    const [code, arg] = w.split(":"), known = WARNINGS[code];
    if (HIDDEN.test(code)) return null;
    if (!known) return block === "loan" ? esc("Note: " + words(w)) : null;
    return known[0] === block ? esc(typeof known[1] === "function" ? known[1](arg, rules) : known[1]) : null;
  }).filter(Boolean));
}

function hazardDetail(h, where) {
  if (h.status === "unavailable" || h.status === "not_found") return `${missing(h)} ${NO_RISK_CAVEAT}`;
  if (h.status === "none") return `The council does not show this layer ${where}. ${NO_RISK_CAVEAT}`;
  const bits = [];
  if (h.susceptibility) bits.push(`Highest class ${where}: ${esc(h.susceptibility)}`);
  else if (h.classes.length) bits.push(`Class ${where}: ${h.classes.map(esc).join(", ")}`);
  if (h.overlap_pct != null) bits.push(`Covers ${h.overlap_pct}% of the parcel`);
  if (h.crosses_parcel) bits.push("Crosses the parcel");
  const near = /^point_method_line_within_(\d+)m$/.exec(h.reason || "");
  if (near) return `Within ${near[1]} m of the address point.`;
  return (bits.join(". ") || `Mapped ${where}`) + ".";
}

function hazards(state) {
  const {ctx} = state, byParcel = ctx.parcel.spatial_method === "parcel";
  const rows = priority => ctx.hazards.filter(h => h.priority === priority).map(h => row(esc(layerName(h.layer)),
    `${hazardDetail(h, byParcel ? "on this parcel" : "at the address point")} ${cite(h.source_id, [h.data_date && "Data date " + h.data_date])}`, h.status)).join("");
  return `${byParcel || ctx.address.status !== "found" ? "" : `<p class="problem">No parcel was matched, so the layers were checked at the address point only and parcel coverage is not calculated.</p>`}
    <h3>Flood layers</h3>${rows("primary") || `<p class="problem">No flood layer was checked. ${NO_RISK_CAVEAT}</p>`}
    <h3>Other mapped hazards</h3>${rows("secondary") || `<p class="problem">No other layer was checked. ${NO_RISK_CAVEAT}</p>`}
    ${warnings(state.rules, "hazards")}${notes(state, "hazards", "Notes on these hazards and insurance")}`;
}

function facts({ctx}) {
  const p = ctx.property, council = ctx.links.find(l => l.id === "council_valuation"), rows = [];
  const add = (name, f, text) => rows.push(row(name, (text || missing(f)
    + (council && (f.source_id || "").startsWith("ac_") ? ` ${link(council.url, "Check the council property search")}.` : ""))
    + " " + cite(f.source_id), text ? "found" : f.status));
  const fact = (name, f, fmt = esc) => add(name, f, f.status === "found" ? fmt(f.value) : null);
  add("Capital value (CV)", p.cv, p.cv.status === "found" ? `${money(p.cv.value)} as at ${esc(p.cv.valuation_date)}. A council rating valuation, not a market value.` : null);
  add("Annual rates", p.annual_rates, ratesText(p.annual_rates));
  add("Title type", p.title_type, p.title_type.value ? esc(p.title_type.value) : (p.title_type.linz_types || []).length ? `LINZ records: ${p.title_type.linz_types.map(esc).join(", ")} (${esc(words(p.title_type.reason))})` : null);
  const r = p.year_built.decade_range;
  add("Decade built", p.year_built, r ? (r[0] ? `${r[0]}-${r[1]}` : `Before ${r[1] + 1}`) + " (from the council property category)" : null);
  fact("Land value", p.land_value, money); fact("Value of improvements", p.improvements_value, money);
  fact("Rateability", p.rateability); fact("Improvements", p.improvement_description);
  fact("Land use", p.land_use); fact("Land area", p.land_area_m2, m2); fact("Total floor area", p.floor_area_m2, m2);
  fact("Building site coverage", p.building_site_coverage_m2, m2); fact("Legal description", p.legal_description);
  fact("Record of title", p.record_of_title); fact("Valuation number", p.valuation_number); fact("Local board", p.local_board);
  fact("Cladding", p.cladding); add("Last sale", p.last_sale);
  return rows.join("");
}

const FLAGS = {
  lvr_over_80: () => "The deposit is under 20% of the price (LVR above 80%). A low-equity margin or a First Home Loan may apply.",
  dti_over_6: r => `Total debt would be more than 6 times income, above the Reserve Bank's high-DTI threshold, and lenders restrict this kind of lending. For this income the threshold is a loan of ${money(r.high_dti_threshold)}.`,
  fhl_deposit_below_5: () => "The First Home Loan scenario is not shown because the deposit is under 5% of the price.",
  fhl_income_over_cap: r => `The First Home Loan scenario is not shown because the income is above its cap: ${money(r.fhl_income_caps.single)} for one applicant, ${money(r.fhl_income_caps.multiple_or_dependants)} for two or more applicants or one with dependants.`,
  fhl_household_type_unknown: () => "First Home Loan eligibility depends on the household. Choose it in the form above.",
};

const stressRow = (r, detail) => row(`If rates were ${r.stress_add_pp} percentage points higher`, detail + " An illustration, not any bank's test rate.");
const table = (head, rows) => `<div class="scroll"><table><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr>${rows.map(cells => `<tr>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table></div>`;
const SCENARIOS = {
  bank_standard: (s, r) => `<h3>Bank loan at published 1-year rates</h3>
    <div class="note">Rates of ${span(s.rate_range_pct, pct)} across ${s.per_bank.length} banks${s.low_equity_margin_pct ? `, including a low-equity margin of ${span(s.low_equity_margin_pct, String)} percentage points where a bank charges one` : ""}. ${cite(s.source_ids)}</div>
    ${row("Monthly repayment", span(s.monthly_range, money))}${row("Fortnightly repayment", span(s.fortnightly_range, money))}
    ${stressRow(r, `${money(s.stress_monthly)} per month at the highest of these rates (${money(s.stress_delta_monthly)} more).`)}
    <details><summary>Rate used for each bank</summary>${table(["Bank", "Rate", "Basis", "Collected"],
      s.per_bank.map(b => [esc(bank(b.bank_id)), pct(b.rate_pct), esc(BASIS[b.basis] || b.basis), esc((b.captured_at || "not recorded").slice(0, 10))]))}</details>`,
  first_home_loan: (s, r) => `<h3>First Home Loan</h3>
    ${s.eligibility === "conditional" ? `<div class="problem">${esc(s.eligibility_note)}</div>` : ""}
    <div class="note">${esc(s.lmi_note)} ${cite(s.source_ids)}</div>
    ${row("Monthly repayment", span(s.monthly_range, money))}${row("Fortnightly repayment", span(s.fortnightly_range, money))}
    ${stressRow(r, `${span(s.stress_monthly_range, money)} per month.`)}
    ${row("Insurance premium at the full rate", money(s.lmi_amount) + (s.full_premium_assumed_for.length ? `. The full premium is assumed for ${s.full_premium_assumed_for.map(id => esc(bank(id))).join(", ")}.` : ""))}
    <details><summary>Figures for each lender</summary>${table(["Lender", "Rate", "Basis", "Premium passed on", "Monthly", "Fortnightly", `Monthly at +${r.stress_add_pp} points`],
      s.lenders.map(l => [esc(l.name), or(l.rate_pct, pct), esc(BASIS[l.rate_basis] || l.rate_basis), or(l.lmi_passthrough_pct, pct), or(l.monthly, money), or(l.fortnightly, money), or(l.stress_monthly, money)]))}</details>`,
  user_rate: (s, r) => `<h3>At the interest rate in the form (${pct(s.rate_pct)})</h3>
    ${row("Monthly repayment", money(s.monthly))}${row("Fortnightly repayment", money(s.fortnightly))}
    ${stressRow(r, `${money(s.stress_monthly)} per month (${money(s.stress_delta_monthly)} more).`)}`,
};

function loan(state) {
  const {ctx, rules: r, rulesError: err} = state;
  if (err) return `<div class="problem">The repayment figures could not be calculated: ${esc(err.message)}${list(err.fields.map(f => `${esc(words(f.field.replace("user_inputs.", "")))}: ${esc(f.message)}`))}</div>`;
  if (!r) return `<p class="muted">Calculating…</p>`;
  const price = r.price_used, rates = ctx.property.annual_rates, out = [];
  out.push(row("Price used", price.basis === "cv" ? `${money(price.value)}, the council valuation (CV) as at ${esc(price.valuation_date)}. Not a market value.`
    : price.basis === "user" ? `${money(price.value)}, your own figure.` : "No price to calculate with."));
  if (r.loan_amount) out.push(row("Loan amount", `${money(r.loan_amount)} (the price less a deposit of ${money(r.deposit)})`));
  if (r.loan_amount && r.lvr_pct != null) out.push(row("Loan-to-value ratio (LVR)", `${pct(r.lvr_pct)} of the price is borrowed`));
  if (r.loan_amount && r.dti != null) out.push(row("Debt-to-income ratio (DTI)", `${r.dti} times income (existing debts plus this loan, divided by income)`));
  out.push(...r.flags.map(f => `<div class="note">${esc(FLAGS[f.id] ? FLAGS[f.id](r) : "Note: " + words(f.id))} ${cite(f.source_ids)}</div>`));
  out.push(...r.scenarios.map(s => SCENARIOS[s.id] ? SCENARIOS[s.id](s, r) : `<h3>${esc(words(s.id))}</h3>`));
  if (r.scenarios.length) out.push(`<div class="muted">Repayments are principal and interest over ${r.loan_term_years} years. They do not include council rates, insurance, utilities or maintenance.${r.rates_snapshot_date ? ` Bank rates were collected by hand from each bank's website; the oldest is from ${esc(r.rates_snapshot_date)}. ${cite("bank_rates_snapshot")}` : ""}</div>`);
  out.push(row("Council rates (not included above)", ratesText(rates) ? `${ratesText(rates)} ${cite(rates.source_id)}` : missing(rates)));
  return out.join("") + warnings(r, "loan") + notes(state, "loan", "Lending notes");
}

function buildNotes(state) {
  const {rules: r} = state;
  if (!r) return state.rulesError ? `<p class="problem">The notes are unavailable until the repayment figures above can be calculated.</p>` : `<p class="muted">Loading…</p>`;
  const EMPTY = {no_verified_items: "No checked background notes are available yet.", no_matching_items: "No background notes apply to this property."};
  return (r.kb_includes_draft ? `<p class="problem">Development mode: draft notes are shown. They have not yet been checked against their sources.</p>` : "")
    + (state.kb ? "" : `<p class="problem">The background notes could not be loaded.</p>`)
    + warnings(r, "notes")
    + (notes(state, "notes", "Notes for this property") || (!state.kb || r.kb_empty_reason === "kb_unavailable" ? ""
       : `<p class="muted">${EMPTY[r.kb_empty_reason] || "No build-era or title notes apply to this property."}</p>`));
}

// report-api's answer, shown as plain text. It only returns sentences that passed its check against the sources they
// cite; a source id is a PropertyContext source, a rules-api source or a knowledge-base item.
function summary({rules, rulesError, kb, report: r, reportError: err}) {
  if (!rules && rulesError) return `<p class="problem">The summary is unavailable until the repayment figures above can be calculated.</p>`;
  if (err) return err.code === "validation_failed"
    ? `<p class="problem">No summary sentence passed the check against its sources, so none is shown. The rest of the report is not affected.</p>`
    : `<p class="problem">The summary is unavailable right now. The rest of the report is not affected.</p><button type="button" id="retry-summary">Try again</button>`;
  if (!r) return `<p class="muted">Writing the summary…</p>`;
  const items = byId(kb || []);
  const line = s => {
    const quoted = new Set(s.source_ids.map(id => items[id]?.source_id));  // a source cited through one of its items needs no second reference
    return `${esc(s.text)} ${s.source_ids.filter(id => !quoted.has(id)).map(id => items[id] ? kbCite(items[id]) : cite(id)).join(" ")}`;
  };
  return list(r.summary.map(line))
    + (r.dropped ? `<p class="muted">${r.dropped === 1 ? "1 sentence was" : r.dropped + " sentences were"} left out because the check against the sources did not pass.</p>` : "")
    + (r.next_steps.length ? `<h3>Next steps</h3>${list(r.next_steps.map(line))}` : "")
    + (r.not_found_notes.length ? `<h3>What could not be found</h3>${list(r.not_found_notes.map(n => esc(n.text)))}` : "")
    + `<p class="muted">Summary written by ${esc(r.model)}. A sentence is shown only when it passes a check against the sources it cites.</p>`;
}

const LINK_KIND = {direct: " (opens this property)", first_result: " (opens the first search result for this address on that site)", home: " (search for the address there)"};

// a block that cannot be drawn must not take the others with it
function draw(id, block, state) {
  try { $(id).innerHTML = block(state); }
  catch (e) { console.error(e); $(id).innerHTML = `<p class="problem">This section could not be shown.</p>`; }
}

// The summary arrives after the rest and is drawn on its own: redrawing the other blocks would close the sources a reader has opened.
export function renderSummary(state) {
  draw("summary", summary, state);
  $("sources").innerHTML = cited.map(s => `<li>${link(s.url, s.title)}${s.licence ? " · " + esc(s.licence) : ""}<br><span class="muted">${esc(s.attribution)}</span></li>`).join("");
}

export function render(state) {
  const {ctx} = state;
  sources = state.sources; banks = state.banks; cited = [];
  $("matched").textContent = ctx.address.matched || "Address not found";
  $("meta").textContent = ctx.address.status !== "found" ? `Not found (${words(ctx.address.reason || ctx.address.status)}).`
    : `Checked against ${ctx.parcel.spatial_method === "parcel" ? "the parcel boundary" : "the address point only, because no parcel was matched"} · ${ctx.generated_at.slice(0, 16).replace("T", " ")} New Zealand time`;
  draw("hazards", hazards, state); draw("facts", facts, state); draw("loan", loan, state); draw("notes", buildNotes, state);
  draw("links", () => ctx.links.map(l => `<li>${link(l.url, l.label)}${LINK_KIND[l.kind] || ""}</li>`).join(""));
  renderSummary(state);
  $("disclaimer").textContent = state.rules?.disclaimer || "";
}
