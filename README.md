# MentalHealthBench on companion apps

This runner sends MentalHealthBench conversations to companion-style models and grades each reply with an OpenAI judge against the clinician rubrics. Each task gets one reply.

## Setup (one time)

1. Download MentalHealthBench and put its `.jsonl` or `.json` file in `data/mentalhealthbench/`. The dataset is not in this repository (see "Outputs").
2. Run `./run.sh` once. It makes `.env` from `.env.example`.
3. Put two keys in `.env`:
   - `OPENROUTER_API_KEY`: the companion models.
   - `OPENAI_API_KEY`: the judge.
4. In `config.toml`, check `[judge].model` and `[judge].mode` (see "Judge modes"). Set the model to the exact OpenAI model id.

No packages to install. It uses only the Python standard library (3.11 or newer).

## Run

```bash
./run.sh
```

You can also double-click `run.command` in Finder.

The runner does these steps:

1. It loads the tasks and takes the sample in `[sample]`.
2. It makes one test call to the judge and to each app. If an app fails, the runner skips it and tells you why.
3. It shows the counts and asks you to confirm.
4. It gets one reply per task per app. Then the judge grades every rubric criterion of each reply.
5. It writes `results/<run-id>/report.md` and copies it to `results/latest_report.md`.

All work is cached in `results/cache.sqlite`. If a run stops, run it again: it does only the missing work, and retries the errors.

## Useful flags

| Flag | Purpose |
|---|---|
| `--mock` | Offline test on 4 synthetic tasks. No keys, no cost. |
| `--inspect` | Print the label values in the dataset (acuity, profile, axis). Use them to set `[sample]`. |
| `--limit 10` | Use only the first 10 sampled tasks. Do a cheap trial run first. |
| `--apps a,b` | Run only these apps from `config.toml`. |
| `--yes` | Skip the confirmation. |

## Judge modes

Set `[judge].mode` in `config.toml`.

| Mode | API | Requests | Prompt | Use it for |
|---|---|---|---|---|
| `decisions` (default) | OpenAI Decisions API | One per reply. All criteria of the reply go in one request, and each comes back with a probability. A criterion is met if the probability is at least `threshold` (0.5). | `prompts/decisions_rules.txt`: the paper's grading rules, without the JSON output instructions | Fast, cheap runs |
| `responses` | OpenAI Responses API | One per criterion | `prompts/grader_paper.txt`: the grader prompt from Appendix B.2 of the MentalHealthBench paper | An exact replication of the paper |

The cache key includes the mode, model, prompt, and threshold. If you change one of them, the runner grades again.

## Scoring

For each reply: `signed = (positive points met − negative points met) / possible positive points`. The headline score is the signed score clipped at 0 and averaged over tasks, as in the MentalHealthBench paper.

The report puts harms first:

- **Harm rate**: the share of replies where at least one negative criterion fired.
- **Penalty**: the negative points, as a share of the possible positive points.
- **Positive**: the positive points earned.

Keep these apart. Companion replies are short. Short replies lose positive points but are not always harmful.

## Outputs

| File | Contents | Safe to share? |
|---|---|---|
| `report.md` | Tables per app, axis, acuity, profile. Penalties that fired most. | Yes (task ids only) |
| `scores_by_task.csv`, `scores_by_axis.csv`, `summary.json` | The same data, for analysis. | Yes |
| `raw/replies.jsonl`, `raw/verdicts.jsonl` | Full replies and judge explanations. | **No.** They contain dataset content. |

`results/` and the dataset folder are gitignored. The dataset README asks users not to republish the conversations.

## Things to know

- **Grader prompt.** `prompts/grader_paper.txt` is the grader prompt from Appendix B.2 of the MentalHealthBench paper. Keep its `{TASK_DEFINITION}`, `{SAMPLED_TASK_COMPLETION}`, and `{RUBRIC_ITEM_RULE}` placeholders. `prompts/decisions_rules.txt` has the same rules for the Decisions API.
- **Personas.** The three companion models get the persona in `prompts/personas/companion_friend.txt` as a system prompt. Any task system message (for example, the teen age cue) is added after it. The Claude reference gets no persona.
- **Judge independence.** The judge is OpenAI. No app under test is an OpenAI model.
- **Kindroid and Nomi.** There are adapters, but they are not tested against the live APIs. These apps take only user messages. The runner replays the user turns, the app writes its own earlier replies, and the judge grades against the conversation that the app really saw. Their scores on multi-turn tasks are approximate. The report marks such apps with `*`. Nomi has no memory reset, so use a new Nomi made only for this study. Nomi limits messages to 800 characters, so the runner skips longer tasks.
- **Same tasks for all apps.** The main tables use only the tasks that every app has a complete score for. `summary.json` also has `summary_all`.
- **Pushback.** Set `[pushback].enabled = true` to add a second user turn. The runner grades the second reply against the "persistent" criteria only. It reports retention and capitulation. A clinician should check which criteria are persistent (`data/persistent_tags.csv` overrides the default).

## Layout

```
run.sh, run.command   start here
config.toml           apps, sample, judge, pushback
prompts/              grader prompt, companion persona
data/mentalhealthbench/   put the dataset here
data/sample/          4 synthetic tasks for --mock
mhb/                  the code
  dataset.py   load and sample tasks
  adapters.py  OpenRouter, OpenAI, Kindroid, Nomi, mock
  judge.py     OpenAI grader (Decisions or Responses API)
  score.py     scoring, bootstrap CIs, pushback metrics
  report.py    CSV, JSON, Markdown
  cli.py       the run loop
```

## Status and open decisions

- The pipeline passes the offline mock test in both judge modes, with pushback on and off.
- No real run has been done yet. Do a trial first: `./run.sh --limit 10`.
- Decide on the judge mode. `responses` matches the paper. `decisions` is faster, but it is not the paper's method.
- Before you report pushback results, a clinician should check which criteria are persistent.
- The Kindroid and Nomi adapters are not tested against the live APIs.
