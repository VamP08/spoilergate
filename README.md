# SpoilerGate

Ask anything about a TV show you're partway through, and get an answer built only from
episodes you've already watched.

Set your position — "I'm about to watch S02E05" — and every answer, recap and character
page is limited to what the show has revealed by then. The limit is enforced by the
retrieval query, not by instructing a model to be careful.

## Why it works this way

A model that has read the internet knows how the show ends. Asking it not to say is a
request, and requests fail quietly and unpredictably. So there are three gates, and only
the last one involves the model at all:

1. **Question shape.** "Does she die?", "is he still around by the end?" — refused before
   anything is retrieved. The summaries record what *has* happened, never what hasn't, so
   an answer claiming someone is safe is unsupported no matter what comes back.
2. **Retrieval.** `WHERE abs_order <= your_position`. The model never receives a summary
   from an episode you haven't reached.
3. **Output scan.** The answer is checked against every character and place the show
   introduces later. A name from your future blocks the answer.

Where the third gate has no data — some shows link too few names to index — the answer
says so rather than implying a guarantee that isn't there.

A character you haven't met yet gets the same response as one who doesn't exist. Telling
you a name is unknown *yet* would confirm they show up.

## What it does

- **Ask** — free-form questions, answered from your watched range, with the episodes cited.
- **Previously on…** — a recap that stops exactly where you did.
- **Characters** — only the people you've met, each with a profile built from your range.

## Running it

```bash
conda create -n spoilergate python=3.12 -y --override-channels -c conda-forge
conda activate spoilergate
pip install -r requirements.txt

cp .env.example .env          # add at least one provider key
python -m ingest.build_db "Breaking Bad" "Severance"
python -m ingest.build_db --entities
uvicorn server.app:app
```

With no keys at all it still runs: answers become the gated episode summaries themselves,
which is less pleasant to read and exactly as spoiler-safe.

`python -m ingest.build_db --top 1500` builds the full index from TVMaze's popularity
ranking. It takes hours, and it is resumable — rerun the same command.

## Where the data comes from

Episode ordering from [TVMaze](https://www.tvmaze.com/api), episode summaries from
Wikipedia, both CC BY-SA and attributed per episode in the index. Characters are dated
without a model: Wikipedia links a character on first mention inside an episode summary,
and TVMaze's cast list supplies the leads that episode tables never link.

Shows outside the prebuilt index are indexed on demand, through the same pipeline, in a
few seconds.

## How well it works

The gate is measured, not asserted. Every entity knows the episode it first appears in, so
each one generates a question whose correct behaviour is known: asked one episode earlier
it must be refused; asked on the reveal episode it should be answered. Reporting both
matters — a gate that never leaks because it never answers anything is not a product.

Answers are recorded with the output scan **disabled** and scored afterwards. Scoring
guarded output with the same scan the guard uses would be circular and would report a
perfect result by construction.

<!-- RESULTS -->

Numbers come from `python -m eval.run` and `python -m eval.score`; runs are saved as JSONL
so rescoring costs nothing.

## Known limits

- Coverage is uneven. 832 of the 1,499 most popular shows have Wikipedia episode
  summaries; the rest are largely reality, talk and non-English series. Search hides shows
  that can't answer anything.
- Character extraction suits serialised fiction. Sketch and reality shows link real
  people constantly, so their "characters" are mostly guests.
- Some organisations still read as characters when their name looks like one.
- Vague questions ("what's the biggest twist?") have no name to retrieve on, so the
  answer is only as good as whichever episodes rank highest.
- The eval measures spoiler leaks well. It does not measure whether an answer is wrong
  about an episode you *have* watched that retrieval didn't return.
