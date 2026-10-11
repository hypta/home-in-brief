"""Compose the final sentences from checked rewrites and the fact drafts (summary and follow-up).

A rewrite that passed validator.check_rewrite is kept. A blocked rewrite is replaced by the drafts of the facts
it named (one sentence each), unless another passing rewrite already covers that fact. For the summary, every
must-mention fact that no sentence covers is added as its draft. Summary sentences are shown in fact (display)
order; over the limit, the least important optional sentences go first, then rewrites whose must facts another
sentence also covers; as a last resort the result is the must drafts alone. Follow-up answers keep the model's
order and drop drafts first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from b_common.report_schemas import Sentence

from .facts import Fact
from .validator import Rewrite

log = logging.getLogger("report-api.rewrite")

REWRITE, DRAFT, MUST_ADDED = "rewrite", "draft", "must_added"


@dataclass
class Item:
    sentence: Sentence
    fact_ids: list[str]
    kind: str          # rewrite | draft (a blocked rewrite's fact) | must_added (a must fact no sentence used)
    user: bool         # uses a fact that relies on chat-supplied input: the caller adds the prefix
    fields: frozenset = frozenset()  # the chat-supplied fields its facts depend on (Fact.fields): picks the prefix


def compose(checked: list[Rewrite], facts: list[Fact], limit: int, add_musts: bool = True,
            check_draft: Optional[Callable[[Sentence], list[str]]] = None, sort: bool = True) -> list[Item]:
    """`sort`: show sentences in fact (display) order (summary); else keep the model's order, with a blocked
    sentence's drafts in its place (follow-up answers)."""
    fmap = {f.id: f for f in facts}
    index = {f.id: n for n, f in enumerate(facts)}
    items: list[Item] = []
    covered: set[str] = {i for r in checked if r.sentence is not None for i in r.fact_ids}

    def add_draft(fid: str, kind: str) -> None:
        f = fmap[fid]
        s = Sentence(text=f.draft(), source_ids=list(f.source_ids))
        fails = check_draft(s) if check_draft else []
        if fails:  # drafts are tested to pass; never show one that does not
            log.warning("draft %s dropped: %s", fid, ",".join(fails))
            return
        items.append(Item(s, [fid], kind, f.user, frozenset(f.fields)))
        covered.add(fid)

    for r in checked:
        if r.sentence is not None:
            items.append(Item(r.sentence, r.fact_ids, REWRITE, any(fmap[i].user for i in r.fact_ids),
                              frozenset(x for i in r.fact_ids for x in fmap[i].fields)))
        else:
            for fid in sorted(r.fact_ids, key=lambda i: fmap[i].priority):
                if fid not in covered:
                    add_draft(fid, DRAFT)
    if add_musts:
        for f in facts:
            if f.must and f.id not in covered:
                add_draft(f.id, MUST_ADDED)

    if not sort:  # follow-up: no must facts; over the limit, drop drafts first, then the last sentences
        while len(items) > limit:
            drafts = [it for it in items if it.kind != REWRITE]
            items.remove(drafts[-1] if drafts else items[-1])
        return items
    items.sort(key=lambda it: min(index[i] for i in it.fact_ids))
    while len(items) > limit:
        optional = [it for it in items if not any(fmap[i].must for i in it.fact_ids)]
        if optional:
            items.remove(max(optional, key=lambda it: (min(fmap[i].priority for i in it.fact_ids),
                                                        items.index(it))))
            continue
        spare = [it for it in items
                 if all(any(i in o.fact_ids for o in items if o is not it) for i in it.fact_ids if fmap[i].must)]
        if spare:
            items.remove(spare[-1])
            continue
        musts = [f for f in facts if f.must][:limit]
        items.clear()
        for f in musts:
            add_draft(f.id, MUST_ADDED)
        break
    return items


def stats(items: list[Item], checked: list[Rewrite]) -> dict:
    """Counts for eval and logs (not part of the response contract)."""
    return {"rewritten": sum(it.kind == REWRITE for it in items),
            "fallback_drafts": sum(it.kind == DRAFT for it in items),
            "must_added": sum(it.kind == MUST_ADDED for it in items),
            "blocked": sum(r.sentence is None for r in checked),
            "model_sentences": len(checked)}
