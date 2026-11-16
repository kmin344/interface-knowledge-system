"""interface.ai knowledge hackathon — a knowledge system that knows what is true *now*.

build() -> TemporalKnowledge with ingest / answer / conflicts, per harness/contract.py.
No API keys, no network: retrieval + rules over the snapshots only. See NOTES.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # the harness puts its own folder on sys.path; use its types when available
    from contract import Answer, Conflict, Snapshot, Source  # type: ignore
except Exception:  # standalone use: identical local copies
    from kb_types import Answer, Conflict, Snapshot, Source  # type: ignore

from answerer import (PCT_RE, REFUSAL_TEXT, coverage, is_rate_chunk, rank, specific_terms,  # noqa: E402
                      missing_name, time_signature, asked_terms, has_terms, strip_dates)
from conflicts import find_conflicts  # noqa: E402
from store import History, authority  # noqa: E402
from textutil import norm_space, numbers, tokens  # noqa: E402

MAX_ROWS = 6


class TemporalKnowledge:
    def __init__(self):
        self.history = History()
        self._cache: dict[tuple, Answer] = {}
        self._conflicts: Optional[list] = None

    # ---------------------------------------------------------------- contract
    def ingest(self, snapshot: Snapshot) -> None:
        self.history.add(snapshot)
        self._cache.clear()
        self._conflicts = None

    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        view = self.history.at(as_of)
        key = (re.sub(r"\W+", " ", (question or "").lower()).strip(), view.snapshot_id if view else None)
        if key in self._cache:
            return self._cache[key]
        ans = self._answer(view, question) if view else Answer(text=REFUSAL_TEXT, confident=False)
        self._cache[key] = ans
        return ans

    def conflicts(self) -> list:
        if self._conflicts is None:
            self._conflicts = find_conflicts(self.history, Conflict, Source, self._branch_count)
        return self._conflicts

    # ---------------------------------------------------------------- internals
    def _branch_count(self):
        m = re.search(r"\b(\d{1,3})\s+branch", self.answer("How many branches does Dupaco have?").text)
        return int(m.group(1)) if m else None

    def _answer(self, view, question: str) -> Answer:
        ranked, qt, it = rank(view, question)
        question = strip_dates(question)
        if not ranked:
            return Answer(text=REFUSAL_TEXT, confident=False)
        best_s, bi = ranked[0]
        best = view.chunks[bi]
        cov = coverage(view, qt, bi)
        asks_rate = it.rate and not it.url
        terms = asked_terms(question)
        if (asks_rate and (cov < 0.45 or not is_rate_chunk(best) or authority(best.url) < 0.8
                           or (terms and not has_terms(best.text, terms)))) \
                or cov < 0.3 or best_s < 1.0 or missing_name(view, question, bi):
            return Answer(text=REFUSAL_TEXT, confident=False)
        spec = specific_terms(qt)

        picked = [bi]
        if it.listing and best.kind in ("table", "row"):
            # "what cards do you offer": the best table from each authoritative page
            seen_tables = {(best.url, best.table_id)}
            for s, i in ranked[1:25]:
                c = view.chunks[i]
                if len(picked) >= 4 or s < 0.5 * best_s:
                    break
                if c.kind not in ("table", "row") or authority(c.url) < 0.9:
                    continue
                if (c.url, c.table_id) in seen_tables or any(c.text == view.chunks[j].text for j in picked):
                    continue
                seen_tables.add((c.url, c.table_id))
                picked.append(i)
        elif it.hours:
            # branches keep different schedules: show the common one first, then the others
            sigs = {time_signature(best.text)}
            for s, i in ranked[1:12]:
                c = view.chunks[i]
                sig = time_signature(c.text)
                if len(picked) >= 3 or s < 0.4 * best_s:
                    break
                if sig and sig not in sigs:
                    sigs.add(sig)
                    picked.append(i)
        elif best.kind == "row":
            # tier questions: sibling rows of the same table that fit as well
            for s, i in ranked[1:]:
                c = view.chunks[i]
                if len(picked) >= MAX_ROWS or s < 0.75 * best_s:
                    break
                if c.kind == "row" and c.url == best.url and c.table_id == best.table_id:
                    picked.append(i)
        elif best.kind == "para" and not it.url and len(best.text) < 350 and any(
                view.chunks[i].url == best.url and view.chunks[i].kind == "para" and s >= 0.7 * best_s
                for s, i in ranked[1:4]):
            picked.append(next(i for s, i in ranked[1:4] if view.chunks[i].url == best.url
                               and view.chunks[i].kind == "para" and s >= 0.7 * best_s))
        elif len(best.text) < 120 and not it.url:
            # a short line ("Tap to Pay: add your card...") rarely says everything; add the next
            # good passage from a trusted page
            for s, i in ranked[1:10]:
                c = view.chunks[i]
                if s >= 0.5 * best_s and c.kind == "para" and authority(c.url) >= 0.8 and c.text != best.text:
                    picked.append(i)
                    break

        parts = []
        for i in picked:
            c = view.chunks[i]
            t = c.text
            if c.kind == "para" and len(t) > 350:
                t = pick_sentences(t, spec, it)
            parts.append(t)
        text = " | ".join(parts)
        if best.heading and best.kind == "para":
            head = best.heading.split(" > ")[-1]
            if head.lower() not in text.lower():
                text = f"{head}: {text}"
        if it.url:
            text = self._with_links(view, bi, spec, text)
        sources = self._sources(view, ranked, picked, text)
        return Answer(text=text[:1500], sources=sources, confident=True)

    def _with_links(self, view, bi, spec, text):
        """For 'which link / how do I ... online' questions: the page itself plus any link on that
        page whose address names what was asked (join.dupaco.com for 'join')."""
        best = view.chunks[bi]
        rec = view.chunk_page[bi]
        links = []
        for c in rec.chunks:
            for l in c.links:
                if not l.startswith("http") or l in links:
                    continue
                lt = set(tokens(l.replace(".", " ").replace("/", " ")))
                if set(spec) & lt and authority(l) >= 0.8:
                    links.append(l)
        url = best.url.split("#")[0]
        return f"{text} Link: {url}" + "".join(f" , {l}" for l in links[:2])

    def _sources(self, view, ranked, picked, text):
        """Cite the answer's page, plus other trusted pages that state the same figures."""
        out, seen = [], set()
        for i in picked:
            c = view.chunks[i]
            if c.url not in seen:
                seen.add(c.url)
                out.append(Source(url=c.url, snapshot_id=view.chunk_page[i].snapshot_id, quote=c.text[:200]))
        key = {n for n in numbers(view.chunks[picked[0]].text) if n[0] in ("%", "$")}
        if key:
            for s, i in ranked[:20]:
                c = view.chunks[i]
                if c.url in seen or authority(c.url) < 0.9 or len(out) >= 4:
                    continue
                if key <= numbers(c.text):
                    seen.add(c.url)
                    out.append(Source(url=c.url, snapshot_id=view.chunk_page[i].snapshot_id, quote=c.text[:200]))
        return out


SENT_RE = re.compile(r"(?<![ap]\.m\.)(?<=[.!?])\s+(?=[A-Z0-9“\"])")


def pick_sentences(text: str, spec: list[str], it) -> str:
    """Keep the one or two sentences of a long paragraph that carry the answer."""
    sents = SENT_RE.split(text)
    if len(sents) <= 2:
        return text
    want = set(spec)
    scored = []
    for k, snt in enumerate(sents):
        st = set(tokens(snt))
        sc = len(want & st)
        if re.search(r"\$\d|\b\d", snt):
            sc += 0.5 if (it.count or it.rate) else 0.3
        if it.rate and PCT_RE.search(snt):
            sc += 0.5
        if snt.lstrip().startswith(("“", '"')):
            sc -= 0.5
        scored.append((sc, -k, k))
    scored.sort(reverse=True)
    keep = sorted(k for sc, _, k in scored[:2] if sc > 0) or [0]
    return " ".join(sents[k] for k in keep)


def build():
    return TemporalKnowledge()
