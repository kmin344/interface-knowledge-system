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

from answerer import (ADDR_RE, PHONE_RE, TIME_RE, PCT_RE, REFUSAL_TEXT, coverage, is_rate_chunk, rank, specific_terms,  # noqa: E402
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
        check = VALUE_CHECK_RE.search(question)
        if check and best.kind == "row" and is_rate_chunk(best) and authority(best.url) >= 0.8:
            # "do any savings tiers pay 0.35%?": a yes/no about one figure. Answer from the rate
            # sheet itself: the main product's tiers, then every row that does carry the figure.
            return self._value_check(view, ranked, bi, check.group(1), question)
        if (asks_rate and (cov < 0.45 or not is_rate_chunk(best) or authority(best.url) < 0.8
                           or (terms and not has_terms(best.text, terms)))) \
                or cov < 0.3 or best_s < 1.0 or missing_name(view, question, bi):
            return Answer(text=REFUSAL_TEXT, confident=False)
        spec = specific_terms(qt)

        picked = [bi]
        if it.listing and best.kind == "row":
            # a list question answered by one row: show the row's whole table instead
            for j, c in enumerate(view.chunks):
                if c.kind == "table" and c.url == best.url and c.table_id == best.table_id and len(c.text) < 1500:
                    picked = [j]
                    break
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
        elif best.kind == "para" and not it.url and any(
                view.chunks[i].url == best.url and view.chunks[i].kind == "para"
                and s >= (0.7 if len(best.text) < 350 else 0.85) * best_s
                for s, i in ranked[1:4]):
            # the next passage of the same article often carries the rest of the answer
            picked.append(next(i for s, i in ranked[1:4] if view.chunks[i].url == best.url
                               and view.chunks[i].kind == "para"
                               and s >= (0.7 if len(best.text) < 350 else 0.85) * best_s))
        elif len(best.text) < 120 and not it.url:
            # a short line ("Tap to Pay: add your card...") rarely says everything; add the next
            # good passage from a trusted page
            for s, i in ranked[1:10]:
                c = view.chunks[i]
                if s >= 0.5 * best_s and c.kind == "para" and authority(c.url) >= 0.8 and c.text != best.text:
                    picked.append(i)
                    break

        if best.kind == "para" and RELATIVE_YEAR_RE.search(question):
            # "last year" is a moving target: if the passage reports some other year, the site may
            # simply not have the asked year yet. Show each year it does report, newest first,
            # so the reader sees which year each figure belongs to.
            picked = self._year_series(view, ranked, best_s, picked, question)

        if it.eligibility and (re.match(r"^/about/join\b", re.sub(r"^https?://[^/]+", "", best.url.lower()))
                               or re.search(r"eligib|who can|join if|membership requirement", best.heading, re.I)):
            # "who can join": the site states *where* in one short line that shares no words with
            # the question ("You can find us in Iowa, Illinois and Wisconsin")
            for j, c in enumerate(view.chunks):
                if c.url == best.url and c.kind == "para" and j not in picked and REGION_RE.search(c.text):
                    picked.append(j)
                    break
        parts = []
        for i in picked:
            c = view.chunks[i]
            t = c.text
            if c.kind == "para" and len(t) > 350:
                t = pick_sentences(t, spec, it, qt)
            parts.append(t)
        text = " | ".join(parts)
        if best.heading and best.kind == "para" and picked[0] == bi:
            head = best.heading.split(" > ")[-1]
            if head.lower() not in text.lower():
                text = f"{head}: {text}"
        if it.url:
            text = self._with_links(view, bi, spec, text)
        sources = self._sources(view, ranked, picked, text)
        if it.hours or it.address:
            # branch facts: also point to the site's own branch locator page
            for url, rec in view.pages.items():
                if rec.present and re.sub(r"^https?://[^/]+", "", url).rstrip("/") in it.homes \
                        and all(s.url != url for s in sources) and len(sources) < 4:
                    sources.append(Source(url=url, snapshot_id=rec.snapshot_id))
        return Answer(text=text[:1500], sources=sources, confident=True)

    def _value_check(self, view, ranked, bi, value: str, question: str) -> Answer:
        best = view.chunks[bi]
        want = f"{float(value):.2f}%"
        # the product the question names, as the rate sheet lists it first ("savings" ->
        # Share Savings, the first savings table): rows ranked for the question on that page
        best_s = ranked[0][0]
        rows = [(s, i) for s, i in ranked if view.chunks[i].kind == "row" and view.chunks[i].url == best.url]
        terms = asked_terms(question)
        termed = [i for s, i in rows if terms and has_terms(view.chunks[i].text, terms)]
        if termed:
            # "the 60 month certificate": the row(s) that name the term, and their table
            main_table = view.chunks[termed[0]].table_id
            main = [i for i in termed if view.chunks[i].table_id == main_table][:MAX_ROWS]
        else:
            # a product named in general ("savings tiers"): of the tables that match as well as
            # the best row, the one the rate sheet lists first (Share Savings before Launchpad)
            main_table = min(view.chunks[i].table_id for s, i in rows if s >= 0.95 * best_s)
            main = [i for i, c in enumerate(view.chunks)
                    if c.kind == "row" and c.url == best.url and c.table_id == main_table][:MAX_ROWS]
        hits = [i for i, c in enumerate(view.chunks)
                if c.kind == "row" and c.url == best.url and want in c.text and i not in main][:4]
        name = row_label(view.chunks[main[0]].text, short=True)
        rates = sorted({float(m) for i in main for m in re.findall(r"(\d+\.\d\d)%", view.chunks[i].text)})
        top = f"{name} tops out at {rates[-1]:.2f}%." if rates else ""
        if any(want in view.chunks[i].text for i in main):
            lead = f"Yes: {want} is on {name}."
        elif hits:
            # say where the figure is, and what the named product pays; let the reader judge
            lead = f"{want} is listed for: " + "; ".join(row_label(view.chunks[i].text) for i in hits) + f". {top}"
        else:
            lead = f"No rate on the rate sheet is {want}. {top}"
        parts = [lead] + [view.chunks[i].text for i in main] + [view.chunks[i].text for i in hits]
        picked = main + hits
        return Answer(text=" | ".join(parts)[:1500], sources=self._sources(view, ranked, picked or [bi], ""),
                      confident=True)

    def _year_series(self, view, ranked, best_s, picked, question):
        def year(t):
            m = YEAR_LABEL_RE.search(t)
            return int(m.group(1)) if m else None
        y0 = year(view.chunks[picked[0]].text)
        ref = int(view.taken_on[:4])
        target = ref if re.search(r"\bthis\s+year", question, re.I) else ref - 1
        if y0 is None or y0 == target:
            return picked
        by_year = {y0: picked[0]}
        for s, i in ranked[1:12]:
            c = view.chunks[i]
            y = year(c.text)
            if s < 0.5 * best_s or c.kind != "para" or y is None or y in by_year or not re.search(r"\d", c.text):
                continue
            by_year[y] = i
            if len(by_year) >= 3:
                break
        return [by_year[y] for y in sorted(by_year, reverse=True)]

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
                host = re.sub(r"^https?://", "", l).split("/")[0]
                page_host = re.sub(r"^https?://", "", best.url).split("/")[0].removeprefix("www.")
                sister = host != page_host and host.endswith("." + page_host) and authority(l) >= 0.8
                if (set(spec) & lt and authority(l) >= 0.8) or sister:
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


_PLACE = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)*"
# a list of three or more places: "in Iowa, Illinois and Wisconsin" (not "in Shine Online and Mobile Banking")
REGION_RE = re.compile(r"\b(?:in|across|throughout)\s+" + _PLACE + r"(?:,\s*" + _PLACE + r")+,?\s+and\s+" + _PLACE)
# yes/no about one figure: "Do any savings tiers pay 0.35% APY?", "Does the 12-month pay 3.40%?"
VALUE_CHECK_RE = re.compile(r"^\s*(?:do|does|is|are|can|will)\b(?!.*\b(?:below|under|above|over|less|more|least|most|lower|higher|than)\b)"
                            r".*?\b(\d{1,2}(?:\.\d{1,3})?)\s?%", re.I)
RELATIVE_YEAR_RE = re.compile(r"\b(?:last|previous|past|this)\s+year\b", re.I)
# a figure labelled with its year at the start of a sentence: "In 2024 alone: ...", "In 2025, ..."
YEAR_LABEL_RE = re.compile(r"(?:^|[.:;!?]\s+)In (20\d\d)\b")
DATE_IN_TEXT_RE =re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? 20\d\d\b")
SENT_RE = re.compile(r"(?<![ap]\.m\.)(?<=[.!?])\s+(?=[A-Z0-9“\"])")


def row_label(row: str, short: bool = False) -> str:
    """'Share Savings — Account Type: Share Savings; Balance Tier: $25,000+; ...' ->
    'Share Savings ($25,000+)'; 'Regular Certificates — Certificate: 12 Month; ...' ->
    'Regular Certificates (12 Month)'."""
    name, _, rest = row.partition(" — ")
    if short:
        return name.strip()
    vals = [f.split(":", 1)[1].strip() for f in rest.split("; ")[:2] if ":" in f]
    extra = [v for v in vals if v and v != name.strip() and v != "—"]
    return f"{name.strip()} ({extra[0]})" if extra else name.strip()


def pick_sentences(text: str, spec: list[str], it, qt_all=None) -> str:
    """Keep the one or two sentences of a long paragraph that carry the answer."""
    sents = SENT_RE.split(text)
    if len(sents) <= 2:
        return text
    want = set(spec)
    allq = set(qt_all or [])
    scored = []
    for k, snt in enumerate(sents):
        st = set(tokens(snt))
        sc = len(want & st) + 0.25 * len((allq - want) & st)
        if re.search(r"\$\d|\b\d", snt):
            sc += 0.5 if (it.count or it.rate) else 0.3
        if it.rate and PCT_RE.search(snt):
            sc += 0.5
        if (it.phone and PHONE_RE.search(snt)) or (it.hours and TIME_RE.search(snt)) \
                or (it.address and ADDR_RE.search(snt)):
            sc += 1.0
        if it.when and DATE_IN_TEXT_RE.search(snt):
            sc += 1.0
        if snt.lstrip().startswith(("“", '"')):
            sc -= 0.5
        scored.append((sc, -k, k))
    scored.sort(reverse=True)
    keep = sorted(k for sc, _, k in scored[:2] if sc > 0) or [0]
    return " ".join(sents[k] for k in keep)


def build():
    return TemporalKnowledge()
