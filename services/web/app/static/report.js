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
const IN_DETAILS = "in your details at the top of the page";  // where the folded form is

const money = v => "$" + Math.round(Number(v)).toLocaleString("en-NZ");
const pct = v => Number(v).toLocaleString("en-NZ", {maximumFractionDigits: 2}) + "%";
const m2 = v => Number(v).toLocaleString("en-NZ") + " m²";
const words = code => String(code).replace(/_/g, " ");
const count = (n, noun) => `${n} ${noun}${n === 1 ? "" : "s"}`;
const code = v => `<span class="code">${esc(v)}</span>`;  // a number copied from a record, where 0 and O, 1 and l must not be confused
const DEV = ["localhost", "127.0.0.1"].includes(location.hostname);  // raw codes show on a developer's own machine only
const dev = code => DEV ? ` <span class="muted">[dev: ${esc(code)}]</span>` : "";
// A reason that is a fact about the property or the data is said in words; a source that failed is not explained.
const REASONS = {
  non_rateable_property: "The council lists this property as non-rateable.",
  council_shows_0_for_cross_lease_or_unit_title: "The council entry does not provide a usable land area for this property.",
  property_category_has_no_build_decade: "The council's property category for this address has no build decade.",
  not_on_council_page: "Not shown on the council property page.",
  no_unique_council_property_match: "The council records could not be matched to a single property at this address.",
  no_free_source: "No free public source has this.",
  no_licensed_source: "We have no licensed source for sale prices.",
  outside_auckland: "This address is outside Auckland. This tool covers Auckland only.",
};
const gone = f => REASONS[f.reason] || (f.status === "unavailable" ? "Could not be checked." : "Not found in the records we checked.");
const missing = f => gone(f) + dev(f.reason || f.status);
const span = (r, fmt) => !r ? "not available" : r[0] === r[1] ? fmt(r[0]) : `${fmt(r[0])} to ${fmt(r[1])}`;
const or = (v, fmt) => v == null ? "not published" : fmt(v);
const list = items => items.length ? `<ul class="plain">${items.map(i => `<li>${i}</li>`).join("")}</ul>` : "";
const row = (name, detail, tag) => `<div class="row"><b>${name}</b>${tag ? `<span class="tag ${tag}">${esc(STATUS[tag] || tag)}</span>` : ""}<div class="detail">${detail}</div></div>`;
const where = ctx => ctx.parcel.spatial_method === "parcel" ? "on this parcel" : "at the address point";
const unchecked = h => h.status === "unavailable" || h.status === "not_found";
export const floodMapped = ctx => ctx.hazards.filter(h => h.priority === "primary" && h.status === "intersects");
const YEAR = /^\d{4}s?$/;  // a year or a decade, as opposed to a council property category typed in the same field
const ratesText = r => r.status === "found" ? `${money(r.value)} for ${esc(r.rating_year)}` : null;

let sources, banks, cited;  // sources and bank names by id; the sources cited in this render, in order of first use
const bank = id => banks[id] || id;

// One source: its name as the link, then extra lines (data date, quote), credit line, licence.
function card(ref, lines = []) {
  const s = typeof ref === "string" ? sources[ref] : ref;
  if (!s) return `<li class="muted">Source unavailable</li>`;
  if (!cited.some(c => c.id === s.id)) cited.push(s);
  const more = [...lines, s.attribution, s.licence && "Licence: " + s.licence, s.accessed_at && "Accessed " + s.accessed_at];
  return `<li>${link(s.url, s.title)}<br><span>${more.filter(Boolean).map(esc).join("<br>")}</span></li>`;
}
function kbCard(k) {
  const s = sources[k.source_id] || {id: k.source_id || k.id, title: k.source_title, url: k.source_url, licence: k.licence};
  return card(s, [`Quoted: "${k.quote}"`, s.published_or_updated && "Published or updated " + s.published_or_updated]);
}
// The one control at the end of a line that opens its sources in place.
const control = (cards, label) => !cards.length ? "" : `<details class="src"><summary>${label || (cards.length === 1 ? "Source" : cards.length + " sources")}</summary><ul class="cards">${cards.join("")}</ul></details>`;
const cite = (refs, lines) => control([].concat(refs ?? []).map(ref => card(ref, lines)));
const kbCite = k => control([kbCard(k)]);

function notes({rules, kb}, block, heading) {
  const mine = (kb || []).filter(k => (TOPIC_BLOCK[k.topic] || "notes") === block), told = new Set(rules?.kb_items_from_user_input);
  if (!mine.length) return "";
  return `<h4>${heading}</h4>` + mine.map(k => `<div class="note">${esc(k.claim_en)} ${kbCite(k)}
      ${k.source_type === "secondary" ? `<span class="tag none">secondary source</span>` : ""}
      ${k.status === "verified" ? "" : `<span class="tag draft">draft, not yet checked</span>`}
      ${told.has(k.id) ? `<div class="muted">Based on what you told us.</div>` : ""}
      ${k.next_step_en ? `<div data-step="${esc(k.next_step_en)}"><b>Next step:</b> ${esc(k.next_step_en)}</div>` : ""}</div>`).join("");
}

// code: [block, text]. A code may carry a layer or bank id after a colon. CODES.md marks the hidden ones as not user-facing.
const HIDDEN = /^(kb_hash_mismatch|kb_verification_incomplete|hazard_status_missing|hazard_status_unknown|hazard_priority_unknown)$/;
const WARNINGS = {
  hazards_unavailable: ["hazards", "No flood layer could be checked. " + NO_RISK_CAVEAT],
  hazards_partial: ["hazards", layer => `${layerName(layer)} could not be checked. ${NO_RISK_CAVEAT}`],
  price_missing: ["loan", "There is no price to calculate with. Enter a price " + IN_DETAILS + "."],
  deposit_missing: ["loan", "Enter a deposit to see the loan amount and repayments."],
  income_missing: ["loan", "Enter the household income to see the debt-to-income ratio and First Home Loan figures."],
  cash_purchase_no_loan: ["loan", "The deposit equals the price, so no loan is needed and no repayments are shown."],
  deposit_exceeds_price: ["loan", "The deposit covers the whole price, so no loan is needed and no repayments are shown. Check the price and the deposit " + IN_DETAILS + "."],
  existing_debts_assumed_zero: ["loan", "The debt-to-income ratio assumes you have no other debts. Enter your existing debts " + IN_DETAILS + ", or 0 if you have none."],
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
  title_type_unmapped: ["notes", "We have no guidance note for this type of title yet, so ask your lawyer what it means for you."],
  year_built_user_conflict: ["notes", "The year you gave differs from the council record, which we used; please check which is right."],
  cladding_user_conflict: ["notes", "The cladding you gave differs from the property record, which we used; please check which is right."],
  year_built_unavailable: ["notes", (_, r, told) => told.year_built ? null : "The year built could not be found. Check the listing for it, then enter it " + IN_DETAILS + "."],
  year_built_unparseable: ["notes", "The year built could not be read. Check the listing for it."],
  cladding_unavailable: ["notes", (_, r, told) => told.cladding ? null : "Cladding is not available from any free public source. Check the listing for it, then choose it " + IN_DETAILS + "."],
  chat_year_built_unparseable: ["notes", "The year built you entered could not be used. Enter a four-digit year or a decade such as 1990s."],
  kb_unavailable: ["notes", "Background notes are unavailable right now."],
};

// told: the year built and cladding the report was calculated with, not what is typed in the folded form
function warnings({rules, inputs}, block) {
  const told = inputs?.chat_supplied || {};
  return list((rules?.warnings || []).map(w => {
    const [code, arg] = w.split(":"), known = WARNINGS[code];
    if (HIDDEN.test(code)) return null;
    if (!known) return DEV && block === "loan" ? esc("Note: " + words(w)) : null;
    return known[0] === block ? esc(typeof known[1] === "function" ? known[1](arg, rules, told) : known[1]) : null;
  }).filter(Boolean));
}

// How each layer is drawn on the map. The rows of the hazards section carry the same colour and line, so the list is the map's key.
export const MAP_STYLE = {
  flood_plain: {color:"#1f6fd1", fillOpacity:.45, weight:2}, flood_prone_area: {color:"#7b3fc4", fillOpacity:.45, weight:2, dashArray:"4 3"},
  overland_flow_path: {color:"#0b3d91", weight:3, dashArray:"6 4", fillOpacity:0}, coastal_inundation: {color:"#0e8a7d", fillOpacity:.45, weight:2},
  tsunami_evacuation_zone: {color:"#d9730d", fillOpacity:.35, weight:2}, coastal_erosion: {color:"#7a4a12", fillOpacity:.4, weight:2, dashArray:"1 4"}, parcel: {color:"#111", weight:3, fillOpacity:0}};
// What the council's layer says where it is mapped. "Not mapped" and "could not be checked" are said once, by the group a layer sits in.
function mapped(h, at) {
  const near = /^point_method_line_within_(\d+)m$/.exec(h.reason || "");
  if (near) return `Within ${near[1]} m of the address point.`;
  const bits = [h.susceptibility ? `Highest class ${at}: ${esc(h.susceptibility)}` : h.classes.length ? `Class ${at}: ${h.classes.map(esc).join(", ")}` : "",
    h.overlap_pct != null ? `Covers ${h.overlap_pct || "less than 0.1"}% of the parcel` : "",  // 0 is a sliver that rounds to 0.0
    h.crosses_parcel ? "Crosses the parcel" : ""].filter(Boolean);
  return (bits.join(". ") || `Mapped ${at}`) + ".";
}

// One layer: its mark as on the map, its name, what was found, a bar as long as the share of the parcel it covers, its source.
function layerRow(h, detail) {
  const style = MAP_STYLE[h.layer];  // a layer coloured by class has no one colour: its row says the class in words, and the map's own key shows the scale
  const mark = style ? (style.fillOpacity === 0 ? " line" : style.dashArray ? " dash" : "") : " class";
  return `<div class="layer" style="--k:${style?.color || "transparent"}"><span class="key${mark}"></span><b>${esc(layerName(h.layer))}</b>
    ${detail ? `<span>${detail}</span>` : ""}${h.status === "intersects" && h.overlap_pct != null ? `<span class="bar" style="width:${Number(h.overlap_pct)}%"></span>` : ""}
    ${cite(h.source_id, [h.data_date && "Data date " + h.data_date])}</div>`;
}

function hazards(state) {
  const {ctx} = state, at = where(ctx), classed = h => h.status === "intersects" && (h.susceptibility || h.classes.length > 0);
  const group = (title, said, test, detail) => {
    const layers = ctx.hazards.filter(test);
    return layers.length ? `<h4>${title}</h4>${said ? `<p>${said}</p>` : ""}${layers.map(h => layerRow(h, detail(h))).join("")}` : "";
  };
  const none = (priority, name) => ctx.hazards.some(h => h.priority === priority) ? "" : `<p class="problem">No ${name} layer was checked. ${NO_RISK_CAVEAT}</p>`;
  return `${ctx.parcel.spatial_method === "parcel" || ctx.address.status !== "found" ? "" : `<p class="problem">No parcel was matched, so the layers were checked at the address point only and parcel coverage is not calculated.</p>`}
    ${none("primary", "flood")}${none("secondary", "other")}
    ${group("Could not be checked", `These layers could not be checked. ${NO_RISK_CAVEAT}`, unchecked, h => esc(REASONS[h.reason] || "") + dev(h.reason || h.status))}
    ${group(`Mapped ${at}`, "", h => h.status === "intersects" && !classed(h), h => mapped(h, at))}
    ${group("Region-wide layers, where the class is the finding", "", classed, h => mapped(h, at))}
    ${group(`Not shown ${at}`, `The council does not show these layers ${at}. ${NO_RISK_CAVEAT}`, h => h.status === "none", () => "")}
    ${warnings(state, "hazards")}${notes(state, "hazards", "Notes on these hazards and insurance")}`;
}

function facts({ctx}) {
  const p = ctx.property, council = ctx.links.find(l => l.id === "council_valuation"), rows = [];
  const listings = ctx.links.filter(l => l.id === "homes" || l.id === "oneroof").map(l => link(l.url, l.label)).join(" or ");
  const onListings = [p.year_built, p.cladding, p.last_sale];  // facts the listing sites show: a missing one points to them
  const add = (name, f, text) => rows.push(row(name, (text || missing(f)
    + (council && (f.source_id || "").startsWith("ac_") ? ` ${link(council.url, "Check the council property search")}.` : "")
    + (listings && onListings.includes(f) ? ` Look for it on ${listings}.` : ""))
    + " " + cite(f.source_id), text ? "found" : f.status));
  const fact = (name, f, fmt = esc) => add(name, f, f.status === "found" ? fmt(f.value) : null);
  add("Capital value (CV)", p.cv, p.cv.status === "found" ? `${money(p.cv.value)} as at ${esc(p.cv.valuation_date)}. A council rating valuation, not a market value.` : null);
  add("Annual rates", p.annual_rates, ratesText(p.annual_rates));
  add("Title type", p.title_type, p.title_type.value ? esc(p.title_type.value) : (p.title_type.linz_types || []).length ? `LINZ records: ${p.title_type.linz_types.map(esc).join(", ")}${dev(p.title_type.reason)}` : null);
  const r = p.year_built.decade_range;
  add("Decade built", p.year_built, r ? (r[0] ? `${r[0]}-${r[1]}` : `Before ${r[1] + 1}`) + " (from the council property category)" : null);
  fact("Land value", p.land_value, money); fact("Value of improvements", p.improvements_value, money);
  fact("Rateability", p.rateability); fact("Improvements", p.improvement_description);
  fact("Land use", p.land_use); fact("Land area", p.land_area_m2, m2); fact("Total floor area", p.floor_area_m2, m2);
  fact("Building site coverage", p.building_site_coverage_m2, m2); fact("Legal description", p.legal_description, code);
  fact("Record of title", p.record_of_title, code); fact("Valuation number", p.valuation_number, code); fact("Local board", p.local_board);
  fact("Cladding", p.cladding); add("Last sale", p.last_sale);
  return rows.join("");
}

const FLAGS = {
  lvr_over_80: () => "The deposit is under 20% of the price (LVR above 80%). A low-equity margin or a First Home Loan may apply.",
  dti_over_6: r => `Total debt would be more than 6 times income, above the Reserve Bank's high-DTI threshold, and lenders restrict this kind of lending. For this income the threshold is a loan of ${money(r.high_dti_threshold)}.`,
  fhl_deposit_below_5: () => "The First Home Loan scenario is not shown because the deposit is under 5% of the price.",
  fhl_income_over_cap: r => `The First Home Loan scenario is not shown because the income is above its cap: ${money(r.fhl_income_caps.single)} for one applicant, ${money(r.fhl_income_caps.multiple_or_dependants)} for two or more applicants or one with dependants.`,
  fhl_household_type_unknown: () => "First Home Loan eligibility depends on the household. Choose it " + IN_DETAILS + ".",
};

const stressRow = (r, detail) => row(`If rates were ${r.stress_add_pp} percentage points higher`, detail + " An illustration, not any bank's test rate.");
const table = (head, rows) => `<div class="scroll"><table><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr>${rows.map(cells => `<tr>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table></div>`;
const SCENARIOS = {
  bank_standard: (s, r) => `<h4>Bank loan at published 1-year rates</h4>
    <div class="note">Rates of ${span(s.rate_range_pct, pct)} across ${s.per_bank.length} banks${s.low_equity_margin_pct ? `, including a low-equity margin of ${span(s.low_equity_margin_pct, String)} percentage points where a bank charges one` : ""}. ${cite(s.source_ids)}</div>
    ${row("Monthly repayment", span(s.monthly_range, money))}${row("Fortnightly repayment", span(s.fortnightly_range, money))}
    ${stressRow(r, `${money(s.stress_monthly)} per month at the highest of these rates (${money(s.stress_delta_monthly)} more).`)}
    <details class="more"><summary>Rate used for each bank</summary>${table(["Bank", "Rate", "Basis", "Collected"],
      s.per_bank.map(b => [esc(bank(b.bank_id)), pct(b.rate_pct), esc(BASIS[b.basis] || b.basis), esc((b.captured_at || "not recorded").slice(0, 10))]))}</details>`,
  first_home_loan: (s, r) => `<h4>First Home Loan</h4>
    ${s.eligibility === "conditional" ? `<div class="problem">${esc(s.eligibility_note)}</div>` : ""}
    <div class="note">${esc(s.lmi_note)} ${cite(s.source_ids)}</div>
    ${row("Monthly repayment", span(s.monthly_range, money))}${row("Fortnightly repayment", span(s.fortnightly_range, money))}
    ${stressRow(r, `${span(s.stress_monthly_range, money)} per month.`)}
    ${row("Insurance premium at the full rate", money(s.lmi_amount) + (s.full_premium_assumed_for.length ? `. The full premium is assumed for ${s.full_premium_assumed_for.map(id => esc(bank(id))).join(", ")}.` : ""))}
    <details class="more"><summary>Figures for each lender</summary>${table(["Lender", "Rate", "Basis", "Premium passed on", "Monthly", "Fortnightly", `Monthly at +${r.stress_add_pp} points`],
      s.lenders.map(l => [esc(l.name), or(l.rate_pct, pct), esc(BASIS[l.rate_basis] || l.rate_basis), or(l.lmi_passthrough_pct, pct), or(l.monthly, money), or(l.fortnightly, money), or(l.stress_monthly, money)]))}</details>`,
  user_rate: (s, r) => `<h4>At the interest rate in your details (${pct(s.rate_pct)})</h4>
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
  out.push(...r.scenarios.map(s => SCENARIOS[s.id] ? SCENARIOS[s.id](s, r) : `<h4>${esc(words(s.id))}</h4>`));
  if (r.scenarios.length) out.push(`<div class="muted">Repayments are principal and interest over ${r.loan_term_years} years. They do not include council rates, insurance, utilities or maintenance.${r.rates_snapshot_date ? ` Bank rates were collected by hand from each bank's website; the oldest is from ${esc(r.rates_snapshot_date)}. ${cite("bank_rates_snapshot")}` : ""}</div>`);
  out.push(row("Council rates (not included above)", ratesText(rates) ? `${ratesText(rates)} ${cite(rates.source_id)}` : missing(rates)));
  return out.join("") + warnings(state, "loan") + notes(state, "loan", "Lending notes");
}

function buildNotes(state) {
  const {rules: r} = state;
  if (!r) return state.rulesError ? `<p class="problem">The notes are unavailable until the repayment figures can be calculated.</p>` : `<p class="muted">Loading…</p>`;
  const EMPTY = {no_verified_items: "No checked background notes are available yet.", no_matching_items: "No background notes apply to this property."};
  return (r.kb_includes_draft ? `<p class="problem">Development mode: draft notes are shown. They have not yet been checked against their sources.</p>` : "")
    + (state.kb ? "" : `<p class="problem">The background notes could not be loaded.</p>`)
    + warnings(state, "notes")
    + (notes(state, "notes", "Notes for this property") || (!state.kb || r.kb_empty_reason === "kb_unavailable" ? ""
       : `<p class="muted">${EMPTY[r.kb_empty_reason] || "No build-era or title notes apply to this property."}</p>`));
}

// The sources a sentence from report-api cites: a source id is a PropertyContext source, a rules-api source or a
// knowledge-base item. A source cited through one of its items needs no second card.
function cardsOf(s, items) {
  const quoted = new Set(s.source_ids.map(id => items[id]?.source_id));
  return s.source_ids.filter(id => !quoted.has(id)).map(id => items[id] ? kbCard(items[id]) : card(id));
}
// A sentence that passed the check against its sources is highlighted. A fixed sentence from the program cites none.
function checked(s, items) {
  const cards = cardsOf(s, items), n = cards.length;
  return `<p class="s">${n ? `<span class="hl">${esc(s.text)}</span>` : esc(s.text)}</p>${control(cards, `Checked against ${count(n, "source")}`)}`;
}
const reworded = n => !n ? "" : `<p class="muted">${n === 1 ? "1 of these sentences is" : n + " of these sentences are"} in the program's own wording, because the model's version did not pass the check against the sources.</p>`;

// How the summary is made, which is also its progress: `at` is the step under way, or the one that failed.
const steps = (at, failed) => `<ol class="steps" aria-label="How this summary is made">${["Facts", "Wording", "Source check"].map((name, i) => {
  const [mark, said] = i < at ? ["done", "done"] : i > at ? ["", "waiting"] : failed ? ["failed", "failed"] : ["doing", "in progress"];
  return `<li class="${mark}">${name}<span class="sr">: ${said}</span></li>`; }).join("")}</ol>`;

// report-api only returns sentences that passed its check against the sources they cite.
function summary({rules, rulesError, kb, report: r, reportError: err, done}) {
  if (!rules) return rulesError ? steps(0, true) + `<p class="problem">The summary is unavailable until the repayment figures can be calculated.</p>`
    : steps(0) + `<p class="muted">Reading the records…</p>`;
  if (err) return err.code === "validation_failed"
    ? steps(2, true) + `<p class="problem">No summary sentence passed the check against its sources, so none is shown. The rest of the report is not affected.</p>`
    : steps(1, true) + `<p class="problem">The summary is unavailable right now. The rest of the report is not affected.</p><button type="button" id="retry-summary">Try again</button>`;
  if (!r) return steps(1) + `<p class="muted">Writing the summary…</p>`;
  const items = byId(kb || []);
  return steps(3) + r.summary.map(s => checked(s, items)).join("") + reworded(r.dropped)
    + `<p class="muted rec">Summary written by ${esc(r.model)}. A sentence is shown only when it passes a check against the sources it cites.</p>`
    + (r.next_steps.length ? `<h3>Next steps</h3><ul class="todo">${r.next_steps.map(s => `<li><label><input type="checkbox"${done.has(s.text) ? " checked" : ""}><span>${esc(s.text)}</span></label>${control(cardsOf(s, items))}</li>`).join("")}</ul>` : "")
    + (r.not_found_notes.length ? `<h3>Could not be found</h3><div class="gap">${list(r.not_found_notes.map(n => esc(n.text)))}</div>` : "");
}

const LINK_KIND = {direct: " (opens this property)", first_result: " (opens the first search result for this address on that site)", home: " (search for the address there)"};
const linkItem = l => `${link(l.url, l.label)}${LINK_KIND[l.kind] || ""}`;

// An answer Gemini found with Google Search, for a question the report does not cover. The Gemini API terms ask for
// it to be shown as returned, with Google's search suggestions: the text is plain text, the suggestions are Google's
// own HTML and sit in a frame that runs no scripts.
// The language of the text is not known here, so it does not inherit the page's.
const webAnswer = w => `<div class="web"><p><b>Found by Gemini with Google Search. Not checked against this report's sources.</b></p>
  <div class="say" lang="">${w.text.split(/\n\s*\n/).map(p => `<p>${esc(p)}</p>`).join("")}</div>${list(w.sources.map(s => link(s.url, s.title || "Source")))}
  ${w.search_suggestions ? `<iframe class="suggest" title="Google Search suggestions" sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc="${esc('<base target="_blank">' + w.search_suggestions)}"></iframe>` : ""}</div>`;

// The conversation about this report, in order. A turn holds the question and, once settled, report-api's answer with
// the knowledge-base items on the page at that time, or the error. The sentences carry the language the answer names.
// The links an answer names are PropertyContext links; an id the report does not have is left out.
function thread({ctx, rules, rulesError, kb: onPage, thread: turns}) {
  const answer = ({answer: a, error: err, added, kb}, items = byId(kb || onPage || [])) => err ? (err.code === "validation_failed"
      ? `<p class="problem">No answer sentence passed the check against its sources, so none is shown. Try asking in a different way.</p>`
      : `<p class="problem">The answer is unavailable right now. The rest of the report is not affected.</p><button type="button" id="retry-answer">Try again</button>`)
    : !a ? `<p class="muted">Answering…</p>`
    : (a.web ? webAnswer(a.web) : `<div class="say${a.answer.some(s => s.source_ids.length) ? "" : " fixed"}"${a.language ? ` lang="${esc(a.language)}"` : ""}>${a.answer.map(s => checked(s, items)).join("")}</div>` + reworded(a.dropped))
      + list((a.links || []).map(id => ctx.links.find(l => l.id === id)).filter(Boolean).map(linkItem))
      + (added?.length ? `<p class="muted rec">Added to your details from your question: ${esc(added.join("; "))}.</p>` : "");
  return turns.map(t => `<div class="msg you">${esc(t.question)}</div><div class="msg">${answer(t)}</div>`).join("")
    + (!rules && rulesError ? `<p class="problem">Questions are unavailable until the repayment figures can be calculated.</p>` : "");
}

// a block that cannot be drawn must not take the others with it
function draw(id, block, state) {
  try { $(id).innerHTML = block(state); }
  catch (e) { console.error(e); $(id).innerHTML = `<p class="problem">This section could not be shown.</p>`; }
}

function listSources({ctx}) {
  $("sources").innerHTML = cited.map(s => card(s)).join("");
  $("d-originals").textContent = `${ctx.links.length} sites to look at yourself, and the ${cited.length} sources cited on this page.`;
}

// The summary and the answers arrive after the rest and are drawn on their own: redrawing the other blocks would close the sources a reader has opened.
// A next step on the summary's list is not said again under the note it comes from; without the list, the notes keep theirs.
export function renderSummary(state) {
  draw("summary", summary, state);
  const listed = new Set((state.report?.next_steps || []).map(s => s.text));
  for (const el of document.querySelectorAll("[data-step]")) el.hidden = listed.has(el.dataset.step);
  listSources(state);
}

// One question at a time, and only about a report that has its figures.
export function renderThread(state) {
  draw("thread", thread, state);
  $("ask").disabled = !state.inputs || state.thread.some(t => !t.answer && !t.error);
  listSources(state);
}

// Shows the words of the newest answer the way they would be typed. Only the display is paced: the answer has arrived
// whole, because its sentences are checked, or its sources attached, before any of it is shown, and its sources and
// links are on the page from the first word. A reader who asked for less motion gets the words at once, and a screen
// reader hears them once, when they are complete.
export function typeAnswer() {
  const box = $("thread"), el = [...box.querySelectorAll(".msg:not(.you) .say")].at(-1);
  if (!el || document.hidden || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const nodes = [], walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) if (walk.currentNode.data.trim() && !walk.currentNode.parentElement.closest(".src")) nodes.push(walk.currentNode);
  const texts = nodes.map(n => n.data);
  let i = 0, at = 0;
  nodes.forEach(n => n.data = ""); box.ariaBusy = "true";
  const timer = setInterval(() => {
    if (at >= (texts[i] || "").length) { i++; at = 0; }
    if (i >= nodes.length || !el.isConnected) { box.ariaBusy = "false"; return clearInterval(timer); }
    nodes[i].data = texts[i].slice(0, at += 3);
  }, 20);
}

// What the report on the page was calculated with, one line each, so the form can stay folded away.
// A year or cladding is only said when the rules used it: not a council category code, and not a value the record overrode.
function readout({rules: r, inputs: i}) {
  const amount = (v, word) => v == null ? null : `${money(v)} ${word}`, price = r.price_used;
  const used = (field, ok = true) => ok && !r.warnings.includes(field + "_user_conflict") && i.chat_supplied[field], told = {year_built: used("year_built", YEAR.test(i.chat_supplied.year_built)), cladding: used("cladding")};
  const loan = [amount(i.deposit, "deposit"), amount(i.household_income, "income"), amount(i.existing_debts, "debts")].filter(Boolean);
  const home = [price.value != null && `${money(price.value)} price${price.basis === "cv" ? " (the council valuation)" : ""}`,
    told.year_built && `built ${told.year_built}, as you told us`, told.cladding && `${words(told.cladding)} cladding, as you told us`].filter(Boolean);
  return `<b>Your loan</b> ${esc(loan.join(", ") || "no details yet")}<br><b>This home</b> ${esc(home.join(", ") || "no price yet")}`;
}

// One line under each section title, from values the section shows with their sources.
function digests({ctx, rules: r, rulesError, inputs}) {
  const p = ctx.property, flood = ctx.hazards.filter(h => h.priority === "primary"), mapped = floodMapped(ctx).map(h => layerName(h.layer));
  const missed = ctx.hazards.filter(unchecked).length, tail = missed ? ` ${count(missed, "layer")} could not be checked.` : "";
  const scenario = id => r?.scenarios.find(s => s.id === id), bankLoan = scenario("bank_standard"), fhl = scenario("first_home_loan"), own = scenario("user_rate");
  const decade = p.year_built.decade_range, told = YEAR.test(inputs?.chat_supplied.year_built) && inputs.chat_supplied.year_built, title = p.title_type.value && `${p.title_type.value} title.`;
  return {
    hazards: mapped.length ? `Mapped ${where(ctx)}: ${mapped.join(", ")}.` + tail
      : flood.length && flood.every(h => h.status === "none") ? `No flood layer is mapped ${where(ctx)}. ${NO_RISK_CAVEAT}` + tail
      : `The flood layers could not all be checked. ${NO_RISK_CAVEAT}`,
    facts: [p.cv.status === "found" && `Council valuation ${money(p.cv.value)} as at ${p.cv.valuation_date}.`, title,
      p.land_area_m2.status === "found" && `Land area ${m2(p.land_area_m2.value)}.`].filter(Boolean).join(" ") || "Council and LINZ records for this address.",
    loan: rulesError ? "The repayment figures could not be calculated." : !r ? "Calculating…"
      : [bankLoan && `${span(bankLoan.monthly_range, money)} a month at bank rates.`, fhl && `${span(fhl.monthly_range, money)} a month with a First Home Loan.`,
        own && `${money(own.monthly)} a month at your rate.`].filter(Boolean).join(" ") || "No repayments are shown yet.",
    notes: [title, decade ? `Built ${decade[0] ? `${decade[0]}-${decade[1]}` : `before ${decade[1] + 1}`}.`
      : told ? `Built ${told}, as you told us.` : "The year built is not known."].filter(Boolean).join(" "),
  };
}

export function render(state) {
  const {ctx} = state;
  sources = state.sources; banks = state.banks; cited = [];
  draw("readout", () => state.inputs ? readout(state) : "<b>Your details</b>");
  try { for (const [id, text] of Object.entries(digests(state))) $("d-" + id).textContent = text; } catch (e) { console.error(e); }
  $("matched").textContent = ctx.address.matched || "Address not found";
  $("meta").textContent = ctx.address.status !== "found" ? gone(ctx.address)
    : `Checked against ${ctx.parcel.spatial_method === "parcel" ? "the parcel boundary" : "the address point only, because no parcel was matched"}, ${ctx.generated_at.slice(0, 16).replace("T", " ")} New Zealand time.`;
  draw("hazards", hazards, state); draw("facts", facts, state); draw("loan", loan, state); draw("notes", buildNotes, state);
  draw("links", () => ctx.links.map(l => `<li>${linkItem(l)}</li>`).join(""));
  renderSummary(state); renderThread(state);
  $("disclaimer").textContent = state.rules?.disclaimer || "";
}
