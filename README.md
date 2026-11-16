# Knowledge system that knows what's true *now*

My solution to the interface.ai knowledge hackathon assignment. Design, rules and results are in
[NOTES.md](NOTES.md).

- `solution/` is the folder to pass to the scorer's `--submission`.
- No API keys and no third-party packages.
- The supplied website snapshots are not included in this repository.

```bash
# from the participant kit folder
python harness/run_eval.py --submission /path/to/this/repo/solution --eval eval/public_eval.jsonl
```
