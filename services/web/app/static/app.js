// Page flow: address search -> PropertyContext and map -> rules and knowledge-base notes -> render -> summary -> questions.
// Each request fails on its own: a failed call only affects the block it feeds.
import {$, byId, esc, floodMapped, link, render, renderSummary, renderThread, typeAnswer} from "./report.js";
import {drawMap} from "./map.js";

// kb: null means the notes could not be loaded; inputs: what the rules on the page were calculated with
const CLEARED = {rules: null, rulesError: null, inputs: null, kb: [], report: null, reportError: null};
const state = {ctx: null, ...CLEARED, sources: {}, banks: {}, thread: [], done: new Set()};  // done: the next steps ticked for this address
let timer, lastTyped = "", searchSeq = 0, lookupSeq = 0, calcSeq = 0, rulesSources, kbIds, kbItems;
let asked = {};  // the summary last asked for: its key and the pending or settled answer

const show = id => { $(id).scrollIntoView(); $(id).focus({preventScroll: true}); };
// The form is open until the first report is on the page; after that it is folded behind "Change" and the readout says what was used.
function details(open) {
  $("details").hidden = !open; $("change").textContent = open ? "Close" : "Change"; $("change").setAttribute("aria-expanded", open);
}

// What a council property category says about the build date, to show the visitor how it was read
// (LINZ Rating Valuations Rules 2008, Table 21). The web service does the same reading before it asks rules-api.
const categoryBuilt = text => {
  const m = /^(?:R[ACDFHNR]|LI)(\d{3}|XXX)[ABC]?$/i.exec(text.trim()), code = m && m[1].toUpperCase();
  return !m ? "" : code === "XXX" ? "in more than one period" : code === "191" ? "before 1914" : code === "192" ? "between 1914 and 1929"
    : code >= "193" && code <= "209" ? `between ${code}0 and ${code}9` : "";
};

async function api(path, body) {
  const res = await fetch("/api/" + path, body && {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await res.json().catch(() => null);
  if (!res.ok) throw Object.assign(new Error(data?.error?.message || "HTTP " + res.status), {code: data?.error?.code, fields: data?.error?.fields || []});
  return data;
}

// One summary request for a lookup and a set of inputs: asking again with nothing changed reuses it, in flight or
// answered. A failed one is asked again, which is what "Try again" does.
async function summarise() {
  const mine = calcSeq, {inputs} = state, address_id = state.ctx.address.linz_address_id, key = JSON.stringify([address_id, state.ctx.generated_at, inputs]);
  if (key !== asked.key) asked = {key, answer: api("report", {address_id, user_inputs: inputs})};
  const {answer} = asked;
  renderSummary(Object.assign(state, {report: null, reportError: null}));
  let update;
  try { update = {report: await answer}; }
  catch (e) { update = {reportError: e}; if (asked.answer === answer) asked = {}; }
  if (mine === calcSeq) renderSummary(Object.assign(state, update));
}

async function calculate() {
  const mine = ++calcSeq, cv = state.ctx.property.cv, rules = inputs => api("rules", {property_context: state.ctx, user_inputs: inputs});
  const num = id => $(id).value.trim() === "" ? null : Number($(id).value);
  const chat_supplied = {year_built: $("year-built").value.trim() || null, cladding: $("cladding").value || null};
  const built = categoryBuilt($("year-built").value);
  $("year-built-read").textContent = built && `Read as a council property category: built ${built}.`;
  const price = num("price");
  const update = {...CLEARED};
  let inputs = {price_override: cv.status === "found" && price === cv.value ? null : price, deposit: num("deposit"),
    household_income: num("income"), existing_debts: num("debts"), rate_pct: num("rate"), household_type: $("household").value || null, chat_supplied};
  try {
    update.rules = await rules(inputs);
  } catch (e) {
    update.rulesError = Object.assign(e, {fields: e.fields || []});
    // rejected loan inputs (a negative amount, say) must not blank the property notes: ask again without them
    if (e.fields.length) update.rules = await rules(inputs = {chat_supplied}).catch(() => null);
  }
  if (update.rules) {
    const ids = update.rules.kb_items.join(",");
    const [kb, extra] = await Promise.all([
      !ids ? [] : ids === kbIds ? kbItems : api("kb/items?ids=" + encodeURIComponent(ids)).catch(() => null),
      (rulesSources ??= api("sources").then(byId)).catch(() => (rulesSources = null, {}))]);
    if (kb) [kbIds, kbItems] = [ids, kb];
    Object.assign(update, {kb, inputs, sources: {...extra, ...byId(state.ctx.sources)}});
  }
  if (mine !== calcSeq) return;
  render(Object.assign(state, update));
  if (update.rules) summarise();
}

async function lookup(a) {
  const mine = ++lookupSeq;
  clearTimeout(timer); ++searchSeq;  // drop any address search still pending
  $("suggestions").innerHTML = ""; $("q").value = a.full_address; $("report").hidden = true;
  $("status").textContent = "Looking up council and LINZ data…";
  try {
    const ctx = await api(`property-context?address_id=${a.address_id}&input=${encodeURIComponent(lastTyped)}`);
    if (mine !== lookupSeq) return;  // a newer lookup owns the page
    if (state.ctx && state.ctx.address.linz_address_id !== a.address_id) $("year-built").value = $("cladding").value = "";  // facts about the previous home
    Object.assign(state, CLEARED, {ctx, sources: byId(ctx.sources), thread: [], done: new Set()});  // earlier answers were about another report
    const cv = ctx.property.cv, site = id => { const l = ctx.links.find(x => x.id === id); return l ? link(l.url, l.label) : id; };
    $("price").value = cv.status === "found" ? cv.value : "";
    $("price-hint").innerHTML = (cv.status === "found" ? `Filled in with the council valuation (CV) as at ${esc(cv.valuation_date)}, which is not a market value. `
      : "The council valuation could not be found, so enter a price. ") + `Check a current estimate on ${site("homes")}, ${site("oneroof")} or the listing, then change this figure.`;
    $("year-built-hint").innerHTML = `Only used when the council records do not give a build decade. Check the listing, ${site("homes")} or ${site("oneroof")} for the decade, or copy the Property Category from the ${site("council_valuation")}. A year (1997), a decade (1990s) or the category code (for example RD199B) all work.`;
    $("cladding-hint").innerHTML = `Check the listing, ${site("homes")} or ${site("oneroof")} for it.`;
    $("status").textContent = "The report is below."; $("report").hidden = $("readout-row").hidden = false;
    $("intro").hidden = $("start-note").hidden = true; details(false);
    $("sec-hazards").open = floodMapped(ctx).length > 0;  // the map is on show when a flood layer is mapped here
    render(state);
    show("matched");
    drawMap(() => api("map?address_id=" + a.address_id), () => mine === lookupSeq);
    calculate();
  } catch (e) { if (mine === lookupSeq) { state.ctx = null; $("status").textContent = "Lookup failed: " + e.message; } }
}

// Loan details the visitor typed stay for this tab (sessionStorage), so they are entered once and used for every
// address checked in it. Only fields inside the form reach this listener: the price and the two facts about the home
// belong to one address. Storage can be switched off in the browser; the page works without it.
try { for (const field of $("loan-form").elements) field.value = sessionStorage.getItem("loan." + field.id) || field.value; } catch {}
$("loan-form").addEventListener("change", e => { try { sessionStorage.setItem("loan." + e.target.id, e.target.value); } catch {} });
$("summary").addEventListener("click", e => { if (e.target.id === "retry-summary") summarise(); });
// a tick survives the summary being written again for the same address
$("summary").addEventListener("change", e => { if (e.target.matches(".todo input")) state.done[e.target.checked ? "add" : "delete"](e.target.closest("label").textContent); });

// A year built or cladding stated in a question goes into the form when the report on the page was not calculated with
// it, unless the records already have that fact: the rules then use the record, and report-api's answer says how the
// two compare. Returns what was put into the form, in words.
function fill({year_built, cladding}) {
  const added = [], option = [...$("cladding").options].find(o => o.value === cladding), p = state.ctx.property;
  const recorded = f => f.status === "found" && f.value != null, used = state.inputs?.chat_supplied || {};
  if (year_built != null && !recorded(p.year_built) && String(used.year_built ?? "") !== String(year_built)) { $("year-built").value = year_built; added.push("year built " + year_built); }
  if (option && !recorded(p.cladding) && used.cladding !== cladding) { $("cladding").value = cladding; added.push("cladding " + option.text); }
  return added;
}

// One question about the report on the page, sent with the last 3 answered turns. A turn that failed is replaced by the
// next one asked. When the answer adds a fact about the home to the form, the report is calculated again with it first,
// so the notes the answer cites are on the page; the turn keeps those notes, whatever the report is changed to later.
async function ask(question) {
  if ($("ask").disabled) return;
  const turn = {question}, answered = state.thread.filter(t => t.answer);
  const said = a => (a.web?.text || a.answer.map(s => s.text).join(" ")).slice(0, 2000);
  const history = answered.slice(-3).flatMap(t => [{role: "user", text: t.question}, {role: "assistant", text: said(t.answer)}]);
  state.thread = [...answered, turn];
  renderThread(state);
  try {
    const answer = await api("followup", {address_id: state.ctx.address.linz_address_id, user_inputs: state.inputs, question, history});
    if (!Array.isArray(answer?.answer)) throw new Error("not an answer");
    turn.answer = answer;
  } catch (e) { turn.error = e; }
  if (!state.thread.includes(turn)) return;  // another address owns the page
  if (turn.answer) {
    if ($("question").value.trim() === question) $("question").value = "";
    turn.added = fill(turn.answer.user_supplied || {});
    if (turn.added.length) await calculate();
    turn.kb = state.kb;
  }
  renderThread(state);
  if (turn.answer) typeAnswer();
}
$("ask-form").addEventListener("submit", e => {
  e.preventDefault();
  const question = $("question").value.trim();
  if (question) ask(question);
});
$("thread").addEventListener("click", e => { if (e.target.id === "retry-answer") ask(state.thread.at(-1).question); });

$("loan-form").addEventListener("submit", e => {
  e.preventDefault();
  if (!state.ctx) { $("status").textContent = "Choose an address first."; $("q").focus(); return; }
  calculate();
  details(false); show("matched");
});
$("change").addEventListener("click", () => details($("details").hidden));
// The theme follows the system until the visitor picks the other one; picking the system's own goes back to following it.
$("theme").addEventListener("click", () => {
  const root = document.documentElement, system = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const next = (root.dataset.theme || system) === "dark" ? "light" : "dark";
  if (next === system) delete root.dataset.theme; else root.dataset.theme = next;
  try { next === system ? localStorage.removeItem("theme") : localStorage.setItem("theme", next); } catch {}
});
$("edit-details").addEventListener("click", e => { e.preventDefault(); details(true); show("details"); });

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
