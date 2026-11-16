"""Print the questions the official scorer did not give full marks, with our answer.

    python dev/show_misses.py results/per_question.jsonl path/to/public_eval.jsonl
"""
import json
import sys

res = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
items = {json.loads(l)["id"]: json.loads(l) for l in open(sys.argv[2]) if l.strip()}
for r in res:
    if r["score"] >= 0.999 and "--all" not in sys.argv:
        continue
    it = items[r["id"]]
    a = r["answer"]
    print(f"{r['score']:.2f} {r['id']} [{r['category']}] {it['question']}")
    print(f"     need {r['missed']}  traps {r['trap_hits']}  src_hit={r['source_hit']}  conf={a['confident']}")
    print(f"     -> {a['text'][:400]!r}")
    print(f"     <- {[s['url'] for s in a['sources']]}")
