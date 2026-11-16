"""Print what conflicts() returns.  python dev/list_conflicts.py SNAPSHOT_DIR"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.argv.append("--no-questions")
from probe import ks  # noqa: E402  (probe ingests on import)
for c in ks.conflicts():
    print(f"- {c.topic} | {c.values} -> {c.resolution} | {[s.url[23:70] for s in c.sources]}")
    print(f"    {c.reason}")
print(len(ks.conflicts()), "conflicts")
