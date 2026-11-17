"""Snapshot history, page authority and a BM25 index per point in time."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse

from chunker import Chunk, chunk_page
from textutil import tokens


def authority(url: str) -> float:
    """How much we trust a page to state *current* product facts.

    Rate sheets and product pages are maintained; blogs, press releases, calculators and old
    PDFs are written once and then left alone, so their numbers go stale.
    """
    p = urlparse(url.lower())
    host, path = p.netloc.removeprefix("www."), p.path
    if host.startswith("info.") or host.startswith("blog."):
        return 0.3                       # legacy marketing subdomain
    if path in ("", "/"):
        return 0.9
    if re.match(r"^/(rates|business-rates)", path):
        return 1.0
    if "calculator" in path:
        return 0.3
    if re.match(r"^/20\d\d/", path):
        return 0.35                      # dated legacy blog posts
    if path.startswith("/learn/") or re.match(r"^/((business|personal)/)?resources/", path):
        return 0.45
    if path.startswith("/newsroom"):
        return 0.5
    if path.startswith("/hubfs/"):
        return 0.55
    if re.match(r"^/(personal|business|about|contact|checking|savings|join|"
                r"first-time-homebuyer|personal-loans|financial-services|digital)", path):
        return 1.0
    return 0.8


MONTHS = {m: i + 1 for i, m in enumerate("january february march april may june july august september "
                                         "october november december".split())}
PUB_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                    r" (\d{1,2}), (20\d\d)\b")


def published_date(url: str, markdown: str) -> str:
    """The date a press release or blog post prints just under its title (first few lines)."""
    if not re.search(r"/(newsroom|learn|blog)/|/20\d\d/", url):
        return ""
    head = "\n".join([l for l in (markdown or "").splitlines() if l.strip()][:8])
    m = PUB_RE.search(head)
    if not m:
        return ""
    return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def story_family(url: str) -> str:
    """'.../forbes-ranks-dupaco-...-america-3' and '...-america-2' are the same story re-published."""
    return re.sub(r"-\d+/?$", "", url.rstrip("/"))


@dataclass
class PageRec:
    url: str
    title: str
    snapshot_id: str
    taken_on: str
    chunks: list[Chunk]
    present: bool = True          # in the snapshot this view belongs to (False = carried over)
    effective: str = ""           # newest "rates effective as of" date stated on the page
    published: str = ""           # press release / blog date printed under the title

    def stale(self, today: str) -> bool:
        """The page's own rate table says it is more than a year old."""
        if not self.effective:
            return False
        y, m, d = (int(x) for x in self.effective.split("-"))
        ty, tm, td = (int(x) for x in today[:10].split("-"))
        return (ty - y) * 12 + (tm - m) > 12


class View:
    """The website as it stood at one snapshot: every page at its newest version so far."""

    K1, B = 1.4, 0.75

    def __init__(self, snapshot_id: str, taken_on: str, pages: dict[str, PageRec]):
        self.snapshot_id, self.taken_on = snapshot_id, taken_on
        self.pages = pages
        self.chunks: list[Chunk] = []
        self.chunk_page: list[PageRec] = []
        for rec in pages.values():
            for c in rec.chunks:
                self.chunks.append(c)
                self.chunk_page.append(rec)
        self.post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.dl: list[int] = []
        for i, c in enumerate(self.chunks):
            tf = Counter(tokens(c.search))
            self.dl.append(sum(tf.values()))
            for t, k in tf.items():
                self.post[t].append((i, k))
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 1.0
        n = len(self.chunks)
        self.idf = {t: math.log(1 + (n - len(ps) + 0.5) / (len(ps) + 0.5)) for t, ps in self.post.items()}

    def bm25(self, qtoks: list[str]) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for t in set(qtoks):
            idf = self.idf.get(t)
            if not idf:
                continue
            for i, k in self.post[t]:
                dl = self.dl[i]
                scores[i] += idf * k * (self.K1 + 1) / (k + self.K1 * (1 - self.B + self.B * dl / self.avgdl))
        return scores


class History:
    """Keeps every version of every page, keyed by the snapshot it was seen in."""

    def __init__(self):
        self.views: list[View] = []
        self._latest: dict[str, PageRec] = {}
        self._cache: dict[str, list[Chunk]] = {}

    def add(self, snapshot) -> None:
        present = set()
        for pg in snapshot.pages:
            h = hashlib.sha1((pg.url + "\0" + pg.title + "\0" + pg.markdown).encode("utf-8", "ignore")).hexdigest()
            if h not in self._cache:
                self._cache[h] = chunk_page(pg.url, pg.title, pg.markdown)
            eff = max((c.effective for c in self._cache[h] if c.effective), default="")
            self._latest[pg.url] = PageRec(pg.url, pg.title, snapshot.snapshot_id, snapshot.taken_on,
                                           self._cache[h], True, eff, published_date(pg.url, pg.markdown))
            present.add(pg.url)
        view_pages = {}
        for url, rec in self._latest.items():
            view_pages[url] = PageRec(rec.url, rec.title, rec.snapshot_id, rec.taken_on, rec.chunks,
                                      url in present, rec.effective, rec.published)
        self.views.append(View(snapshot.snapshot_id, snapshot.taken_on, view_pages))
        self.views.sort(key=lambda v: v.taken_on)

    def at(self, as_of: str | None) -> View | None:
        if not self.views:
            return None
        if not as_of:
            return self.views[-1]
        best = None
        for v in self.views:
            if v.taken_on <= as_of[:10]:
                best = v
        return best or self.views[0]
