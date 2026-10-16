// Draws the report blocks from PropertyContext, RulesOutput and knowledge-base items.
// Wording of flags and warnings follows CODES.md: "could not check" is never shown as "no risk".
export const $ = id => document.getElementById(id);
export const byId = items => Object.fromEntries(items.map(i => [i.id, i]));
export const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// noreferrer: DuckDuckGo only jumps to the first result when the click does not name the page it came from
// a PDF says so: some browsers save it instead of showing it
export const link = (url, text) => `<a href="${/^https?:\/\//.test(url || "") ? esc(url) : "#"}" target="_blank" rel="noreferrer">${esc(text)}</a>${/\.pdf$/i.test(url || "") ? " (PDF)" : ""}`;
// "1% chance each year": the layer's own description, "the AEP is the % probability of an event occurring each year"
// (ArcGIS item e1b4f3e57dc64b6395098a17ac979eeb, its source card). The coastal erosion name keeps the council's codes:
// its description explains ASCIE but not 2130 or RCP8.5+.
const LAYERS = {flood_plain:"Flood plains", flood_prone_area:"Flood prone areas", overland_flow_path:"Overland flow paths",
  coastal_inundation:"Coastal inundation (1% chance each year)", landslide_shallow:"Landslide susceptibility - shallow",
  landslide_large_scale:"Landslide susceptibility - large scale", liquefaction:"Liquefaction vulnerability",
  tsunami_evacuation_zone:"Tsunami evacuation zone", coastal_erosion:"Coastal erosion (ASCIE 2130, RCP8.5+)"};
export const layerName = id => LAYERS[id] || id;

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
const row = (name, detail) => `<div class="row"><b>${name}</b><div class="detail">${detail}</div></div>`;
const fold = (title, inside, open) => inside ? `<details class="more"${open ? " open" : ""}><summary>${title}</summary>${inside}</details>` : "";
const where = ctx => ctx.parcel.spatial_method === "parcel" ? "on this parcel" : "at the address point";
const unchecked = h => h.status === "unavailable" || h.status === "not_found";
export const floodMapped = ctx => ctx.hazards.filter(h => h.priority === "primary" && h.status === "intersects");
const YEAR = /^\d{4}s?$/;  // a year or a decade, as opposed to a council property category typed in the same field
const ratesText = r => r.status === "found" ? `${money(r.value)} for ${esc(r.rating_year)}` : null;
// A year built or cladding the records lack is the visitor's own, or was found by a web search and is not checked: the
// inputs the report was calculated with say which, and every place that states the value says so too.
const searched = (inputs, field) => inputs?.chat_supplied_source?.[field] === "web_search";
const how = (inputs, field) => searched(inputs, field) ? "from a web search, not checked" : "as you told us";
const FIELD_OF = {build_era: "year_built", cladding: "cladding"};  // the detail a note selected by it depends on, by the note's topic

// sources and bank names by id; the sources cited in this render, in order of first use; and by source id, what the
// lines that cite it add to its entry in the full list (the words quoted from it, the date of a layer's data)
let sources, banks, cited, noted;
const bank = id => banks[id] || id;

// One source where it is cited: its name as the link. `lines` go to its entry in the full list.
function card(ref, lines = []) {
  const s = typeof ref === "string" ? sources[ref] : ref;
  if (!s) return `<li class="muted">Source unavailable</li>`;
  if (!cited.some(c => c.id === s.id)) cited.push(s);
  for (const line of lines.filter(Boolean)) (noted[s.id] ??= new Set()).add(line);
  return `<li>${link(s.url, s.title)}</li>`;
}
// The same source in the full list of section 5: what was quoted from it, its credit line, licence and dates.
const credit = s => `<li>${link(s.url, s.title)}${[...(noted[s.id] || []), s.attribution, s.licence && "Licence: " + s.licence,
  s.published_or_updated && "Published or updated " + s.published_or_updated, s.accessed_at && "Accessed " + s.accessed_at].filter(Boolean).map(l => `<br><span>${esc(l)}</span>`).join("")}</li>`;
function kbCard(k) {
  const s = sources[k.source_id] || {id: k.source_id || k.id, title: k.source_title, url: k.source_url, licence: k.licence};
  return card(s, [`Quoted: "${k.quote}"`]);
}
// The one control at the end of a line that opens its sources in place.
const control = (cards, label, after = "") => !cards.length ? "" : `<details class="src"><summary>${label || (cards.length === 1 ? "Source" : cards.length + " sources")}</summary><ul class="cards">${cards.join("")}</ul>${after}</details>`;
const cite = (refs, lines) => control([].concat(refs ?? []).map(ref => card(ref, lines)));
const kbCite = k => control([kbCard(k)]);

const noteOf = ({rules, inputs}) => { const told = new Set(rules?.kb_items_from_user_input);
  return k => `<div class="note">${esc(k.claim_en)} ${kbCite(k)}
      ${k.source_type === "secondary" ? `<span class="tag">secondary source</span>` : ""}
      ${k.status === "verified" ? "" : `<span class="tag draft">draft, not yet checked</span>`}
      ${told.has(k.id) ? `<div class="muted">Based on ${searched(inputs, FIELD_OF[k.topic]) ? "a web search, not checked" : "what you told us"}.</div>` : ""}
      ${k.next_step_en ? `<div data-step="${esc(k.next_step_en)}"><b>Next step:</b> ${esc(k.next_step_en)}</div>` : ""}</div>`; };
// A note about this property stays in view. Notes that are the same for every home, and the two long lists of lending rules, are folded with a count.
const FOLDS = {always: "Background for any Auckland home", fhl_eligible: "First Home Loan rules", lvr_over: "Low-deposit lending rules"};
const foldOf = k => Object.keys(FOLDS).find(rule => rule in (k.applies_when || {}));
const FLOOD_COVER = "kb_flood_insurance_limit";  // the same for every home, but in view where a flood layer is mapped on the parcel
// `placed`: topics whose notes another part of the block shows (under the record they explain)
function notes(state, block, heading, placed = []) {
  const flooded = floodMapped(state.ctx).length > 0, shut = k => !(flooded && k.id === FLOOD_COVER) && foldOf(k);
  const mine = (state.kb || []).filter(k => (TOPIC_BLOCK[k.topic] || "notes") === block && !(placed.includes(k.topic) && !shut(k))
    && !Object.values(DEFINES).includes(k.id));
  const note = noteOf(state), open = mine.filter(k => !shut(k));
  return (open.length ? `<h4>${heading}</h4>${open.map(note).join("")}` : "")
    + Object.entries(FOLDS).map(([rule, title]) => { const inside = mine.filter(k => shut(k) === rule); return fold(`${title} (${count(inside.length, "note")})`, inside.map(note).join("")); }).join("");
}

// code: [block, text]. A code may carry a layer or bank id after a colon. CODES.md marks the hidden ones as not user-facing;
// the last two are said once, where "This home" lists what the records lack.
const HIDDEN = /^(kb_hash_mismatch|kb_verification_incomplete|hazard_status_missing|hazard_status_unknown|hazard_priority_unknown|year_built_unavailable|cladding_unavailable|existing_debts_assumed_zero)$/;
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
  year_built_unparseable: ["notes", "The year built could not be read. Check the listing for it."],
  chat_year_built_unparseable: ["notes", "The year built you entered could not be used. Enter a four-digit year or a decade such as 1990s."],
  kb_unavailable: ["notes", "Background notes are unavailable right now."],
};

// The warnings about one bank sit with the table of banks; `banksOnly` picks them, false leaves them out.
const BANK = /^(bank_skipped|lem_base_unverified)$/;
function warnings({rules}, block, banksOnly) {
  return list((rules?.warnings || []).map(w => {
    const [code, arg] = w.split(":"), known = WARNINGS[code];
    if (HIDDEN.test(code) || (banksOnly != null && BANK.test(code) !== banksOnly)) return null;
    if (!known) return DEV && block === "loan" ? esc("Note: " + words(w)) : null;
    return known[0] === block ? esc(typeof known[1] === "function" ? known[1](arg, rules) : known[1]) : null;
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
  // One line says how the layers fell; the layers found come first. Those not shown are named on one line, their caveat with them.
  const isMapped = h => h.status === "intersects" && !classed(h), shown = ctx.hazards.filter(h => h.status === "none"), gaps = ctx.hazards.filter(unchecked);
  const tally = [[ctx.hazards.filter(isMapped).length, `mapped ${at}`], [ctx.hazards.filter(classed).length, "graded across the region"],
    [shown.length, `not shown ${at}`], [gaps.length, "could not be checked"]].filter(([n]) => n).map(([n, what]) => `${n} ${what}`).join("; ");
  return `${ctx.parcel.spatial_method === "parcel" || ctx.address.status !== "found" ? "" : `<p class="problem">No parcel was matched, so the layers were checked at the address point only and parcel coverage is not calculated.</p>`}
    ${none("primary", "flood")}${none("secondary", "other")}
    ${ctx.hazards.length ? `<p class="tally">Of the ${count(ctx.hazards.length, "council layer")} checked: ${tally}.</p>` : ""}
    ${group("Could not be checked", `These layers could not be checked. ${NO_RISK_CAVEAT}`, unchecked, h => esc(REASONS[h.reason] || "") + dev(h.reason || h.status))}
    ${group(`Mapped ${at}`, "", isMapped, h => mapped(h, at))}
    ${group("Graded across the region", "", classed, h => mapped(h, at))}
    ${shown.length ? `<h4>Not shown ${at}</h4><div class="note">${shown.map(h => esc(layerName(h.layer))).join(", ")}. The council does not show these layers ${at}. ${NO_RISK_CAVEAT}
      ${control(shown.map(h => card(h.source_id, [h.data_date && "Data date " + h.data_date])))}</div>` : ""}
    ${warnings(state, "hazards")}${notes(state, "hazards", "Notes on these hazards and insurance")}`;
}

// The build-era, cladding and title notes sit under the record they explain.
const UNDER = {"Title type": ["title_type"], "Decade built": ["build_era"], "Cladding": ["cladding"]};
function facts(state) {
  const {ctx, inputs} = state, p = ctx.property, council = ctx.links.find(l => l.id === "council_valuation"), main = [], rest = [], lacking = [];
  const listings = ctx.links.filter(l => l.id === "homes" || l.id === "oneroof").map(l => link(l.url, l.label)).join(" or ");
  const told = inputs?.chat_supplied || {};  // the year built and cladding the report was calculated with
  const note = noteOf(state), era = eraless(state);
  const under = name => (state.kb || []).filter(k => (UNDER[name] || []).includes(k.topic) && !foldOf(k)).map(note).join("")
    + (name === "Decade built" && era ? `<p class="muted">${esc(ERALESS(era))}</p>` : "") + defined(state, name);
  // a value the records have goes to `to`; one they lack is listed with the reason, and where to look is said once for all of them.
  // `search`: the field report-api can search the web for, offered while the records and the details have no value for it
  const using = field => told[field] ? ` Using ${esc(field === "cladding" ? words(told[field]) : told[field])}, ${how(inputs, field)}.` : "";
  const add = (to, name, f, text, search) => (text ? to : lacking).push(row(name, `${text || missing(f) + using(search)} ${cite(f.source_id)}${
    !text && search && f.status !== "found" && !told[search] ? `<button type="button" class="search" data-search="${search}">Search the web</button>` : ""}${under(name)}`));
  const look = `<p class="muted">Look for these on the listing${listings && `, on ${listings}`}${council ? `, or in the ${link(council.url, "council property search")}` : ""}.${
    (!p.year_built.decade_range && !told.year_built) || (p.cladding.status !== "found" && !told.cladding) ? ` A year built or a cladding you find can be added ${IN_DETAILS}.` : ""}</p>`;
  const fact = (to, name, f, fmt = esc, search) => add(to, name, f, f.status === "found" ? fmt(f.value) : null, search);
  const cvNote = (state.kb || []).some(k => k.id === DEFINES["Capital value (CV)"]);  // the note under the row says it is not a market value
  add(main, "Capital value (CV)", p.cv, p.cv.status === "found" ? `${money(p.cv.value)} as at ${esc(p.cv.valuation_date)}.${cvNote ? "" : " A council rating valuation, not a market value."}` : null);
  add(main, "Annual rates", p.annual_rates, ratesText(p.annual_rates));
  add(main, "Title type", p.title_type, p.title_type.value ? esc(p.title_type.value) : (p.title_type.linz_types || []).length ? `LINZ records: ${p.title_type.linz_types.map(esc).join(", ")}${dev(p.title_type.reason)}` : null);
  const r = p.year_built.decade_range;
  add(main, "Decade built", p.year_built, r ? (r[0] ? `${r[0]}-${r[1]}` : `Before ${r[1] + 1}`) + " (from the council property category)" : null, "year_built");
  fact(main, "Land area", p.land_area_m2, m2); fact(main, "Total floor area", p.floor_area_m2, m2); fact(main, "Cladding", p.cladding, esc, "cladding"); add(main, "Last sale", p.last_sale, null, "last_sale");
  fact(rest, "Land value", p.land_value, money); fact(rest, "Value of improvements", p.improvements_value, money);
  fact(rest, "Rateability", p.rateability); fact(rest, "Improvements", p.improvement_description); fact(rest, "Land use", p.land_use);
  fact(rest, "Building site coverage", p.building_site_coverage_m2, m2); fact(rest, "Legal description", p.legal_description, code);
  fact(rest, "Record of title", p.record_of_title, code); fact(rest, "Valuation number", p.valuation_number, code); fact(rest, "Local board", p.local_board);
  return (main.length ? `<h4>Main facts</h4>${main.join("")}` : "") + homeNotes(state) + (lacking.length ? `<h4>Not in the records we checked</h4>${look}${lacking.join("")}` : "")
    + fold(`More from the council record (${rest.length})`, rest.join("")) + notes(state, "notes", "Notes for this property", Object.values(UNDER).flat());
}

const FLAGS = {
  lvr_over_80: () => "The deposit is under 20% of the price (LVR above 80%). A low-equity margin or a First Home Loan may apply.",
  dti_over_6: r => `Total debt would be more than 6 times income, above the Reserve Bank's high-DTI threshold, and lenders restrict this kind of lending. For this income the threshold is a loan of ${money(r.high_dti_threshold)}.`,
  fhl_deposit_below_5: () => "The First Home Loan scenario is not shown because the deposit is under 5% of the price.",
  fhl_income_over_cap: r => `The First Home Loan scenario is not shown because the income is above its cap: ${money(r.fhl_income_caps.single)} for one applicant, ${money(r.fhl_income_caps.multiple_or_dependants)} for two or more applicants or one with dependants.`,
  fhl_household_type_unknown: () => "First Home Loan eligibility depends on the household. Choose it " + IN_DETAILS + ".",
};

const stressRow = (r, detail) => row(`If rates were ${r.stress_add_pp} percentage points higher`, detail);
const table = (head, rows) => `<div class="scroll"><table><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr>${rows.map(cells => `<tr>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table></div>`;
// The monthly figure of each scenario, which leads the section: [amount, what it is]
const MONTHLY = {bank_standard: s => [span(s.monthly_range, money), "a month at the banks' published 1-year rates"],
  first_home_loan: s => [span(s.monthly_range, money), "a month with a First Home Loan"],
  user_rate: s => [money(s.monthly), `a month at ${pct(s.rate_pct)}, the rate you entered`]};
// The details of each repayment are folded, a row that names what is inside: the lead figures say the monthly amounts.
const SCENARIOS = {
  bank_standard: (s, r) => fold(`Bank loan at published 1-year rates (${count(s.per_bank.length, "bank")})`, `
    <div class="note">Rates of ${span(s.rate_range_pct, pct)} across ${s.per_bank.length} banks${s.low_equity_margin_pct ? `, including a low-equity margin of ${span(s.low_equity_margin_pct, String)} percentage points where a bank charges one` : ""}. ${cite(s.source_ids)}</div>
    ${row("Repayment every two weeks", span(s.fortnightly_range, money))}
    ${stressRow(r, `${money(s.stress_monthly)} per month at the highest of these rates (${money(s.stress_delta_monthly)} more).`)}
    ${table(["Bank", "Rate", "Basis", "Collected"],
      s.per_bank.map(b => [esc(bank(b.bank_id)), pct(b.rate_pct), esc(BASIS[b.basis] || b.basis), esc((b.captured_at || "not recorded").slice(0, 10))])) + warnings({rules: r}, "loan", true)}`),
  // a lender with neither a rate nor a repayment is named on one line under the table, not as a row of "not published"
  first_home_loan: (s, r, blank = l => l.rate_pct == null && l.monthly == null) => (s.eligibility === "conditional" ? `<div class="problem">${esc(s.eligibility_note)}</div>` : "")
    + fold(`First Home Loan (${count(s.lenders.length, "lender")})`, `
    <div class="note">${esc(s.lmi_note)} ${cite(s.source_ids)}</div>
    ${row("Repayment every two weeks", span(s.fortnightly_range, money))}
    ${stressRow(r, `${span(s.stress_monthly_range, money)} per month.`)}
    ${row("Insurance premium at the full rate", money(s.lmi_amount) + (s.full_premium_assumed_for.length ? `. The full premium is assumed for ${s.full_premium_assumed_for.map(id => "the lender " + esc(bank(id))).join(", ")}.` : ""))}
    ${table(["Lender", "Rate", "Basis", "Premium passed on", "Monthly", "Every two weeks", `Monthly at +${r.stress_add_pp} points`],
      s.lenders.filter(l => !blank(l)).map(l => [esc(l.name), or(l.rate_pct, pct), esc(BASIS[l.rate_basis] || l.rate_basis), or(l.lmi_passthrough_pct, pct), or(l.monthly, money), or(l.fortnightly, money), or(l.stress_monthly, money)]))}
    ${s.lenders.some(blank) ? `<p class="muted">No published rate for: ${s.lenders.filter(blank).map(l => esc(l.name)).join(", ")}.</p>` : ""}`),
  user_rate: (s, r) => fold(`At the rate you entered (${pct(s.rate_pct)})`, `
    ${row("Repayment every two weeks", money(s.fortnightly))}
    ${stressRow(r, `${money(s.stress_monthly)} per month (${money(s.stress_delta_monthly)} more).`)}`),
};
// Notes that say what a term on a row means sit under that row.
const DEFINES = {"Capital value (CV)": "kb_cv_not_market", "Loan-to-value ratio (LVR)": "kb_lvr_definition", "Debt-to-income ratio (DTI)": "kb_dti_definition"};
const defined = (state, name) => { const k = (state.kb || []).find(k => k.id === DEFINES[name]); return k ? noteOf(state)(k) : ""; };

function loan(state) {
  const {ctx, rules: r, rulesError: err} = state;
  if (err) return `<div class="problem">The repayment figures could not be calculated: ${esc(err.message)}${list(err.fields.map(f => `${esc(words(f.field.replace("user_inputs.", "")))}: ${esc(f.message)}`))}</div>`;
  if (!r) return `<p class="muted">Calculating…</p>`;
  const price = r.price_used, rates = ctx.property.annual_rates, out = [];
  // The section leads with one repayment at the visitor's own rate, or else at the banks' rates, and the First Home Loan one beside it.
  const lead = r.scenarios.filter(s => MONTHLY[s.id] && !(s.id === "bank_standard" && r.scenarios.some(x => x.id === "user_rate")));
  out.push(`<div class="figs">${lead.map(s => MONTHLY[s.id](s)).map(([amount, what]) => `<p class="fig"><b>${amount}</b>${what}</p>`).join("")}</div>`,
    `<p class="edit"><a href="#details" target="_self">Change the price or your loan details</a></p>`);
  out.push(row("Price used", price.basis === "cv" ? `${money(price.value)}, the council valuation (CV) as at ${esc(price.valuation_date)}. Not a market value.`
    : price.basis === "user" ? `${money(price.value)}, your own figure.` : "No price to calculate with."));
  if (r.loan_amount) out.push(row("Loan amount", `${money(r.loan_amount)} (the price less a deposit of ${money(r.deposit)})`));
  if (r.loan_amount && r.lvr_pct != null) out.push(row("Loan-to-value ratio (LVR)", `${pct(r.lvr_pct)} of the price is borrowed${defined(state, "Loan-to-value ratio (LVR)")}`));
  // the note that no debts were entered is said here, on the line it changes, and nowhere else
  const noDebts = r.warnings.includes("existing_debts_assumed_zero") ? ` No existing debts were entered, so none are counted; enter them ${IN_DETAILS}, or 0 if you have none.` : "";
  if (r.loan_amount && r.dti != null) out.push(row("Debt-to-income ratio (DTI)", `${r.dti} times income (existing debts plus this loan, divided by income).${esc(noDebts)}${defined(state, "Debt-to-income ratio (DTI)")}`));
  out.push(...r.flags.map(f => `<div class="note">${esc(FLAGS[f.id] ? FLAGS[f.id](r) : "Note: " + words(f.id))} ${cite(f.source_ids)}</div>`));
  out.push(...r.scenarios.map(s => SCENARIOS[s.id] ? SCENARIOS[s.id](s, r) : `<h4>${esc(words(s.id))}</h4>`));
  if (r.scenarios.length) out.push(`<div class="muted">Repayments are principal and interest over ${r.loan_term_years} years. They do not include council rates, insurance, utilities or maintenance. The higher-rate figures are an illustration, not any bank's test rate.${r.rates_snapshot_date ? ` Bank rates were collected by hand from each bank's website; the oldest is from ${esc(r.rates_snapshot_date)}. ${cite("bank_rates_snapshot")}` : ""}</div>`);
  return out.join("") + warnings(state, "loan", state.rules.scenarios.some(s => s.id === "bank_standard") ? false : undefined) + notes(state, "loan", "Lending notes");
}

// A year built the records lack, given or found online, that selects no build-era note: said, so that adding it is not
// taken for having done nothing.
const eraless = ({ctx, rules, kb, inputs}) => { const y = inputs?.chat_supplied?.year_built;
  return rules && kb && !ctx.property.year_built.decade_range && YEAR.test(y) && !kb.some(k => k.topic === "build_era") ? y : null; };
const ERALESS = y => `No build-era note applies to the year built, ${y}.`;
// What stands in for the build-era and title notes under the main facts when they cannot all be shown.
function homeNotes(state) {
  const {rules: r} = state;
  if (!r) return state.rulesError ? `<p class="problem">The notes on the build era and title are unavailable until the repayment figures can be calculated.</p>` : "";
  return (r.kb_includes_draft ? `<p class="problem">Development mode: draft notes are shown. They have not yet been checked against their sources.</p>` : "")
    + (state.kb ? "" : `<p class="problem">The background notes could not be loaded.</p>`)
    + warnings(state, "notes");
}

// The sources a sentence from report-api cites: a source id is a PropertyContext source, a rules-api source or a
// knowledge-base item. A source cited through one of its items needs no second card.
function cardsOf(s, items) {
  const quoted = new Set(s.source_ids.map(id => items[id]?.source_id));
  return s.source_ids.filter(id => !quoted.has(id)).map(id => items[id] ? kbCard(items[id]) : card(id));
}
// A sentence that passed the check against its sources is highlighted. A fixed sentence from the program cites none.
// One that states a value a web search found (`web`) is not: its wording was checked, the value was not.
function checked(s, items, after) {
  const cards = cardsOf(s, items), n = cards.length, hl = n && !s.web;
  return `<p class="s">${hl ? `<span class="hl">${esc(s.text)}</span>` : esc(s.text)}</p>${control(cards, s.web ? `Cites ${count(n, "source")}` : `Checked against ${count(n, "source")}`, after)}`;
}
// The section that holds the records behind a sentence, named by its first source: a note's topic, a hazard layer, a property record, or else the lending sources.
const SECTION = {hazards: "the mapped hazards", facts: "this home's records", loan: "the repayment figures"};
function jump(s, items, ctx) {
  const id = s.source_ids[0], item = items[id];
  const to = item ? TOPIC_BLOCK[item.topic] || "facts" : ctx.hazards.some(h => h.source_id === id) ? "hazards" : ctx.sources.some(x => x.id === id) ? "facts" : "loan";
  return `<a class="jump" href="#sec-${to}" target="_self">See ${SECTION[to]}</a>`;
}

// How far the report has got: `at` is the step under way, or the one that failed. A step ends when the page sees its
// request finish: the records, with the figures calculated from them a moment later, then report-api's one call, in which
// the model words the summary and each sentence is checked against its sources. The step under way says what is being
// done, in the place of its name and with the name's own words, so the eye follows one word through a state that often
// lasts a moment only. Both are short enough for the two steps to share a line on a phone.
const STEPS = [["Records", "Reading the records…"], ["Summary and check", "Writing and checking the summary…"]];
const steps = (at, failed) => `<ol class="steps" aria-label="How far this page has got">${STEPS.map(([name, doing], i) => {
  const said = i < at ? "done" : i > at ? "waiting" : failed ? "failed" : "";
  return said ? `<li class="${i > at ? "" : said}">${name}<span class="sr">: ${said}</span></li>` : `<li class="doing">${doing}</li>`; }).join("")}</ol>`;

// What is said when the summary or an answer did not come: asked too often (web) or too many at once (report-api) is
// said as such, and nothing is asked again until the reader chooses to.
const unavailable = (what, err) => err.code === "rate_limited" || err.code === "busy" ? "Too many requests at the moment. Wait a few seconds, then try again."
  : `The ${what} is unavailable right now. The rest of the page is not affected.`;
// The council valuation, the monthly repayments and, when the loan is above it, the high-DTI threshold for this income,
// written by the program from the records and the calculation: plain text, never highlighted, each with its sources.
function keyFigures(ctx, r) {
  const cv = ctx.property.cv, sc = id => r.scenarios.find(s => s.id === id), own = sc("user_rate"), bank = sc("bank_standard"), fhl = sc("first_home_loan");
  const pay = [own ? [money(own.monthly), `a month at ${pct(own.rate_pct)}, the rate you entered`, own] : bank && [span(bank.monthly_range, money), "a month at the banks' published 1-year rates", bank],
    fhl && [span(fhl.monthly_range, money), "a month with a First Home Loan", fhl]].filter(Boolean);
  const dti = r.flags.find(f => f.id === "dti_over_6");
  const lines = [cv.status === "found" && `<li>Council valuation ${money(cv.value)}, which is not a market value. ${cite(cv.source_id)}</li>`,
    ...pay.map(([amount, what, s]) => `<li>${amount} ${what}. ${cite(s.source_ids)}</li>`),
    dti && r.high_dti_threshold != null && `<li>A loan of ${money(r.high_dti_threshold)}: above it, total debt is more than 6 times this income, the Reserve Bank's high-DTI threshold. ${cite(dti.source_ids)}</li>`].filter(Boolean);
  return lines.length ? `<ul class="keyfigs">${lines.join("")}</ul>` : "";
}

// report-api only returns sentences that passed its check against the sources they cite.
function summary({ctx, rules, rulesError, kb, report: r, reportError: err, done, first}) {
  if (!rules) return rulesError ? steps(0, true) + `<p class="problem">The summary is unavailable until the repayment figures can be calculated.</p>`
    : steps(0);
  if (err) return steps(1, true) + (err.code === "validation_failed"
    ? `<p class="problem">No summary sentence passed the check against its sources, so none is shown. The rest of the page is not affected.</p>`
    : `<p class="problem">${unavailable("summary", err)}</p><button type="button" id="retry-summary">Try again</button>`);
  if (!r) return steps(1);
  const items = byId(kb || []), drafts = r.model === "code-draft";  // report-api wrote every sentence itself: no model call could be made
  return r.summary.map(s => checked(s, items, jump(s, items, ctx))).join("") + keyFigures(ctx, rules)  // both steps done: the line has said what it had to
    + (drafts ? `<p class="muted">The model was not used for this summary. These sentences are the program's own, written from the same facts.</p>` : "")
    + `<details class="how"${first ? " open" : ""}><summary>How this summary is made</summary><ol>
      <li>The program collects the facts from council and LINZ records, your loan figures and the notes that apply.</li>
      <li>Gemini, Google's AI model, rewrites them as three to five plain sentences. It never sees a figure: the program fills those in.</li>
      <li>Each sentence is checked against the sources it cites. One that fails is replaced by the program's own wording.</li></ol>
      <p class="muted">The check compares wording and figures. It cannot judge everything a sentence means, so open the sources of any sentence that matters to you.</p></details>`
    + (r.next_steps.length ? `<h3>Next steps</h3>${fold(`<span>${count(r.next_steps.length, "thing")} to ask or check<span id="ticked">${ticked(r.next_steps.filter(s => done.has(s.text)).length)}</span></span>`,
        `<ul class="todo">${r.next_steps.map(s => `<li><label><input type="checkbox"${done.has(s.text) ? " checked" : ""}><span>${esc(s.text)}</span></label>${control(cardsOf(s, items))}</li>`).join("")}</ul>`, true)}` : "")  // open: what to do about each sentence
    + notFound(r.not_found_notes);
}
export const ticked = n => n ? `, ${n} ticked` : "";  // said on the folded list's line
// Before the records have arrived, the sheet has the address that was chosen and the first step.
// The readout keeps its two lines meanwhile, so the sheet under it does not move when the report arrives: the loan details
// as typed, and nothing yet about a home whose records are still being read.
export function renderWait(address, failed, typed) {
  $("matched").textContent = address; $("meta").textContent = "";
  $("readout").innerHTML = `${loanLine(typed)}<br><b>This home</b> …`;
  $("summary").innerHTML = steps(0, failed) + (failed ? `<p class="problem">The council and LINZ records could not be read. Choose the address again to retry.</p>` : "");
}
// What the records lack is named in one line, with report-api's sentence for each behind it. A hazard layer that could
// not be checked keeps its sentence, and the caveat in it, in view.
function notFound(notes) {
  const layer = n => (n.field || "").startsWith("hazards."), fields = notes.filter(n => !layer(n));
  const names = fields.flatMap(n => n.labels || []);
  return !notes.length ? "" : `<h3>Could not be found</h3><div class="gap">${list(notes.filter(layer).map(n => esc(n.text)))}
    ${fold(`Not in the records: ${esc(names.join(", ") || count(fields.length, "note"))}`, list(fields.map(n => esc(n.text))))}</div>`;
}

const LINK_KIND = {direct: " (opens this property)", first_result: " (first search result)", home: " (search for the address there)"};
const linkItem = l => `${link(l.url, l.label)}${LINK_KIND[l.kind] || ""}`;

// An answer Gemini found with Google Search, for a question the report does not cover or a field the records lack. The Gemini API terms ask for
// it to be shown as returned, with Google's search suggestions: the text is plain text, the suggestions are Google's
// own HTML and sit in a frame that runs no scripts.
// The language of the text is not known here, so it does not inherit the page's.
// The frame's document accepts both colour schemes: one that only knew light would be given an opaque white ground on a dark page.
// Google's part stays together and in Google's order: the text, the pages it rests on, the search suggestions. Ours comes
// after it, in one small paragraph: what this is, and `own`, the official pages report-api names for checking it.
const webAnswer = (w, own) => `<div class="web">
  <div class="say" lang="">${w.text.split(/\n\s*\n/).map(p => `<p>${esc(p)}</p>`).join("")}</div>
  ${w.sources.length ? `<p class="muted">Sources: ${w.sources.map(s => link(s.url, s.title || "Source")).join(" &middot; ")}</p>` : ""}
  ${w.search_suggestions ? `<iframe class="suggest" title="Google Search suggestions" sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc="${esc('<meta name="color-scheme" content="light dark"><base target="_blank">' + w.search_suggestions)}"></iframe>` : ""}
  <p class="muted">Found by Gemini with Google Search. Not checked by this tool.${own.length ? ` Check it yourself: ${own.join("; ")}.` : ""}</p></div>`;

// While an answer is on its way: three dots and a few words that take turns. The words say that work is going on, not
// which part of it: the page sends one request and cannot see inside it. A screen reader hears "Answering" once.
const THINKING = `<p class="thinking muted"><span class="sr">Answering</span><span aria-hidden="true"><i></i><i></i><i></i></span><span class="verbs" aria-hidden="true">${
  ["Thinking", "Reading the records", "Looking it up", "Putting it together", "Still working on it"].map(v => `<span>${v}</span>`).join("")}</span></p>`;

// The conversation about this report, in order. A turn holds the question and, once settled, report-api's answer with
// the knowledge-base items on the page at that time, or the error. The sentences carry the language the answer names.
// The links an answer names are PropertyContext links; an id the report does not have is left out.
// What an answer put into the form, by where it came from. A value from a web search can be taken out again.
function addedLine(added, web) {
  const line = (mine, removed) => !mine.length ? "" : `<p class="muted rec">${removed ? "Taken out of your details again" : `Added to your details from ${web ? "a web search, not checked" : "your question"}`}: ${
    esc(mine.map(a => a.text).join("; "))}.${web && !removed ? ` <button type="button" data-remove="${mine.map(a => a.field).join(",")}">Remove</button>` : ""}</p>`;
  const mine = (added || []).filter(a => a.web === web);
  return line(mine.filter(a => !a.removed), false) + line(mine.filter(a => a.removed), true);
}
function thread({ctx, rules, rulesError, kb: onPage, thread: turns}) {
  // in the conversation a link's label loses its own bracketed description: the line already says what the link opens
  const linked = a => (a.links || []).map(id => ctx.links.find(l => l.id === id)).filter(Boolean)
    .map(l => linkItem({...l, label: l.label.replace(/\s*\([^)]*\)$/, "")}));
  const answer = ({answer: a, error: err, added, kb}, items = byId(kb || onPage || [])) => err ? (err.code === "validation_failed"
      ? `<p class="problem">No answer sentence passed the check against its sources, so none is shown. Try asking in a different way.</p>`
      : `<p class="problem">${unavailable("answer", err)}</p><button type="button" id="retry-answer">Try again</button>`)
    : !a ? THINKING
    // a search answer stays whole, with its sources and its caveat; what the program says about it comes after
    : (a.web ? webAnswer(a.web, linked(a)) : "")
      + (a.answer.length ? `<div class="say"${a.language ? ` lang="${esc(a.language)}"` : ""}>${a.answer.map(s => checked(s, items)).join("")}</div>` : "")
      + (a.web ? "" : list(linked(a)))
      + ((a.warnings || []).includes("generation_degraded") ? `<p class="muted rec">The model was not used for this answer. These sentences are the program's own.</p>` : "")
      // a search that ran and found nothing is said plainly; only one that could not run is worth trying again
      + ((a.warnings || []).includes("search_unavailable") ? `<p class="muted rec">${a.search_outcome === "no_result"
        ? "A web search found no answer for this address." : "A web search could not be made just now. You can ask again later."}</p>` : "")
      + addedLine(added, false) + addedLine(added, true);
  return turns.map(t => `<div class="msg you">${esc(t.question)}</div><div class="msg">${answer(t)}</div>`).join("")
    + (!rules && rulesError ? `<p class="problem">Questions are unavailable until the repayment figures can be calculated.</p>` : "");
}

// a block that cannot be drawn must not take the others with it
function draw(id, block, state) {
  try { $(id).innerHTML = block(state); }
  catch (e) { console.error(e); $(id).innerHTML = `<p class="problem">This section could not be shown.</p>`; }
}

function listSources({ctx}) {
  $("sources").innerHTML = cited.map(credit).join("");
  $("d-originals").textContent = `${ctx.links.length} sites to look at yourself, and the ${cited.length} sources cited on this page with their licences.`;
}

// The summary and the answers arrive after the rest and are drawn on their own: redrawing the other blocks would close the sources a reader has opened.
// A next step is said once: not under a note when the summary's list has it, and only under the first note that has it.
function drawSummary(state) {
  draw("summary", summary, state);
  const said = new Set((state.report?.next_steps || []).map(s => s.text));
  for (const el of document.querySelectorAll("[data-step]")) { el.hidden = said.has(el.dataset.step); said.add(el.dataset.step); }
  listSources(state);
}
// While the reader has the address at the top of the sheet in view, the browser must not hold a record or the footer in
// place as the report is drawn above it: in Chrome that scrolled the address and the summary out of view. A reader who has
// scrolled on is left to the browser, which keeps what they are reading where it is.
function held(drawing) {
  const {top} = $("matched").getBoundingClientRect(), hold = top > -1 && top < innerHeight;
  if (hold) document.body.style.overflowAnchor = "none";
  drawing();
  if (hold) { void document.body.offsetHeight; document.body.style.overflowAnchor = ""; }  // laid out before the browser may hold anything again
}
export const renderSummary = state => held(() => drawSummary(state));

// One question at a time, and only about a report that has its figures.
export function renderThread(state) {
  draw("thread", thread, state);
  $("ask").disabled = !state.inputs || state.thread.some(t => !t.answer && !t.error);
  $("starters").hidden = $("ask-note").hidden = state.thread.length > 0;  // both are for a reader who has not asked yet
  // Each question to start with is one a note on every report answers; the last asks about this home's own title type,
  // and is offered only when the records give the type and a note about it is on the page.
  const title = state.ctx?.property.title_type.value;
  $("starter-title").hidden = !(title && (state.kb || []).some(k => k.topic === "title_type"));
  $("starter-title").textContent = `What does a ${String(title).toLowerCase()} title mean?`;
  // the phone's handle shows once there is something to show or hide: an answer, or the panel open
  $("handle-count").textContent = state.thread.length || ""; $("handle").hidden = !state.thread.length && !$("ask-panel").classList.contains("open");
  $("thread").scrollTop = $("thread").scrollHeight;  // the newest turn is in view
  listSources(state);
}

// Shows the words of the newest answer the way they would be typed. Only the display is paced: the answer has arrived
// whole, because its sentences are checked, or its sources attached, before any of it is shown, and its sources and
// links are on the page from the first word. A reader who asked for less motion gets the words at once, and a screen
// reader hears them once, when they are complete.
export function typeAnswer() {
  const box = $("thread"), el = [...box.querySelectorAll(".msg:not(.you) .say")].at(-1);
  if (!el || el.closest(".web") || document.hidden || matchMedia("(prefers-reduced-motion: reduce)").matches) return;  // a search answer is shown as returned, all at once
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
const HOUSEHOLD = {single: "one applicant", single_with_dependants: "one applicant with dependants", multiple: "two or more applicants"};
const loanLine = i => `<b>Your loan</b> ${esc([...[[i.deposit, "deposit"], [i.household_income, "income"], [i.existing_debts, "debts"]]
  .filter(([v]) => v != null).map(([v, word]) => `${money(v)} ${word}`), HOUSEHOLD[i.household_type]].filter(Boolean).join(", ") || "no details yet")}`;
function readout({rules: r, inputs: i}) {
  const price = r.price_used;
  const used = (field, ok = true) => ok && !r.warnings.includes(field + "_user_conflict") && i.chat_supplied[field], told = {year_built: used("year_built", YEAR.test(i.chat_supplied.year_built)), cladding: used("cladding")};
  const home = [price.value != null && `${money(price.value)} price${price.basis === "cv" ? " (the council valuation)" : ""}`,
    told.year_built && `built ${told.year_built}, ${how(i, "year_built")}`, told.cladding && `${words(told.cladding)} cladding, ${how(i, "cladding")}`].filter(Boolean);
  return `${loanLine(i)}<br><b>This home</b> ${esc(home.join(", ") || "no price yet")}`;
}

// One line under each section title, from values the section shows with their sources.
function digests(state) {
  const {ctx, rules: r, rulesError, inputs} = state;
  const p = ctx.property, flood = ctx.hazards.filter(h => h.priority === "primary"), mapped = floodMapped(ctx).map(h => layerName(h.layer));
  const missed = ctx.hazards.filter(unchecked).length, tail = missed ? ` ${count(missed, "layer")} could not be checked.` : "";
  const scenario = id => r?.scenarios.find(s => s.id === id), bankLoan = scenario("bank_standard"), fhl = scenario("first_home_loan"), own = scenario("user_rate");
  const decade = p.year_built.decade_range, told = YEAR.test(inputs?.chat_supplied.year_built) && inputs.chat_supplied.year_built, title = p.title_type.value && `${p.title_type.value} title.`;
  return {
    hazards: mapped.length ? `Mapped ${where(ctx)}: ${mapped.join(", ")}.` + tail
      : flood.length && flood.every(h => h.status === "none") ? `No flood layer is mapped ${where(ctx)}. ${NO_RISK_CAVEAT}` + tail
      : `The flood layers could not all be checked. ${NO_RISK_CAVEAT}`,
    facts: [p.cv.status === "found" && `Council valuation ${money(p.cv.value)}.`, title,
      decade ? `Built ${decade[0] ? `${decade[0]}-${decade[1]}` : `before ${decade[1] + 1}`}.` : told ? `Built ${told}, ${how(inputs, "year_built")}.` : "Year built not known."].filter(Boolean).join(" "),
    loan: rulesError ? "The repayment figures could not be calculated." : !r ? "Calculating…"
      : [bankLoan && `${span(bankLoan.monthly_range, money)} a month at bank rates.`, fhl && `${span(fhl.monthly_range, money)} a month with a First Home Loan.`,
        own && `${money(own.monthly)} a month at your rate.`].filter((x, i) => x && !(i === 0 && own)).join(" ") || "No repayments are shown yet.",
  };
}

export function render(state) {
  held(() => {
  const {ctx} = state;
  sources = state.sources; banks = state.banks; cited = []; noted = {};
  // while the figures are calculated the readout keeps the loan details as typed (renderWait)
  if (state.inputs || state.rulesError) draw("readout", () => state.inputs ? readout(state) : "<b>Your details</b>");
  try { for (const [id, text] of Object.entries(digests(state))) $("d-" + id).textContent = text; } catch (e) { console.error(e); }
  $("matched").textContent = ctx.address.matched || "Address not found";
  $("meta").textContent = ctx.address.status !== "found" ? gone(ctx.address)
    // when the records were read, in New Zealand time wherever the page is opened; how the hazard layers were checked is said in their section
    : `Records read ${new Date(ctx.generated_at).toLocaleString("en-NZ", {timeZone: "Pacific/Auckland", day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit"})} (New Zealand time).`;
  draw("hazards", hazards, state); draw("facts", facts, state); draw("loan", loan, state);
  draw("links", () => ctx.links.map(l => `<li>${linkItem(l)}</li>`).join(""));
  drawSummary(state); renderThread(state);
  });
}
