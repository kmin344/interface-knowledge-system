"""Find places where the website contradicts itself, and say which value we believe.

Three kinds, each kept deliberately narrow because every extra entry that is not a real
contradiction lowers precision:

1. Rate leftovers. A rate (APR / APY / dividend) quoted on a lower-authority page (calculator,
   promo landing page, blog, press release) that does not appear among the values the official
   rate table gives for the same product and term. Winner: the official table's value for the
   closest tier.
2. Repeated counts. A figure the site states about itself on many pages ("22 branch office
   locations") contradicted by a minority of pages ("24 branch office locations"). Winner: the
   value the large majority of pages state.
3. Ordinals. "the fifth time Forbes recognized Dupaco" vs "the fourth time". Winner: the highest
   ordinal, since awards accumulate.
"""
from __future__ import annotations

import re
from collections import defaultdict

from answerer import expired
from store import authority
from textutil import tokens

PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
YEARS_RE = re.compile(r"\b(?:19|20)\d\d\s?[-–]\s?(?:19|20)?\d\d\b|\b(?:19|20)\d\d\b")
TERM_RE = re.compile(r"\b(\d{1,3})[- ]?(?:month|mo\b|year|yr)", re.I)
METRIC_RE = re.compile(r"\b(apr|apy|annual percentage rate|annual percentage yield|dividend rate|rate)\b", re.I)
RATE_SENT_RE = re.compile(r"[^.;|]*\d+(?:\.\d+)?\s?%[^.;|]*", re.I)
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
ORD_RE = re.compile(r"\b(" + "|".join(ORDINALS) + r") time (?:that )?([A-Z][A-Za-z]+)")
COUNT_RE = re.compile(r"\b(\d{1,3})\s+(branch office locations|branch locations|branches)\b", re.I)
STOPKEY = {"apr", "apy", "annual", "percentage", "rate", "yield", "dividend", "low", "term", "loan", "program",
           "month", "year", "model", "min", "earn", "open", "point", "payment", "example", "type", "card"}


def _metric(text: str) -> str:
    m = METRIC_RE.search(text)
    if not m:
        return ""
    w = m.group(1).lower()
    return {"annual percentage rate": "apr", "annual percentage yield": "apy", "dividend rate": "dividend"}.get(w, w)


def _key(text: str) -> frozenset:
    t = YEARS_RE.sub(" ", text)
    t = PCT_RE.sub(" ", t)
    ws = (re.sub(r"\d+$", "", w) for w in tokens(t))
    return frozenset(w for w in ws if w and not w[0].isdigit() and w not in STOPKEY and len(w) > 2)


def _terms(text: str) -> frozenset:
    return frozenset(int(x) for x in TERM_RE.findall(YEARS_RE.sub(" ", text)))


def _src(Source, rec, url, quote):
    return Source(url=url, snapshot_id=rec.snapshot_id, quote=quote[:200])


def find_conflicts(history, Conflict, Source, resolve_count=None) -> list:
    view = history.at(None)
    if view is None:
        return []
    out: list = []
    out += _rate_leftovers(view, Conflict, Source)
    out += _changes(history, Conflict, Source)
    out += _counts(view, Conflict, Source, resolve_count)
    out += _ordinals(view, Conflict, Source)
    return out


def _rate_leftovers(view, Conflict, Source) -> list:
    # Official facts: rows of rate tables on trusted pages.
    facts = []   # (key, terms, metric, value, url, rec, text)
    for c, rec in zip(view.chunks, view.chunk_page):
        if authority(c.url) < 0.9 or not rec.present or rec.stale(view.taken_on) or c.kind == "table":
            continue
        if expired(c.text, view.taken_on):
            continue
        if c.kind == "para":
            # short rate statements on official pages: "6-month certificate APY: 3.50%"
            for sent in RATE_SENT_RE.findall(c.text):
                m = PCT_RE.search(sent)
                met = _metric(sent)
                if m and met in ("apr", "apy") and len(sent) <= 120 and len(PCT_RE.findall(sent)) == 1:
                    facts.append((_key(sent), _terms(sent), met, float(m.group(1)), c.url, rec, sent.strip()))
            continue
        for part in c.text.split(";"):
            m = PCT_RE.search(part)
            if not m:
                continue
            met = _metric(part)
            if not met:
                continue
            facts.append((_key(c.text), _terms(c.text), met, float(m.group(1)), c.url, rec, c.text))
    by_key = defaultdict(list)
    for f in facts:
        by_key[(f[2], f[1])].append(f)

    groups: dict = {}   # (claim page, official page + table) -> list of (old, new, fact)
    order = []
    for c, rec in zip(view.chunks, view.chunk_page):
        a = authority(c.url)
        stale_page = rec.stale(view.taken_on) or expired(c.text, view.taken_on)
        if a >= 0.9 and not stale_page:
            continue
        if c.kind == "table":
            continue
        claims = [c.text] if c.kind == "row" else RATE_SENT_RE.findall(c.text)
        for sent in claims:
            met = _metric(sent)
            if met not in ("apr", "apy"):
                continue
            terms = _terms(sent) or _terms(c.text)
            k = _key(sent)
            cands = by_key.get((met, terms), [])
            if not cands or not k:
                continue
            scored = [(len(k & f[0]) / max(1, len(f[0])), f) for f in cands]
            scored = [(s, f) for s, f in scored if s >= 0.5]
            if not scored:
                continue
            official = {f[3] for s, f in scored}
            best = _closest_tier(sent, scored)
            for v in PCT_RE.findall(sent):
                v = float(v)
                if v in official or v < 0.5:
                    continue
                g = (c.url, best[4], best[6].split(" — ")[0])
                if g not in groups:
                    groups[g] = {"rows": [], "rec": rec, "a": a, "stale": stale_page, "sent": sent}
                    order.append(g)
                if all(r[0] != v for r in groups[g]["rows"]):
                    groups[g]["rows"].append((v, best[3], best, terms))
    out = []
    for g in sorted(order, key=lambda g: groups[g]["a"]):
        d = groups[g]
        rows = d["rows"]
        best = rows[0][2]
        met = best[2]
        terms = sorted({t for r in rows for t in r[3]})
        label = g[2]
        olds = [f"{r[0]:g}%" for r in rows]
        news = list(dict.fromkeys(f"{r[1]:g}%" for r in rows))
        res = "; ".join(f"{_tier_label(r[2][6])}: {r[1]:g}%" for r in rows)
        out.append(Conflict(
            topic=f"{label} {met.upper()} ({', '.join(f'{t}-month' for t in terms)})",
            values=olds + [n for n in news if n not in olds],
            sources=[_src(Source, d["rec"], g[0], d["sent"]), _src(Source, best[5], best[4], best[6])],
            resolution=res,
            reason=(f"The official rate table ({best[4]}) gives {', '.join(news)}; "
                    f"{', '.join(olds)} appear only on a {_kind(g[0], d['stale'])} that is not kept current."),
        ))
    return out[:15]


def _changes(history, Conflict, Source) -> list:
    """Official rate rows whose value changed between snapshots. The newest snapshot wins; the
    older value is what stale copies and old answers repeat."""
    seq: dict = {}
    where: dict = {}
    for v in history.views:
        for c, rec in zip(v.chunks, v.chunk_page):
            if c.kind != "row" or authority(c.url) < 0.9 or not rec.present:
                continue
            vals = tuple(PCT_RE.findall(c.text))
            if not vals:
                continue
            label = (c.url.rstrip("/"), _row_label(c.text))
            if (label, v.taken_on) in where:
                continue
            seq.setdefault(label, []).append((v.taken_on, vals))
            where[(label, v.taken_on)] = (c, rec)
    latest = history.views[-1].taken_on
    out = []
    done = set()

    def prio(label):
        u = label[0]
        return (0 if re.search(r"/(rates|personal/[^/]*savings|personal/checking|personal/loans|personal/credit)", u)
                else 1 if "/personal/" in u else 2, u, label[1])
    for label in sorted(seq, key=prio):
        s = seq[label]
        if s[-1][0] != latest:
            continue            # the product is gone; nothing current to resolve to
        changes = [(a, b) for a, b in zip(s, s[1:]) if a[1] != b[1] and len(a[1]) == len(b[1])]
        if not changes:
            continue
        first, last = s[0], s[-1]
        old_vals, new_vals = [], []
        for (d0, a), (d1, b) in changes:
            for x, y in zip(a, b):
                if x != y:
                    old_vals.append(f"{float(x):g}%")
                    new_vals.append(f"{float(y):g}%")
        cur = [f"{float(x):g}%" for x in last[1]]
        olds = [o for o in dict.fromkeys(old_vals) if o not in cur]
        if not olds:
            continue
        c_old, r_old = where[(label, changes[0][0][0])]
        c_new, r_new = where[(label, latest)]
        name = label[1].split(" | ", 1)[-1].split(";")[0].strip()
        sig = (re.sub(r"\b(certificate|roth|traditional|business)\b|:", "", name).split(), tuple(sorted(olds)))
        sig = (" ".join(sig[0]), sig[1])
        if sig in done:
            continue
        done.add(sig)
        out.append(Conflict(
            topic=f"{c_new.text.split(' — ')[0]} {name}"[:120],
            values=olds + [n for n in dict.fromkeys(new_vals) if n in cur],
            sources=[_src(Source, r_old, c_old.url, c_old.text), _src(Source, r_new, c_new.url, c_new.text)],
            resolution=", ".join(n for n in dict.fromkeys(new_vals) if n in cur) or ", ".join(cur),
            reason=f"The rate changed between snapshots ({changes[0][0][0]} -> {latest}); the newest rate table wins.",
        ))
    return out[:12]


def _row_label(text: str) -> str:
    """Row identity that survives cosmetic edits: caption + cell values, minus numbers that are
    rates, header names, footnote marks and trailing .00 on amounts."""
    cap, _, body = text.rpartition(" — ")
    cells = []
    for part in body.split(";"):
        v = part.split(":", 1)[-1]
        v = PCT_RE.sub("#", v)
        v = re.sub(r"#\d+", "#", v)
        v = re.sub(r"\.00\b", "", v)
        v = re.sub(r"(?<=[a-zA-Z])\d\b", "", v)
        cells.append(re.sub(r"\s+", " ", v).strip().lower())
    cap_word = (cap.strip().lower().split() or [""])[0]     # "Special" == "Special Certificates"
    return (cap_word + " | " + "; ".join(cells))


def _ranges(text: str) -> list[tuple[int, int]]:
    out = []
    for m in re.finditer(r"\b((?:19|20)\d\d)\s?[-–]\s?((?:19|20)?\d\d)\b", text):
        a, b = m.group(1), m.group(2)
        out.append((int(a), int(b) if len(b) == 4 else int(a[:2] + b)))
    for m in re.finditer(r"\b((?:19|20)\d\d) Model Year", text):
        out.append((int(m.group(1)), int(m.group(1))))
    return out


def _closest_tier(claim: str, scored):
    """Line up the stale value with the official tier it was the old version of."""
    cr = _ranges(claim)
    def score(item):
        s, f = item
        fr = _ranges(f[6])
        if cr and fr:
            (a, b), (c, d) = cr[0], fr[0]
            ov = max(0, min(b, d) - max(a, c) + 1)
            return (ov, c, s)
        newest = fr[0][0] if fr else 0
        return (0, newest, s)
    return max(scored, key=score)[1]


def _tier_label(row_text: str) -> str:
    parts = [p.split(":", 1)[-1].strip() for p in row_text.split(" — ")[-1].split(";")]
    return " ".join(p for p in parts if not PCT_RE.search(p))[:60]


def _topic(row_text: str, met: str, terms) -> str:
    label = re.split(r";", row_text)[0]
    label = re.sub(r"\s+", " ", label).strip()
    t = ", ".join(f"{x}-month" for x in sorted(terms))
    return f"{label} {t} {met.upper()}".replace("  ", " ").strip()


def _kind(url: str, stale: bool) -> str:
    u = url.lower()
    if "calculator" in u:
        return "calculator page"
    if "/newsroom" in u:
        return "press release"
    if "/learn/" in u or re.search(r"/20\d\d/", u):
        return "blog post"
    if stale:
        return "page whose offer has ended or whose own rates date is over a year old"
    return "secondary page"


def _counts(view, Conflict, Source, resolve_count=None) -> list:
    """Self-descriptions that disagree across pages ("22" vs "23" vs "24 branch office locations").
    The winner is the figure our answer() gives for the same question, so the two never disagree."""
    pages = defaultdict(set)
    where = {}
    for c, rec in zip(view.chunks, view.chunk_page):
        for n, noun in COUNT_RE.findall(c.text):
            if int(n) < 2:
                continue
            pages[int(n)].add(c.url)
            where.setdefault(int(n), (c.url, rec, c.text))
    vals = [n for n in sorted(pages, key=lambda n: -len(pages[n])) if len(pages[n]) >= 2]
    if len(vals) < 2:
        return []
    win = resolve_count() if resolve_count else None
    if win not in vals:
        win = vals[0]
    return [Conflict(
        topic="number of Dupaco branch office locations (branches)",
        values=[f"{n} branch office locations" for n in sorted(vals)],
        sources=[_src(Source, where[n][1], where[n][0], where[n][2]) for n in sorted(vals)],
        resolution=f"{win} branch office locations",
        reason="Press releases from different years carry different boilerplate counts; "
               + ", ".join(f"{n}: {len(pages[n])} pages" for n in sorted(vals)) + ".",
    )]


def _ordinals(view, Conflict, Source) -> list:
    hits = defaultdict(dict)
    for c, rec in zip(view.chunks, view.chunk_page):
        # the same story re-published under -2, -3 ... slugs
        base = re.sub(r"-\d+$", "", c.url.rstrip("/"))
        for o, who in ORD_RE.findall(c.text):
            hits[(who, base)][o.lower()] = (c.url, rec, c.text)
    out = []
    for (who, _base), d in hits.items():
        if len(d) < 2:
            continue
        ords = sorted(d, key=ORDINALS.index)
        top = ords[-1]
        for o in ords[:-1]:
            out.append(Conflict(
                topic=f"how many times {who} recognized Dupaco",
                values=[o, top],
                sources=[_src(Source, d[o][1], d[o][0], d[o][2]), _src(Source, d[top][1], d[top][0], d[top][2])],
                resolution=top,
                reason=f"Recognitions accumulate; the newer release says the {top} time.",
            ))
    return out
