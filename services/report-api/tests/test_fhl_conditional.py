"""F05: a conditional First Home Loan (income between the single and the multiple/dependants caps, household type
not entered) states its condition; eligible and over-cap keep their own wording."""
import copy

from b_common.report_schemas import ReportRequest, Sentence
from conftest import body

from app import kb, prompt
from app import validator as V
from app.validator import build_evidence


def fhl_fact(b):
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    ev = build_evidence(req, data.items, data.sources.keys())
    fs = prompt.fact_list(req, ev, data.items)
    return next(f for f in fs if "First Home Loan" in f.text and f.topic == "first_home_loan"), ev


def case1(eligibility="eligible", household="multiple", income=130000, over_cap=False):
    b = copy.deepcopy(body("case1"))
    b["user_inputs"]["household_income"] = income
    if household is None:
        b["user_inputs"].pop("household_type", None)
    else:
        b["user_inputs"]["household_type"] = household
    ro = b["rules_output"]
    if over_cap:
        ro["scenarios"] = [s for s in ro["scenarios"] if s["id"] != "first_home_loan"]
        ro["flags"].append({"id": "fhl_income_over_cap", "source_ids": ["kainga_ora_fhl_brochure"]})
    else:
        fhl = next(s for s in ro["scenarios"] if s["id"] == "first_home_loan")
        fhl["eligibility"] = eligibility
    return b


def check(f, ev):
    draft = Sentence(text=f.draft(), source_ids=f.source_ids)
    assert V.check_sentence(draft, ev) == [], f.draft()
    assert prompt.words(f.draft()) <= V.MAX_WORDS - 1
    return f.draft()


def test_eligible_keeps_the_within_cap_wording():
    f, ev = fhl_fact(case1())
    assert check(f, ev).startswith("A First Home Loan is shown because your income is within Kāinga Ora's cap")


def test_conditional_states_the_household_condition():
    f, ev = fhl_fact(case1("conditional", household=None, income=120000))
    text = check(f, ev)
    assert text == ("A First Home Loan may be available: your income is above Kāinga Ora's $95,000 single-buyer cap, "
                    "but within $150,000 for multiple buyers or dependants.")
    assert {"kb_fhl_income_cap_individual", "kb_fhl_income_cap_multiple"} <= set(f.source_ids)
    assert "because your income is within" not in text
    assert {"may be available", "but within"} <= set(f.keep)


def test_conditional_rewrite_without_the_condition_is_blocked():
    b = case1("conditional", household=None, income=120000)
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    ev = build_evidence(req, data.items, data.sources.keys())
    fs = prompt.fact_list(req, ev, data.items)
    f = next(x for x in fs if x.topic == "first_home_loan")
    from app.facts import RewriteSentence
    bad = RewriteSentence(text="A First Home Loan is shown because your income is within Kāinga Ora's cap.",
                          fact_ids=[f.id])
    assert V.check_rewrite(bad, fs, ev, V.MAX_WORDS).fails


def test_over_cap_says_not_shown():
    f, ev = fhl_fact(case1(household="single", income=200000, over_cap=True))
    assert check(f, ev).startswith("A First Home Loan is not shown because your income is above the Kāinga Ora cap")


def test_conditional_reaches_the_report_as_a_draft(monkeypatch):
    from app import gemini, service

    def down(prompt_, timeout_s):
        raise gemini.ModelUnavailable("timeout", "timeout")

    b = case1("conditional", household=None, income=120000)
    rep = service.build_report(ReportRequest.model_validate(b), generate=down)
    texts = [s.text for s in rep.summary]
    assert any(t.startswith("A First Home Loan may be available") for t in texts), texts
    assert not any("because your income is within" in t for t in texts)
