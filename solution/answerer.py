"""Pick the passage that answers the question, from the right page, at the right time.

score(chunk) = BM25(question, chunk)
             x authority(page)          rate sheet / product page > PDF > newsroom > blog > calculator
             x freshness(page)          page missing from this snapshot, or its own "rates effective"
                                        date more than a year old
             x intent fit               a rate question wants a rate row, an hours question a time ...
             x topic home               the page the site itself uses for that topic (branches -> /contact)
             x coverage                 how many of the question's specific words the chunk contains

The answer text is the chunk itself (plus sibling rows of the same table when the question spans
tiers). Nothing is paraphrased, so a number in the answer is always a number on the cited page.
When nothing on the site fits well enough we say so and give no number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from store import View, authority, story_family
from textutil import GENERIC, tokens

PCT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:a\.?m\.?|p\.?m\.?)|\bnoon\b", re.I)
ADDR_RE = re.compile(r"\b\d{2,6}\s+[A-Z][\w.]*(?:\s+[A-Z][\w.]*)*\s+(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|"
                     r"Lane|Ln|Blvd|Way|Court|Ct|Pkwy|Parkway|Highway|Hwy|Place|Pl|Square|Sq)\b")
PHONE_RE = re.compile(r"\b\d{3}[-.)\s]\s?\d{3}[-.]\d{4}\b")
RATE_HDR_RE = re.compile(r"\b(rate|apr|apy|annual percentage|dividend)\b", re.I)
# a sentence that actually states a rate: "12.50%–19.50% APR", "APY of 4.10%", "rate: 7.13%"
RATE_STMT_RE = re.compile(r"\d+(?:\.\d+)?\s?%\s*(?:[-–]\s*\d+(?:\.\d+)?\s?%\s*)?(?:\d\s*)?(?:apr|apy|annual|fixed|"
                          r"variable|intro|dividend|interest)\b|(?:apr|apy|rate|dividend)s?\s*(?:of|is|are|:|at)?\s*"
                          r"(?:as low as\s*|up to\s*)?\d+(?:\.\d+)?\s?%", re.I)
FOOTNOTE_RE = re.compile(r"^[_*\d]|subject to change|payment example|rates? (?:are|is) based on|"
                         r"rate quotes|not include taxes", re.I)
YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")
RANGE_RE = re.compile(r"\b((?:19|20)\d\d)\s?[-–]\s?((?:19|20)?\d\d)\b")

REFUSAL_TEXT = ("I'm sorry, that isn't listed on Dupaco's website, so I can't confirm it. "
                "Please contact Dupaco directly and a team member can help.")

# The page the website itself treats as the home of a topic. Only a gentle boost.
TOPIC_HOME = [
    (r"\b(branch|branches|lobby|drive[- ]?up|hours|address|atm|routing|locat)", ("/contact",)),
    (r"\b(mortgage|home loan|refinanc)", ("/personal/loans/home-loans", "/rates")),
    (r"\b(auto|car|vehicle|motorcycle)", ("/personal/loans/auto-motorcycle-loans", "/rates")),
    (r"\b(certificate|cd|cds|savings|money market|ira)\b", ("/personal/savings-account", "/rates")),
    (r"\bchecking\b", ("/personal/checking-accounts", "/rates")),
    (r"\bcredit cards?\b|\bvisa\b", ("/personal/credit-cards", "/business/credit-cards", "/rates")),
    (r"\b(join|member|eligib)", ("/about/join",)),
    (r"\bpersonal loan", ("/personal/loans/personal-loans", "/rates")),
    (r"\bstudent", ("/personal/loans/student-loans",)),
    (r"\bhome equity|heloc", ("/personal/loans/home-equity", "/rates")),
]


@dataclass
class Intent:
    rate: bool
    hours: bool
    url: bool
    count: bool
    address: bool
    phone: bool
    listing: bool
    year: int | None
    homes: tuple
    amount: float | None = None
    times: bool = False
    when: bool = False
    award: bool = False
    eligibility: bool = False


def intent_of(q: str) -> Intent:
    ql = q.lower()
    ym = YEAR_RE.search(q)
    homes = tuple(h for pat, hs in TOPIC_HOME if re.search(pat, ql) for h in hs)
    return Intent(
        rate=bool(re.search(r"\b(rates?|apr|apy|interest|dividends?|yield|percent)\b|%", ql)
                  or re.search(r"\bcharg\w*\b.*\b(loan|mortgage|card)s?\b", ql)),
        hours=bool(re.search(r"\b(hours?|lobby|lobbies|drive[- ]?(?:up|thru|through)|what time|open until|"
                             r"(?:is|are) (?:\w+ ){0,3}open on)\b", ql)),
        url=bool(re.search(r"\b(link|url|website|web page|webpage|web address|online)\b", ql)),
        count=bool(re.search(r"\bhow many\b", ql)),
        address=bool(re.search(r"\b(address|located|location of)\b", ql)),
        phone=bool(re.search(r"\b(phone|call|telephone|contact information|contact info)\b|"
                             r"\b(?:update|change|correct)\b.{0,20}\b(?:address|email|name|number)\b", ql)),
        listing=bool(re.search(r"\b(what|which) (\w+ )?(cards|accounts|certificates|options|loans|products)\b"
                               r"|\ball\b|\blist\b|\bcredit cards?\b.*\brates\b|\bcredit cards\b|"
                               r"\b(accounts|certificates|cds|cards)\b.*\b(pay|earn|rates|interest|apy|apr)\b|"
                               r"\bcertificate of accounts\b", ql)),
        year=int(ym.group(1)) if ym else None,
        homes=homes,
        amount=float(am.group(1).replace(",", "")) if (am := AMT_RE.search(q)) else None,
        times=bool(re.search(r"\bhow many times\b", ql)),
        award=bool(re.search(r"\b(recogni\w*|awards?|honou?red|honou?rs|ranked|rankings?|accolades?)\b", ql)),
        eligibility=bool(re.search(r"\b(who can (join|become)|qualif\w* (to|for) (join|membership)|"
                                   r"can i (join|become a member))\b|"
                                   r"\beligib\w*\b.{0,40}\b(join|member\w*)\b|\b(join|member\w*)\b.{0,40}\beligib\w*\b", ql)),
        when=bool(re.search(r"\b(when|what date|still open|reopen)\b", ql)
                  or (re.search(r"\b(closed?|closing)\b", ql) and not re.search(r"\bwhere\b", ql))),
    )


def _year_fit(text: str, year: int) -> float:
    """'2024-27 Model Year' covers 2025; '2021-22' does not."""
    best = 1.0
    for a, b in RANGE_RE.findall(text):
        lo = int(a)
        hi = int(b) if len(b) == 4 else int(a[:2] + b)
        if lo <= year <= hi:
            return 1.6
        best = 0.4
    return best


ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
ORDINAL_TIME_RE = re.compile(r"\b(" + "|".join(ORDINALS) + r") time\b", re.I)


def _fit(c, it: Intent, qspec: list[str]) -> float:
    t = c.text
    f = 1.0
    if it.rate and not it.url:
        f *= 1.5 if is_rate_chunk(c) else 0.4
        if c.kind == "row" and PCT_RE.search(t) and not re.search(r"(?<![\d.])(?:0\.\d*[1-9]|[1-9]\d*(?:\.\d+)?)\s?%", t):
            f *= 0.8   # a 0.00% tier rarely answers "what do you pay"
    if FOOTNOTE_RE.search(t) and c.kind == "para":
        f *= 0.4
    if it.hours:
        f *= 1.5 if TIME_RE.search(t) else 0.3
        if not re.search(r"\b(lobby|drive|branch|hours)", t, re.I):
            f *= 0.5   # a shop's opening times are not ours
    if it.address:
        f *= 1.6 if ADDR_RE.search(t) else 0.6
    if it.phone:
        f *= 1.4 if PHONE_RE.search(t) else 0.7
    if it.count:
        pat = count_pattern(qspec)
        if pat and pat.search(t):
            f *= 2.5
        else:
            f *= 0.5
    if it.year:
        f *= _year_fit(t, it.year)
    if it.amount is not None and c.kind == "row":
        f *= _amount_fit(t, it.amount)
    elif it.amount is not None and c.kind == "table":
        f *= 0.8   # a balance question wants the one tier, not the whole table
    if it.award and c.kind == "para":
        # an award question wants the sentence that says what the subject received
        m = re.search(ORDINAL_TIME_RE, t)
        if m and it.times:
            pass   # the "how many times" boost below already weighs the ordinal
        elif m:
            # recognitions accumulate: the highest count is the current one
            f *= 1.3 + 0.3 * ORDINALS.index(m.group(1).lower())
        elif re.search(r"\b(named|ranked|awarded|honored|selected as|recognized as)\b", t, re.I):
            f *= 1.15
    if it.times:
        m = re.search(ORDINAL_TIME_RE, t)
        f *= (1.5 + 0.4 * ORDINALS.index(m.group(1).lower())) if m else 0.6
    if c.kind == "table":
        f *= 1.3 if it.listing else 0.8
    if len(t) < 25:
        f *= 0.6
    elif c.kind == "para" and len(t) < 60 and "." not in t.rstrip(".")[-40:] and not re.search(r"\d:\d\d|\$|%", t):
        f *= 0.6   # labels and buttons: "Keep reading 2 min read", "Apply for Student Loan"
    return f


def count_pattern(spec: list[str]):
    nouns = [re.escape(w[:5]) for w in spec if len(w) > 2]
    if not nouns:
        return None
    return re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})?)\+?\s+(?:[a-z]+\s+){0,2}(?:" + "|".join(nouns) + ")", re.I)


def is_rate_chunk(c) -> bool:
    return bool((c.kind in ("row", "table") and PCT_RE.search(c.text)) or RATE_STMT_RE.search(c.text))


def time_signature(text: str) -> frozenset:
    return frozenset(re.sub(r"[\s.]", "", m.lower()) for m in TIME_RE.findall(text))


def specific_terms(qt: list[str]) -> list[str]:
    # calendar years are dates, not topics: "in May 2026" must not count against a passage
    return [t for t in dict.fromkeys(qt) if t not in GENERIC and not re.fullmatch(r"(19|20)\d\d", t)]


TERM_Q_RE = re.compile(r"\b(\d{1,3})\s?[- ]?\s?(month|mo|year|yr)s?\b|\b(\d{1,2}/\d{1,2})\b", re.I)


NUM_WORDS = {"ten": 10, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "thirty": 30,
             "forty": 40, "sixty": 60, "six": 6, "seventeen": 17}


def asked_terms(question: str) -> set[str]:
    """'40-year', '60 month', '7/1': the product term a rate question is about."""
    question = re.sub(r"\b(" + "|".join(NUM_WORDS) + r")\b", lambda m: str(NUM_WORDS[m.group(1).lower()]),
                      question, flags=re.I)
    out = set()
    for n, unit, arm in TERM_Q_RE.findall(question):
        if arm:
            out.add(arm)
        elif n:
            out.add(f"{int(n)}{unit[0].lower()}")
    return out


def has_terms(text: str, terms: set[str]) -> bool:
    found = asked_terms(text)
    return terms <= found


QUALIFIERS = ("business", "jumbo", "ira", "roth", "traditional", "kids", "youth", "refinance", "health",
              "student", "commercial", "promo", "intro", "introductory")


NEG_RE = re.compile(r"\b(?:not|non|no|excluding|except|without)[- ](?:the |a )?(\w+)")


def _qualifier_fit(c, ql: str) -> float:
    """'Rewards Visa' is not 'Business Rewards Visa'; a 30-year mortgage is not a refinance;
    '(not jumbo)' rules jumbo out."""
    label = (c.text.split(";")[0] if c.kind in ("row", "table") else c.text[:80]).lower()
    negated = set(NEG_RE.findall(ql))
    asked = NEG_RE.sub(" ", ql)
    f = 1.0
    for q in QUALIFIERS:
        if re.search(r"\b" + q, label) and not re.search(r"\b" + q, asked):
            f *= 0.4 if q in negated else 0.7
    for w in negated - set(QUALIFIERS):
        if len(w) > 3 and re.search(r"\b" + re.escape(w), label):
            f *= 0.4
    return f


AMT_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)")


def _amount_fit(text: str, amount: float) -> float:
    """Balance / loan-amount tiers: '$10,000 – $24,999.99' holds $12,000, '$25,000+' does not."""
    best = 1.0
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:[-–]|to)\s*\$\s?([\d,]+(?:\.\d+)?)|\$\s?([\d,]+(?:\.\d+)?)\s?\+"
                         r"|\$\s?([\d,]+(?:\.\d+)?)\s+or less", text):
        a, b, plus, less = m.groups()
        f = lambda x: float(x.replace(",", ""))
        if a and b:
            ok = f(a) <= amount <= f(b)
        elif plus:
            ok = amount >= f(plus)
        else:
            ok = amount <= f(less)
        if ok:
            return 1.6
        best = 0.5
    return best


def coverage(view: View, qt: list[str], chunk_i: int) -> float:
    """Share of the question's specific (non-generic) words found in the chunk, weighted by rarity."""
    spec = specific_terms(qt)
    if not spec:
        return 1.0
    c = view.chunks[chunk_i]
    have_all = set(tokens(c.search))
    have_txt = set(tokens(c.text if c.kind == "para" else c.heading + " " + c.text))
    w_all = sum(view.idf.get(t, 3.0) for t in spec)
    if not w_all:
        return 1.0
    hit_all = sum(view.idf.get(t, 3.0) for t in spec if t in have_all) / w_all
    hit_txt = sum(view.idf.get(t, 3.0) for t in spec if t in have_txt) / w_all
    return 0.3 * hit_all + 0.7 * hit_txt


DATE_RE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?"
                     r"(?:,?\s*(?:19|20)\d\d)?\b|\b\d{1,2}/\d{1,2}/(?:19|20)?\d\d\b|\b(?:19|20)\d\d-\d\d-\d\d\b", re.I)


EXPIRED_RE = re.compile(r"\b(?:offer|promotion|promo|special|rate)?\s*(?:ended|expired|ends|expires|valid (?:through|until)|"
                        r"available (?:through|until))\s*(?:on\s*)?((?:19|20)\d\d-\d\d-\d\d|\d{1,2}/\d{1,2}/(?:19|20)\d\d|"
                        r"[A-Z][a-z]+\.? \d{1,2},? (?:19|20)\d\d)", re.I)
MONTH_NUM = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _iso(d: str) -> str:
    d = d.strip()
    if re.fullmatch(r"\d{4}-\d\d-\d\d", d):
        return d
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", d)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"([A-Za-z]+)\.? (\d{1,2}),? (\d{4})", d)
    if m and m.group(1)[:3].lower() in MONTH_NUM:
        return f"{m.group(3)}-{MONTH_NUM[m.group(1)[:3].lower()]:02d}-{int(m.group(2)):02d}"
    return ""


def expired(text: str, today: str) -> bool:
    """'Summer special ... (offer ended 2025-09-30)': an offer whose own end date has passed."""
    for m in EXPIRED_RE.finditer(text):
        d = _iso(m.group(1))
        if d and d < today[:10]:
            return True
    return False


def strip_dates(q: str) -> str:
    """'as of June 30, 2026' says *when*, not *what*; the 30 is not a 30-year anything."""
    return DATE_RE.sub(" ", q)


def rank(view: View, question: str, limit: int = 60):
    question = strip_dates(question)
    names = names_in(question)
    # "Is 0.35% available on Share Savings?": the figure is what to check, not what to search for
    question = re.sub(r"\d+(?:\.\d+)?\s?%", " ", question)
    qt = tokens(question)
    it = intent_of(question)
    spec = specific_terms(qt)
    ql = question.lower()
    raw = view.bm25(qt)
    brand = site_brand(view)
    # among re-publications of one story, the newest is the current one
    newest: dict = {}
    for i in raw:
        rec = view.chunk_page[i]
        if rec.published:
            fam = story_family(rec.url)
            if rec.published > newest.get(fam, ""):
                newest[fam] = rec.published
    out = []
    for i, s in raw.items():
        c = view.chunks[i]
        rec = view.chunk_page[i]
        f = authority(c.url)
        if not rec.present:
            f *= 0.6
        if rec.stale(view.taken_on):
            f *= 0.55
        if expired(c.text, view.taken_on):
            f *= 0.15
        if rec.published:
            fam_new = newest.get(story_family(rec.url))
            if fam_new and rec.published < fam_new:
                f *= 0.6
        path = re.sub(r"^https?://[^/]+", "", c.url.lower()).split("#")[0].split("?")[0].rstrip("/")
        if it.homes and any(path == h or path.startswith(h + "/") for h in it.homes):
            f *= 1.3
        cov = coverage(view, qt, i)
        if names:
            have = set(tokens(c.search))
            f *= 0.5 ** sum(1 for n in names if n not in have)
            if c.kind == "para" and brand and _cooccur(c.text, brand, names):
                f *= 1.3   # "Forbes ... named Dupaco" beats "Forbes recognized 207 credit unions"
        if it.eligibility and re.match(r"^/about/join\b", path):
            f *= 1.25
        if it.eligibility and re.search(r"eligib|who can|join if|membership requirement",
                                        c.heading.split(" > ")[-1], re.I):
            f *= 1.5   # the section the site titles "You're eligible to join if…"
        out.append((s * f * _fit(c, it, spec) * _qualifier_fit(c, ql) * (0.35 + 0.65 * cov), i))
    out.sort(key=lambda x: (-x[0], view.chunks[x[1]].url, x[1]))
    out = out[:limit]
    if it.count:
        # "how many branches": the figure the site repeats on many pages beats a one-off
        pat = count_pattern(spec)
        if pat:
            fig_pages: dict = {}
            for s, i in out[:30]:
                for fig in pat.findall(view.chunks[i].text):
                    fig_pages.setdefault(fig, set()).add(view.chunks[i].url)
            def bonus(i):
                figs = pat.findall(view.chunks[i].text)
                return max((len(fig_pages.get(f, ())) for f in figs), default=0)
            out = [(s * (1 + 0.3 * max(0, bonus(i) - 1)), i) for s, i in out]
            out.sort(key=lambda x: (-x[0], view.chunks[x[1]].url, x[1]))
    if it.phone:
        # the main number is the one printed on many pages
        ph_pages: dict = {}
        for s, i in out[:30]:
            for ph in PHONE_RE.findall(view.chunks[i].text):
                ph_pages.setdefault(re.sub(r"\D", "", ph), set()).add(view.chunks[i].url)
        if ph_pages:
            def pb(i):
                return max((len(ph_pages.get(re.sub(r"\D", "", p), ())) for p in PHONE_RE.findall(view.chunks[i].text)),
                           default=0)
            out = [(s * (1 + 0.2 * max(0, pb(i) - 1)), i) for s, i in out]
            out.sort(key=lambda x: (-x[0], view.chunks[x[1]].url, x[1]))
    if it.hours:
        # "general" hours: the schedule most branches share beats a one-off
        sig_pages: dict = {}
        for s, i in out[:20]:
            sig = time_signature(view.chunks[i].text)
            if sig:
                sig_pages.setdefault(sig, set()).add(view.chunks[i].url)
        out = [(s * (1 + 0.25 * (len(sig_pages.get(time_signature(view.chunks[i].text), ())) - 1)), i)
               if time_signature(view.chunks[i].text) else (s, i) for s, i in out]
        out.sort(key=lambda x: (-x[0], view.chunks[x[1]].url, x[1]))
    return out, qt, it


MONTHS_DAYS = set("""january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday today""".split())


def site_brand(view: View) -> str:
    """The organisation's own name, read off the most common host: www.dupaco.com -> 'dupaco'."""
    cached = getattr(view, "_brand", None)
    if cached is not None:
        return cached
    hosts: dict = {}
    for url in view.pages:
        h = re.sub(r"^https?://", "", url).split("/")[0].removeprefix("www.")
        hosts[h] = hosts.get(h, 0) + 1
    top = max(hosts, key=hosts.get) if hosts else ""
    view._brand = top.split(".")[0] if top else ""
    return view._brand


def _cooccur(text: str, brand: str, names: list[str]) -> bool:
    for snt in re.split(r"(?<=[.!?])\s+", text):
        sl = snt.lower()
        if brand in sl and any(n in set(tokens(snt)) for n in names):
            return True
    return False


def names_in(question: str) -> list[str]:
    """Capitalised words that are not the first word: branch names, product names, people."""
    out = []
    for m in re.finditer(r"\b([A-Z][a-z]{2,})\b", question):
        if m.start() == 0 or question[:m.start()].rstrip().endswith((".", "?", "!")):
            continue
        if m.group(1).lower() in MONTHS_DAYS:
            continue
        t = tokens(m.group(1))
        if t and t[0] not in GENERIC:
            out.append(t[0])
    return out


def missing_name(view: View, question: str, chunk_i: int) -> bool:
    """The question names something (e.g. 'Mullan Avenue branch') the chosen passage does not."""
    have = set(tokens(view.chunks[chunk_i].search))
    return any(n not in have for n in names_in(question))
