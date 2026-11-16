# Exercise 01: Small-model benchmark

Compare two small open-weight LLMs on one structured-extraction task: recipe
cleaned recipe-page text in, a list of ingredients out. See [PLAN.md](PLAN.md) for the experiment
design and [data/DATASET.md](data/DATASET.md) for what the dataset is and what
its labels are (and are not).

## Layout

```text
configs/    one file per experiment
data/
pipeline/
prompts/
runs/       results, one folder per experiment
src/        run, evaluate and report code
viewer/     optional results viewer
```

## Dataset row

Each line of `dataset.jsonl` is one JSON object, shown here pretty-printed:

```json
{
  "id": 12,
  "page_url": "https://example.com/recipe",
  "input": "# Recipe title\n- 185 grams all purpose flour\n...",
  "expected": [
    {
      "name": "all purpose flour",
      "amount": 185,
      "unit": "grams",
      "original_text": "185 grams all purpose flour"
    }
  ]
}
```
