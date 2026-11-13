# Exercise 01: Small-model benchmark

Compare two small open-weight LLMs on the same structured-output task, and recommend one on accuracy, cost and latency.

Role being practised: applied AI engineer / forward deployed AI engineer.

## Goal

Given a dataset of text inputs with expected structured outputs, answer:

- Which model is more accurate, and on which kinds of input does each fail?
- What does each cost per 1,000 requests at a realistic load?
- How does each compare with a hosted frontier model as a reference point?

The deliverable is a one-page recommendation written for an imaginary customer, backed by the results files.

## Recommendation criteria

The recommendation depends on three things, in this order:

1. **Accuracy.** Set a minimum bar before running (for example, a schema-valid rate and a per-field accuracy the customer could live with). A model below the bar is not recommended, however cheap or fast it is.
2. **Cost** per 1,000 requests, among the models that clear the bar.
3. **Latency**, among the models that clear the bar.

If the accuracy gap between the two models is inside the confidence intervals, treat them as tied on accuracy and decide on cost and latency. If neither clears the bar, the recommendation says so and points to the follow-up experiments or the reference baseline.

## Principles

- **Scripts and config files, not a UI.** One config file defines an experiment. One command runs it end to end. The script enforces the order of steps, and the config is the record of what ran.
- **Run and evaluate are separate.** The runner writes raw outputs to disk. The evaluator reads them. Scoring can change without paying for GPU time again.
- **Teardown always happens.** The pod is stopped when the run finishes or fails.
- **Rigor is traded for experience where it is cheap to do so.** One GPU pod, models served one after the other, files instead of a database.

## Layout

```
01-small-model-benchmark/
  PLAN.md
  README.md               how to run, in order
  configs/
    exp01.yaml            one file per experiment
  data/
    dataset.jsonl         input + expected output per line
  prompts/
    base.txt
  src/
    provision.py          launch pod, wait for health, tear down
    run.py                send dataset to an endpoint, write raw outputs
    evaluate.py           score raw outputs against expected
    report.py             aggregate results across models
    run_experiment.py     entry point: provision -> run -> evaluate -> teardown
  runs/
    exp01/
      config.snapshot.yaml
      <model>/
        outputs.jsonl     one line per request
        metrics_before.txt
        metrics_after.txt
        results.json
      summary.md
  viewer/                 optional, last: HTML page to browse results
```

## Experiment config

Each config file holds everything needed to reproduce a run:

- Dataset path and prompt file
- Output JSON schema
- Models: Hugging Face ID, plus vLLM launch arguments (for example `--max-model-len`)
- Sampling settings: temperature 0, max tokens, seed
- Whether constrained JSON decoding is on
- Concurrency level for the runner
- Provider settings: GPU type, hourly price, region

## Pipeline

`python src/run_experiment.py configs/exp01.yaml` does the following for each model in turn:

1. **Provision.** Launch a pod from the stock `vllm/vllm-openai` image with the model and launch arguments from the config. No custom Docker images.
2. **Wait for health.** Poll the server until it reports ready. Abort and tear down on timeout.
3. **Snapshot metrics.** Save `GET /metrics` to `metrics_before.txt`.
4. **Run.** Send every dataset row to the OpenAI-compatible endpoint. Write one line per request to `outputs.jsonl`.
5. **Snapshot metrics again.** Save to `metrics_after.txt`.
6. **Tear down.** Stop the pod, in a `finally` block so it runs on failure too.
7. **Evaluate.** Score `outputs.jsonl` and write `results.json`.

After all models: `report.py` writes `summary.md` comparing them.

## What gets recorded

Per request, in `outputs.jsonl`:

- Row ID, raw model output, parsed output (or the parse error)
- `prompt_tokens` and `completion_tokens` from the response's `usage` object
- Client-side latency, and time to first token if streaming
- Any request error

Per model run:

- The difference between the two `/metrics` snapshots: server-side latency, time to first token, queue time, tokens generated. Check metric names against the running server; they vary by vLLM version.
- Wall-clock duration of the run and the pod's hourly price

## Evaluation

Scored separately, not as one number:

- **Schema-valid rate:** does the output parse and match the schema?
- **Per-field accuracy:** which fields are right and wrong?
- **Exact match:** is the whole object correct?

Also:

- **Confidence intervals** on each rate, so a small gap is not over-read.
- **Failure analysis:** read the wrong outputs, sort them into categories, and count each. This goes in the write-up.
- **Cost:** hourly price x run duration, divided by requests, reported at the concurrency used. Cost per request depends on throughput, so state the load.

## Reference baseline

Run the same dataset and prompt through a hosted frontier model's API using the same `run.py` (it only needs an OpenAI-compatible endpoint, or a thin adapter). This anchors the small models' accuracy and cost.

## Build order

1. `run.py` and `evaluate.py` against any OpenAI-compatible endpoint, with a handful of dataset rows.
2. Launch vLLM on a pod by hand. Run both models. Check the numbers look sane.
3. `provision.py`: launch and teardown through the provider's API.
4. `run_experiment.py`: tie the steps together.
5. Failure analysis and the one-page recommendation.
6. Optional: HTML results viewer.

## Follow-up experiments

Each is a new config file, not new code:

- Prompt variants on the same model
- Constrained decoding on versus off
- Different concurrency levels, for the cost and latency curve
- Base model versus a fine-tuned LoRA adapter of itself, served by vLLM

## Open decisions

- Provider: RunPod or Vast.ai
- The two models to compare
- GPU type, which sets the size of model that fits
- The minimum accuracy bar for a model to be recommended
