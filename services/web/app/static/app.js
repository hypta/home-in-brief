// Page flow: address search -> PropertyContext and map -> rules and knowledge-base notes -> render.
// Each request fails on its own: a failed call only affects the block it feeds.
import {$, esc, link, render} from "./report.js";
import {drawMap} from "./map.js";

const CLEARED = {rules: null, rulesError: null, kb: []};  // kb: null means the notes could not be loaded
const state = {ctx: null, ...CLEARED, sources: {}, banks: {}};
const byId = items => Object.fromEntries(items.map(s => [s.id, s]));
let timer, lastTyped = "", searchSeq = 0, lookupSeq = 0, calcSeq = 0, rulesSources, kbIds, kbItems;

const show = id => { $(id).scrollIntoView(); $(id).focus({preventScroll: true}); };

async function api(path, body) {
  const res = await fetch("/api/" + path, body && {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await res.json().catch(() => null);
  if (!res.ok) throw Object.assign(new Error(data?.error?.message || "HTTP " + res.status), {fields: data?.error?.fields || []});
  return data;
}

async function calculate() {
  const mine = ++calcSeq, cv = state.ctx.property.cv, rules = inputs => api("rules", {property_context: state.ctx, user_inputs: inputs});
  const num = id => $(id).value.trim() === "" ? null : Number($(id).value);
  const price = num("price");
  const update = {...CLEARED};
  try {
    update.rules = await rules({price_override: cv.status === "found" && price === cv.value ? null : price, deposit: num("deposit"),
      household_income: num("income"), existing_debts: num("debts"), rate_pct: num("rate"), household_type: $("household").value || null});
  } catch (e) {
    update.rulesError = Object.assign(e, {fields: e.fields || []});
    // rejected loan inputs must not blank the property notes: ask again without them
    if (e.fields.length) update.rules = await rules({}).catch(() => null);
  }
  if (update.rules) {
    const ids = update.rules.kb_items.join(",");
    const [kb, extra] = await Promise.all([
      !ids ? [] : ids === kbIds ? kbItems : api("kb/items?ids=" + encodeURIComponent(ids)).catch(() => null),
      (rulesSources ??= api("sources").then(byId)).catch(() => (rulesSources = null, {}))]);
    if (kb) [kbIds, kbItems] = [ids, kb];
    Object.assign(update, {kb, sources: {...extra, ...byId(state.ctx.sources)}});
  }
  if (mine !== calcSeq) return;
  render(Object.assign(state, update));
}

async function lookup(a) {
  const mine = ++lookupSeq;
  clearTimeout(timer); ++searchSeq;  // drop any address search still pending
  $("suggestions").innerHTML = ""; $("q").value = a.full_address; $("report").hidden = true;
  $("status").textContent = "Looking up council and LINZ data…";
  const mapRequest = api("map?address_id=" + a.address_id);
  mapRequest.catch(() => {});
  try {
    const ctx = await api(`property-context?address_id=${a.address_id}&input=${encodeURIComponent(lastTyped)}`);
    if (mine !== lookupSeq) return;  // a newer lookup owns the page
    Object.assign(state, CLEARED, {ctx, sources: byId(ctx.sources)});
    const cv = ctx.property.cv, site = id => { const l = ctx.links.find(x => x.id === id); return l ? link(l.url, l.label) : id; };
    $("price").value = cv.status === "found" ? cv.value : "";
    $("price-hint").innerHTML = (cv.status === "found" ? `Filled in with the council valuation (CV) as at ${esc(cv.valuation_date)}, which is not a market value. `
      : "The council valuation could not be found, so enter a price. ") + `Check a current estimate on ${site("homes")}, ${site("oneroof")} or the listing, then change this figure.`;
    $("status").textContent = "The report is below."; $("report").hidden = false;
    render(state);
    show("matched");
    drawMap(mapRequest, () => mine === lookupSeq);
    calculate();
  } catch (e) { if (mine === lookupSeq) { state.ctx = null; $("status").textContent = "Lookup failed: " + e.message; } }
}

$("loan-form").addEventListener("submit", e => {
  e.preventDefault();
  if (!state.ctx) { $("status").textContent = "Choose an address first."; $("q").focus(); return; }
  calculate();
  show("h-loan");
});

// Bank names, and the default interest rate: the middle of the banks' published 1-year standard rates.
api("rates/latest").then(snap => {
  state.banks = Object.fromEntries(snap.banks.map(b => [b.bank_id, b.name]));
  if (state.ctx) render(state);
  const quoted = snap.banks.filter(b => b.standard?.["1y"] != null), rates = quoted.map(b => b.standard["1y"]).sort((a, b) => a - b);
  if (!rates.length || $("rate").value) return;
  const mid = rates.length >> 1, median = Math.round((rates.length % 2 ? rates[mid] : (rates[mid - 1] + rates[mid]) / 2) * 100) / 100;
  const oldest = quoted.map(b => (b.captured_at || "").slice(0, 10)).filter(Boolean).sort()[0];
  $("rate").value = median;
  $("rate-hint").textContent = `Filled in with ${median}%, the middle of ${rates.length} banks' published 1-year standard rates (collected by hand${oldest ? "; the oldest is from " + oldest : ""}). Change it to a rate you have been offered.`;
}).catch(() => {});  // without it the rate stays blank and banks show by id

$("q").addEventListener("input", () => {
  clearTimeout(timer);
  const q = $("q").value.trim();
  if (q.length < 3) { ++searchSeq; $("suggestions").innerHTML = ""; return; }
  timer = setTimeout(async () => {
    const mine = ++searchSeq; lastTyped = q; $("status").textContent = "Searching addresses…";
    try {
      const found = await api("addresses?q=" + encodeURIComponent(q));
      if (mine !== searchSeq) return;
      $("status").textContent = found.length ? "" : "No Auckland address starts with that text.";
      $("suggestions").innerHTML = "";
      for (const a of found) {
        const li = document.createElement("li"), b = document.createElement("button");
        b.type = "button"; b.textContent = a.full_address; b.onclick = () => lookup(a);
        li.append(b); $("suggestions").append(li);
      }
    } catch (e) { if (mine === searchSeq) $("status").textContent = "Address search failed: " + e.message; }
  }, 400);
});
