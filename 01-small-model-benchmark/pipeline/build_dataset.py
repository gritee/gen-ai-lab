#!/usr/bin/env python3
"""Stage 2: turn the fetched pages into the exp01 dataset.

Reads the pages saved by `fetch_pages.py` and, for each one:

1. Finds the schema.org Recipe the page publishes about itself as JSON-LD.
2. Strips the schema.org markup (JSON-LD blocks, microdata and RDFa
   attributes) from the HTML, so the answer is no longer sitting in the page.
   Then cuts the page down to its visible text (see `html_to_text`): no head,
   scripts, styles, svg, nav, footer or attributes, just the text with list and
   heading structure kept. Raw pages run to ~100,000 tokens, past the context of
   small models; the cleaned text is a few thousand.
3. Keeps English recipes only, and only if every `recipeIngredient` line can
   be found both in the stripped HTML and in the cleaned text (see `Evidence`).
   Pages where cleaning loses an ingredient are logged as
   `ingredients_lost_in_cleaning`, apart from `ingredients_not_on_page`.
4. Parses each ingredient line with ingredient-parser-nlp into
   {name, amount, unit, original_text}. That flat list, in recipe order, is
   the page's answer. No ids, no unit vocabulary, no normalisation.
5. Writes the cleaned text and the answer to the output JSONL.

The labels are the parser's reading of the page's own ingredient lines, not
hand-checked ground truth. Models are scored on agreement with that reading.

No network access; rerun it freely after changing any rule here. Every page
looked at gets a line in the log CSV saying what happened to it.

    .venv/bin/python pipeline/build_dataset.py     (needs ingredient-parser-nlp, langdetect, lxml)

Output: data/dataset.jsonl, one object per line:
    {"id": <row_index>, "page_url": ..., "input": <cleaned page text>, "expected": [...]}
and data/build_log.csv, one line per page looked at.
"""

import argparse
import csv
import gzip
import html as htmllib
import json
import re
import sys
import unicodedata
from collections import Counter
from fractions import Fraction
from pathlib import Path

import lxml.html
from ingredient_parser import parse_ingredient
from langdetect import DetectorFactory, detect
from lxml import etree

DetectorFactory.seed = 0  # deterministic language detection

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG_FIELDS = ["row_index", "page_url", "status", "detail"]

JSON_LD_BLOCK = re.compile(
    r"<script\b[^>]*\btype\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
# The scraper returns the serialised DOM, where every attribute is written as
# name="value" with inner quotes escaped, so this cannot cut a value short.
MICRODATA_ATTR = re.compile(r'\s+item(?:scope|type|prop|id|ref)="[^"]*"', re.IGNORECASE)
# RDFa: `vocab` and `typeof` always go. `property` goes unless it carries a
# non-schema.org prefix such as og: or article:, which Open Graph tags use.
RDFA_ATTR = re.compile(
    r'\s+(?:vocab|typeof)="[^"]*"'
    r'|\s+property="(?:schema:|https?://schema\.org/)?[^":]*"',
    re.IGNORECASE,
)
INVISIBLE = re.compile(
    r"<(script|style|template)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL
)
TAG = re.compile(r"<[^>]+>")

# Elements that carry no recipe text, dropped with everything inside them.
DROP_TAGS = (
    "head script style noscript template svg canvas iframe object embed video audio "
    "picture img nav footer aside form button select dialog"
).split()
BLOCK_TAGS = set(
    "address article blockquote br dd details div dl dt fieldset figcaption figure "
    "header hr li main ol p pre section summary table tbody thead tr ul".split()
)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
INLINE_SPACE = re.compile(r"[ \t\r\f\v\u00a0\u200b]+")

# A value this short can turn up in a page by coincidence, so finding it
# proves nothing. See `Evidence.supports`.
MIN_TEXT_LENGTH = 4
MIN_DIGITS_LENGTH = 6
MIN_MARKUP_LENGTH = 8


# --- the page's own JSON-LD ---------------------------------------------------


def walk(node):
    """Yield every dict in a parsed JSON-LD document, at any depth."""
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def is_recipe(node):
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.rsplit("/", 1)[-1] == "Recipe" for t in types)


def page_recipes(page_html):
    """Every schema.org Recipe object in the page's JSON-LD blocks.

    Values that are only a reference to another object in the page's JSON-LD
    (`{"@id": ...}`, the way Yoast links a recipe to its author) are replaced
    by that object, one level deep.
    """
    nodes = []
    for block in JSON_LD_BLOCK.findall(page_html):
        try:
            # strict=False: real pages put raw newlines inside JSON strings.
            nodes.extend(walk(json.loads(block, strict=False)))
        except ValueError:
            continue
    by_id = {n["@id"]: n for n in nodes if isinstance(n.get("@id"), str) and len(n) > 1}

    def resolve(value):
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict) and set(value) == {"@id"}:
            return by_id.get(value["@id"], value)
        return value

    return [{key: resolve(value) for key, value in n.items()} for n in nodes if is_recipe(n)]


def strip_schema(page_html):
    """Remove JSON-LD blocks and microdata and RDFa attributes from the HTML."""
    stripped = JSON_LD_BLOCK.sub("", page_html)
    stripped = MICRODATA_ATTR.sub("", stripped)
    return RDFA_ATTR.sub("", stripped)


def html_to_text(page_html):
    """Visible text of a page: list items as "- ", headings as "# ", one block per line.

    Drops everything in `DROP_TAGS`, comments and all attributes. Walks the
    tree iteratively because real pages nest deeply enough to overflow
    recursion.
    """
    root = lxml.html.fromstring(page_html)
    doomed = root.xpath("//comment()|//processing-instruction()")
    doomed += root.xpath("|".join(f"//{tag}" for tag in DROP_TAGS))
    for element in doomed:
        if element.getparent() is not None:
            element.drop_tree()

    parts = []
    for event, element in etree.iterwalk(root, events=("start", "end")):
        tag = element.tag
        if not isinstance(tag, str):
            continue
        tag = tag.lower()
        if event == "start":
            if tag in BLOCK_TAGS or tag in HEADING_TAGS:
                parts.append("\n")
            if tag in HEADING_TAGS:
                parts.append("# ")
            elif tag == "li":
                parts.append("- ")
            elif tag in ("td", "th"):
                parts.append(" ")
            if element.text:
                parts.append(element.text)
        else:
            if tag in BLOCK_TAGS or tag in HEADING_TAGS:
                parts.append("\n")
            if element.tail:
                parts.append(element.tail)
    lines = (INLINE_SPACE.sub(" ", line).strip() for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line and line not in ("-", "#"))


# --- verifying values against the stripped page -------------------------------


def clean(value):
    """Tidy one text value: decode entities, drop inline tags, collapse spaces."""
    text = str(value)
    for _ in range(3):  # values are sometimes escaped more than once
        text = htmllib.unescape(text)
    text = TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def letters_and_digits(text):
    """Only the letters and digits of `text`, for layout-proof containment.

    Pages show "1/2 cup onion, diced" where the JSON says "½ cup onion
    (diced)", and split values across inline tags; neither should count as
    the value being absent from the page.
    """
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


class Evidence:
    """What a model can see in a page, indexed for lookups.

    Built twice per page: on the schema-stripped HTML, then on the cleaned
    text, to tell "never on the page" from "lost in cleaning". Script and
    style contents are left out on purpose: they are not page content.
    """

    def __init__(self, stripped_html):
        body = INVISIBLE.sub(" ", stripped_html)
        self.markup = htmllib.unescape(body)
        self.visible = letters_and_digits(htmllib.unescape(TAG.sub(" ", body)))

    def supports(self, value, in_list):
        """True if `value` is on the page: as visible text, or verbatim in markup.

        Visible text is compared on letters and digits only. A standalone
        value must be long enough that finding it is not a coincidence; an
        item of a list is exempt, because a list is only kept when every one
        of its items is found, and short items ("1 egg") are legitimate.
        Verbatim markup covers values held in attributes: image and link
        URLs, <time datetime>, meta description.
        """
        text = clean(value)
        key = letters_and_digits(text)
        if key and key in self.visible:
            long_enough = len(key) >= (MIN_DIGITS_LENGTH if key.isdigit() else MIN_TEXT_LENGTH)
            if in_list or long_enough:
                return True
        raw = str(value).strip()
        return len(raw) >= MIN_MARKUP_LENGTH and (raw in self.markup or text in self.markup)


def is_english(recipe, ingredient_lines):
    """`inLanguage` if the page states one, else a guess from the ingredient lines."""
    declared = recipe.get("inLanguage")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower().replace("_", "-").startswith("en")
    try:
        return detect(" ; ".join(ingredient_lines)) == "en"
    except Exception:  # langdetect raises on text with no letters
        return False


def number(value):
    """A parsed Fraction as a plain JSON number: 3/2 -> 1.5, 2 -> 2."""
    value = Fraction(value)
    return int(value) if value.denominator == 1 else round(float(value), 4)


def parse_line(text, stats):
    """One ingredient line -> {name, amount, unit, original_text}, or None."""
    parsed = parse_ingredient(text, string_units=True)
    name = " ".join(part.text for part in parsed.name).strip()
    if not name:
        return None
    amount = unit = None
    if parsed.amount:
        if len(parsed.amount) > 1:
            stats["lines_with_several_amounts"] += 1
        first = parsed.amount[0]  # first amount in text order
        if not hasattr(first, "RANGE"):
            # "1 pound 2 ounces": no single amount and unit; leave the page out.
            return None
        if first.RANGE:
            stats["ranges_lower_bound_used"] += 1
        if isinstance(first.quantity, str):
            # "a few", "several": no number to give; leave the page out.
            return None
        amount = number(first.quantity)
        unit = str(first.unit).strip() or None
    return {"name": name, "amount": amount, "unit": unit, "original_text": text}


def build_answer(page_html, stats):
    """Return (status, detail, cleaned_text, answer) for one fetched page."""
    recipes = page_recipes(page_html)
    if not recipes:
        return "no_recipe_jsonld", "", None, None

    stripped = strip_schema(page_html)
    evidence = Evidence(stripped)
    cleaned = None  # built once, and only if some recipe gets that far
    cleaned_evidence = None
    candidates = {}  # ingredient lines -> recipe, so a recipe published twice counts once
    reasons = Counter()
    for recipe in recipes:
        raw = recipe.get("recipeIngredient")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            reasons["no_ingredients"] += 1
            continue
        lines = [clean(item) for item in raw if isinstance(item, str) and clean(item)]
        if not lines:
            reasons["no_ingredients"] += 1
        elif not is_english(recipe, lines):
            reasons["not_english"] += 1
        elif not all(evidence.supports(line, in_list=True) for line in lines):
            reasons["ingredients_not_on_page"] += 1
        else:
            if cleaned is None:
                cleaned = html_to_text(stripped)
                cleaned_evidence = Evidence(cleaned)
            if all(cleaned_evidence.supports(line, in_list=True) for line in lines):
                candidates[tuple(lines)] = recipe
            else:
                reasons["ingredients_lost_in_cleaning"] += 1

    if not candidates:
        return "no_usable_recipe", " ".join(sorted(reasons)), None, None
    if len(candidates) > 1:
        # Several recipes on one page: no single right answer for it.
        return "multiple_recipes", str(len(candidates)), None, None

    (lines,) = candidates
    answer = []
    for line in lines:
        item = parse_line(line, stats)
        if item is None:
            return "unparseable_line", line[:100], None, None
        answer.append(item)
    return "ok", "", cleaned, answer


# --- main ---------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--pages-dir", type=Path, default=DATA / "raw" / "pages", help="output of fetch_pages.py"
    )
    parser.add_argument("--output", type=Path, default=DATA / "dataset.jsonl")
    parser.add_argument(
        "--log", type=Path, help="per-page outcome log (default: data/build_log.csv)"
    )
    parser.add_argument("--target", type=int, help="stop after writing this many rows")
    return parser.parse_args()


def main():
    args = parse_args()
    csv.field_size_limit(sys.maxsize)
    log_path = args.log or DATA / "build_log.csv"

    with open(args.pages_dir / "index.csv", newline="", encoding="utf-8") as f:
        fetched = [entry for entry in csv.DictReader(f) if entry["status"] == "ok"]
    fetched.sort(key=lambda entry: int(entry["row_index"]))

    statuses = Counter()
    stats = Counter()
    with (
        open(args.output, "w", encoding="utf-8") as out_file,
        open(log_path, "w", newline="", encoding="utf-8") as log_file,
    ):
        log = csv.DictWriter(log_file, fieldnames=LOG_FIELDS)
        log.writeheader()
        for done, entry in enumerate(fetched, start=1):
            if args.target is not None and statuses["ok"] >= args.target:
                break
            page_path = args.pages_dir / f"{entry['row_index']}.html.gz"
            with gzip.open(page_path, "rt", encoding="utf-8") as f:
                page_html = f.read()
            status, detail, cleaned, answer = build_answer(page_html, stats)
            if status == "ok":
                record = {
                    "id": int(entry["row_index"]),
                    "page_url": entry["page_url"],
                    "input": cleaned,
                    "expected": answer,
                }
                out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                stats["ingredient_lines"] += len(answer)
            log.writerow(
                {
                    "row_index": entry["row_index"],
                    "page_url": entry["page_url"],
                    "status": status,
                    "detail": detail,
                }
            )
            statuses[status] += 1
            if done % 500 == 0:
                print(f"{done}/{len(fetched)} pages read, {statuses['ok']} written")

    written = statuses["ok"]
    print(f"outcomes: {dict(statuses)}")
    print(f"{written} rows in {args.output}")
    print(f"stats: {dict(stats)}")
    if args.target is not None and written < args.target:
        print(f"stopped short of the target of {args.target}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
