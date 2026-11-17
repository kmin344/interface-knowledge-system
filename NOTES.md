# NOTES

A knowledge system that answers credit-union questions with the value that is true **now**
(or on an `as_of` date), from the right tier, with a source, and says "I don't know" when the
site doesn't say.

**API keys: none.** Standard library only (Python 3.9+). No network calls. Ingest of all five
snapshots takes about 6 seconds; answers take 0.2–0.8 s (p95 well under the 2 s mark).

## Results on the kit

| Run | Score |
|---|---|
| `starter` (official scorer, public 40) | 0.093 |
| this solution (official scorer, public 40) | **0.915** accuracy, consistency 1.0, speed 1.0 (p95 ≈ 0.8 s), 0 stale-trap hits |
| `harness/selftest` (with conflicts gold) | conflicts F1 1.0, resolution 1.0 |

Because the hidden set rewords questions, most of the tuning was done against questions I wrote
myself and scored with the official scorer: a second phrasing of each of the 40 public questions
(0.74 → **0.95**), 30 more rewordings of the same facts (**0.97**), and 30 casual / new-tier /
`as_of` / not-on-the-site questions (**0.80**). Zero stale-trap hits on all of them. They live
outside this repo because they quote the data.

## How it works

```
ingest(snapshot)          chunk every page -> keep history -> one BM25 index per point in time
answer(q, as_of)          pick the view for as_of -> rank chunks -> refuse or compose -> cite
conflicts()               stale rate copies + rate changes across snapshots + self-contradictions
```

**History.** Every snapshot is kept. The view for a date is every page at its newest version on
or before that date. A page missing from the newest download is kept but down-weighted, so a
fact that only lived on a removed page is still answerable, but never beats a live page.

**Chunking** (`chunker.py`). A table row is its own chunk and carries its caption, section
heading and column names, so "60 Months | 5.39%" stays attached to "2024-27 Model Year" and
"APR". The whole table is also kept for "list everything" questions. Short lines (hours,
addresses) that the crawler split with blank lines are merged back together.

**Ranking** (`answerer.py`): BM25, multiplied by:

| Factor | Why |
|---|---|
| Page authority | Rate sheets and product pages are maintained. Calculators, blogs, press releases, old PDFs and the legacy `info.` subdomain are written once and go stale. |
| Freshness | A page whose own "rates effective as of" date is over a year old, or an offer whose own end date has passed, is heavily demoted. |
| Intent fit | A rate question wants a rate row; an hours question wants times; an address question an address; "how many" a number next to the noun asked about. |
| Tier fit | Model year 2025 falls in "2024-27", not "2021-22"; $15,000 falls in "$10,000 – $24,999.99"; a "Rewards Visa" question is not about the "Business Rewards Visa". |
| Coverage | How many of the question's specific words the passage itself contains, weighted by rarity. |
| Consensus | For hours, phone numbers and counts, the value the site repeats on many pages beats a one-off. |
| Re-publication | When a press release exists as `story`, `story-2`, `story-3`, the newest printed date wins. |
| Names and negation | A passage that lacks a name the question uses ("Mullan Avenue") is demoted; "(not jumbo)" rules jumbo rows out. |
| Subject | A sentence where the question's name and the site's own name meet ("Forbes … named Dupaco") beats one about the name alone ("Forbes recognized 207 credit unions"). Award questions prefer the highest "Nth time". |
| Eligibility | "Who can join" prefers the section the site titles "eligible to join", and adds the one line that lists where it operates ("in Iowa, Illinois and Wisconsin"), which shares no words with the question. |

**Answering.** The answer is the passage itself, never a paraphrase, so every number in it is on
the cited page. Tier questions bring along sibling rows from the same table; "what cards do you
offer" brings the best table from each trusted page. Other trusted pages that state the same
figures are cited too.

**Refusing.** "I'm sorry, that isn't listed…" with no numbers when:
- a rate is asked for and the best passage is not a rate statement on a trusted page;
- the product term asked for (40-year, 7/1, 60-month jumbo) is not on the passage;
- the question names something (a branch, a product) the passage does not mention;
- or nothing covers the question's specific words.

## What "current" means (the rules, and the question that taught each one)

1. Newest snapshot on or before `as_of`; with no date, the latest. *(10-month jumbo special:
   3.90% in May, 4.00% now)*
2. An official rate table beats any other page that quotes a rate. *(auto 60-month: 5.39% on the
   rate table vs 5.34% on a calculator and a promo page)*
3. A page that dates its own rates more than a year back is stale, even on the main domain.
   *(the switch-and-earn page says "effective as of 10/22/2024")*
4. An offer whose stated end date has passed is not current. *(selftest summer special)*
5. Tiers are different facts, not conflicts. Lead with the tier the question names; otherwise show
   the tiers side by side. *(17-month special 4.10% vs jumbo 4.20%; Share Savings by balance)*
6. When the site repeats a fact about itself on many pages, the majority wins over one-offs.
   *(branch count; general lobby hours; the main phone number)*
7. Recognitions accumulate: "fifth time" supersedes "fourth time", and the newest re-publication
   of a story supersedes older ones.
8. If the site doesn't say, say so. *(7/1 ARM, construction-loan rate, Mullan Avenue branch)*

## conflicts()

Three narrow detectors, because every false entry costs precision:
1. **Stale rate copies:** a rate on a lower-authority or stale page that is not among the official
   table's values for the same product and term. Lined up with the official tier it was the old
   version of (model-year overlap), grouped per page so one stale table is one conflict.
2. **Rate changes across snapshots:** an official row whose value changed between downloads.
   Winner: the newest value.
3. **Self-descriptions that disagree:** branch counts across press releases (winner agrees with
   what `answer()` says) and ordinal recognitions ("fourth" vs "fifth" time).

## Known limits

- No language model, so answers are extracts, not prose. A question whose answer is spread
  across several pages (e.g. charter-area counties shown only as an image) can be missed.
- Topic-home hints (`TOPIC_HOME` in `answerer.py`) and the authority map are specific to how this
  credit union's site is organised. They are a few lines each and are the first thing to revisit
  for another site.
- A paraphrase that uses a word the site never uses ("CD", "drive-thru", "thirty-year" work;
  rarer slang may not) falls back to plain BM25.
- Public misses I know about: the Volunteer-Time-Off count prefers the 2024 article over the 2023
  one the eval expects ("last year" is ambiguous, and newest-wins is the rule elsewhere);
  "do any tiers pay 0.35%?" needs a yes/no judgement an extractive answer cannot make; dup-005
  has no expected values, so the scorer gives it 0 whatever the answer.

## Layout

```
solution/            pass this folder to --submission
  solution.py        build() and the three contract methods
  chunker.py         markdown -> chunks
  store.py           snapshot history, page authority, BM25 per view
  answerer.py        intent, ranking, refusal rules
  conflicts.py       the three conflict detectors
  textutil.py        tokens, synonyms, number parsing
  kb_types.py        contract types, used only when harness/contract.py is not importable
dev/                 local tools (not used by the scorer)
```

Run: `python harness/run_eval.py --submission <this repo>/solution --eval eval/public_eval.jsonl`
from the kit folder.
