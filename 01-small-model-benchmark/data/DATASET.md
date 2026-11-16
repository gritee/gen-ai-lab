# Dataset card: recipe ingredient extraction

**Task.** Given a recipe web page's visible text (schema.org markup removed, page
cleaned down to text, see below), return its
ingredients as a list of `{name, amount, unit, original_text}`, in page order.

## Source

- Page list: Web Data Commons schema.org Recipe table, release 2023-12, part 0
  (`data/raw/wdc_2023-12_Recipe_part_0.csv`, 62 MB, not committed). Confirm the
  exact download URL and license terms at webdatacommons.org before sharing.
- Pages: fetched 2026-11-13 to 2026-11-15 with a Playwright scraper
  (`pipeline/fetch_pages.py`), saved untouched as rendered HTML (434 MB, not committed).
- The recipe pages are third-party content. This dataset is for private
  experimentation; do not redistribute `dataset.jsonl`.

## How it was built (`pipeline/build_dataset.py`)

| Step | Result |
|---|---|
| WDC rows fetched | 6,172 pages saved |
| No schema.org Recipe JSON-LD on the page | 670 dropped |
| Not English (`inLanguage`, else `langdetect` on the ingredient lines), no ingredients, or ingredient lines not found in the visible page | 2,917 dropped |
| An ingredient line that cannot become one name/amount/unit (headings such as "For the crust:", composite amounts like "1 pound 2 ounces", word quantities like "a few") | 178 dropped, whole recipe |
| Ingredient lines present in the page but lost when cleaning it to text | *rerun to fill in* |
| More than one recipe on the page | 8 dropped |
| **Kept** | **2,354 rows, 23,746 ingredient lines** |

Dataset hash and counts: `data/manifest.json`.

`input` is the page cleaned in two steps, both in `build_dataset.py`:

1. Schema.org markup removed: JSON-LD blocks, microdata (`itemscope`, `itemprop`,
   ...) and RDFa attributes, so the answer is not sitting in the page.
2. Reduced to visible text with lxml (`html_to_text`): head, script, style,
   svg, nav, footer, aside, form, buttons, images, comments and all attributes
   dropped; one block per line, list items as `- `, headings as `# `. A recipe is
   kept only if every ingredient line survives this step.

Raw pages ran to ~100,000 tokens, past the context of small models, which is why
the input is text and not HTML. This is the "scrape, then ask the LLM" setup: a
real pipeline would do the same reduction before calling a model. The cleaner is
a plain tag-based strip, not trafilatura or readability, which are tuned for
article bodies and can drop recipe cards.

## Labels: read this before trusting a score

- **The labels are not ground truth.** They are `ingredient-parser-nlp` 2.10.0's
  reading of the page's own `recipeIngredient` strings. Nobody checked them. A
  model that scores 100% agrees with the parser, not necessarily with the recipe.
- `name`: the parser's ingredient name, lowercased as written (no singularizing,
  no merging of "butter" and "unsalted butter").
- `amount`: a number, or null when the line has no amount ("salt to taste").
  Ranges ("2-3 cups") use the lower bound (573 lines).
- `unit`: the unit as written ("cups", "cloves"), not normalized, or null
  (including count items like "2 large eggs"). 18,374 of 23,746 lines have one.
- **Lines with several amounts** ("1 container (8 oz) ...") use the first amount
  in text order (1,921 lines, 8%). This is the noisiest part of the labels.
- Preparation notes ("diced", "thawed") are not extracted.
- `original_text` is the line from the page's JSON-LD, whitespace-cleaned. Every
  line was checked to appear in the cleaned `input` text.

Unit synonyms (`tbsp` vs `tablespoons`) are not normalized in the labels;
that is left to scoring.

## Known limits

- **English-only**, and biased toward whatever sites WDC and the scraper reached.
- **Input size.** Raw HTML was a median 411,000 characters (about 100,000 tokens),
  so inputs are cleaned text instead; current sizes are in `data/manifest.json`
  (`input_chars`). Count tokens with the target model's tokenizer and decide a
  context budget before running; some pages may still exceed it, and those
  should be counted and reported, not silently truncated.
- **Selection bias.** Every kept page had usable JSON-LD, and its ingredient
  lines were found in the page text. Pages without good markup, the case where
  an LLM is most needed in practice, are not represented. The dataset is
  easier than the open web.
- Only the ingredient list is labelled, not the rest of the recipe.
- Pages that publish several recipes, or an ingredient list that differs from the
  page text, are excluded.
- The row counts above predate the cleaning step and will shift when
  `build_dataset.py` is rerun; update them and regenerate `manifest.json`.
