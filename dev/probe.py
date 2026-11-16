"""Debug helper (local only): ingest the snapshots once, then show the ranking for questions.

    python dev/probe.py SNAPSHOT_DIR "question" ["question" ...] [--as-of 2026-05-31]
"""
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solution"))
from answerer import coverage, rank  # noqa: E402
from kb_types import Page, Snapshot  # noqa: E402
from solution import build  # noqa: E402


def load(root):
    out = []
    for d in sorted(p for p in Path(root).iterdir() if (p / "manifest.json").exists()):
        m = json.loads((d / "manifest.json").read_text())
        pages = [Page(x["url"], x.get("title", ""), (d / x["file"]).read_text(encoding="utf-8"),
                      x.get("fetched_at", "")) for x in m["pages"]]
        out.append(Snapshot(m["snapshot_id"], m["taken_on"], pages))
    return out


args = [a for a in sys.argv[2:]]
as_of = None
if "--as-of" in args:
    k = args.index("--as-of")
    as_of = args[k + 1]
    del args[k:k + 2]
ks = build()
for s in load(sys.argv[1]):
    ks.ingest(s)
for q in [a for a in args if a != "--no-questions"]:
    view = ks.history.at(as_of)
    ranked, qt, it = rank(view, q)
    print("=" * 100)
    print(q, "| tokens", qt, "|", it)
    for s, i in ranked[:8]:
        c = view.chunks[i]
        print(f"  {s:7.2f} cov={coverage(view, qt, i):.2f} {c.kind:5} {c.url[23:80]:57} {c.text[:150]!r}")
    a = ks.answer(q, as_of)
    print("  ANSWER:", a.confident, a.text[:500], [x.url for x in a.sources])
