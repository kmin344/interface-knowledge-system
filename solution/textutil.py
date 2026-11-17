"""Tokenising, number handling and a few domain synonyms.

Everything here is deterministic: the same question always produces the same tokens,
which is what makes answers repeatable (the consistency pillar).
"""
from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z0-9]+(?:[./][0-9]+)?")
NUM_RE = re.compile(r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent)?", re.I)

STOP = set("""
a an the of for to in on at by and or is are was were be been do does did can could would should
will what whats which who whom how when where why i me my we our you your it its this that these
those there their they them with from as about any some much many tell please give provide get
know like want need current currently right now today dupaco dupacos credit union s also than
just have has had if into out up get let lets am pm vs per
""".split())

# Words that make a question about a *specific* thing. Used by the refusal check.
GENERIC = STOP | set("""
rate rates interest apr apy percentage annual yield dividend price cost fee fees much offer offered
offering account accounts information info number amount current latest still open available
general highest lowest best top maximum minimum max min between option options flexibility flexible
recognition received get getting see find standard regular typical normal
ongoing usual going new keep same over under finance financing borrow would use using mid early late
january february march april may june july august september october november december
charging charge charges day days these those pay paying pays earn earning earns list lists show shows
webpage page tell please
""".split())

SYN = {
    "cd": "certificate", "cds": "certificate", "certificates": "certificate", "certificat": "certificate",
    "share": "share", "car": "auto", "cars": "auto", "vehicle": "auto", "vehicles": "auto",
    "automobile": "auto", "mortgages": "mortgage", "mo": "month", "months": "month", "mos": "month",
    "yr": "year", "years": "year", "yrs": "year", "six": "6", "twelve": "12", "eighteen": "18",
    "branches": "branch", "locations": "location", "hrs": "hours", "hour": "hours",
    "cards": "card", "loans": "loan", "savings": "savings", "checkings": "checking",
    "visa": "visa", "atms": "atm", "url": "link", "website": "link", "webpage": "link",
    "phone": "phone", "telephone": "phone", "employees": "employee", "staff": "employee",
    "dividends": "dividend", "fees": "fee", "charges": "fee", "charge": "fee",
    "conventional": "mortgage",
    "illinois": "il", "iowa": "ia", "wisconsin": "wi",
    "ten": "10", "fifteen": "15", "twenty": "20", "thirty": "30", "forty": "40", "sixty": "60",
    "seventeen": "17", "thirtysix": "36", "fortyeight": "48", "seventytwo": "72", "eightyfour": "84",
    "membership": "join", "signup": "join", "enroll": "join",
    "drivethru": "drive", "drivethrough": "drive", "thru": "drive",
}


SUFFIXES = ("ations", "ation", "itions", "ition", "izing", "ized", "izes", "ize", "ing", "ed")


def stem(w: str) -> str:
    w = SYN.get(w, w)
    if len(w) > 6 and not w[0].isdigit():
        for suf in SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not w.isdigit():
        w = w[:-1]
    return SYN.get(w, w)


GENERIC = GENERIC | {stem(w) for w in GENERIC}


def tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("-", " ").replace("–", " ")
    out = []
    for w in WORD_RE.findall(t):
        if w in STOP or (len(w) < 2 and not w.isdigit()):
            continue
        out.append(stem(w))
    return out


def numbers(text: str) -> set[tuple[str, float]]:
    """Same parsing idea as the scorer: (unit, value) pairs."""
    out = set()
    for m in NUM_RE.finditer(text or ""):
        unit = "$" if m.group(1) else ("%" if m.group(4) else "")
        out.add((unit, round(float(m.group(2).replace(",", "") + ("." + m.group(3) if m.group(3) else "")), 4)))
    return out


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()
