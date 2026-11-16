#!/usr/bin/env python3
"""Stage 1: fetch every page listed in the WDC Recipe CSV and keep it untouched.

Each `page_url` is scraped once with the Playwright scraper in --scraper-dir.
The rendered HTML is saved exactly as returned, one gzip file per page:

    data/raw/pages/<row_index>.html.gz
    data/raw/pages/index.csv          one line per input row: what happened to it

Nothing is filtered or altered here; `build_dataset.py` (stage 2) reads this
directory and can be rerun as often as needed without fetching again.

A URL that appears on several rows is fetched for its first row only; the
other rows are indexed as `duplicate_url` with the first row's index.

Rerunning resumes: rows already in the index are skipped. Run it with a
Python that has Playwright installed:

    python fetch_pages.py --scraper-dir <directory containing scrape.py>
"""

import argparse
import csv
import gzip
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from multiprocessing.util import Finalize
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
INDEX_FIELDS = ["row_index", "page_url", "status", "detail"]

# --- worker process ---------------------------------------------------------

_scraper = None
_ScrapeError = None


def init_worker(scraper_dir):
    """Open one headless browser per worker; the Scraper is not thread-safe."""
    global _scraper, _ScrapeError
    sys.path.insert(0, scraper_dir)
    from scrape import ScrapeError, Scraper

    _scraper = Scraper()
    _ScrapeError = ScrapeError
    Finalize(_scraper, _scraper.close, exitpriority=10)


def fetch_row(row_index, url, pages_dir, timeout):
    """Scrape one page and save it. Returns the row's index entry."""
    entry = {"row_index": row_index, "page_url": url, "detail": ""}
    try:
        try:
            page_html = _scraper.fetch(url, timeout=timeout)
        except _ScrapeError as exc:
            first_line = (str(exc).splitlines() or [""])[0]
            return entry | {"status": "fetch_error", "detail": first_line[:200]}

        # Write to a temporary name first so an interrupted run never leaves
        # a truncated page behind under its final name.
        final = Path(pages_dir) / f"{row_index}.html.gz"
        partial = final.with_suffix(".part")
        with gzip.open(partial, "wt", encoding="utf-8") as f:
            f.write(page_html)
        partial.replace(final)
        return entry | {"status": "ok"}
    except Exception as exc:  # one bad page must not take the run down
        return entry | {"status": "worker_error", "detail": f"{type(exc).__name__}: {exc}"[:200]}


# --- main process -----------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--input", type=Path, default=DATA / "raw" / "wdc_2023-12_Recipe_part_0.csv")
    parser.add_argument("--pages-dir", type=Path, default=DATA / "raw" / "pages")
    parser.add_argument("--limit", type=int, help="only look at the first N input rows")
    parser.add_argument("--workers", type=int, default=8, help="parallel browsers (default: 8)")
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="seconds per scraper wait stage (default: 20)"
    )
    parser.add_argument(
        "--scraper-dir",
        required=True,
        help="directory holding scrape.py",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    csv.field_size_limit(sys.maxsize)
    args.pages_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.pages_dir / "index.csv"

    done = set()
    first_row_for_url = {}
    if index_path.exists():
        with open(index_path, newline="", encoding="utf-8") as f:
            for entry in csv.DictReader(f):
                done.add(int(entry["row_index"]))
                if entry["status"] != "duplicate_url":
                    first_row_for_url[entry["page_url"]] = int(entry["row_index"])
        print(f"resuming: {len(done)} rows already indexed")

    is_new = not index_path.exists() or index_path.stat().st_size == 0
    index_file = open(index_path, "a", newline="", encoding="utf-8")
    index = csv.DictWriter(index_file, fieldnames=INDEX_FIELDS)
    if is_new:
        index.writeheader()

    statuses = Counter()
    started = time.monotonic()

    def record(entry):
        index.writerow(entry)
        index_file.flush()
        statuses[entry["status"]] += 1
        handled = sum(statuses.values())
        if handled % 250 == 0:
            minutes = (time.monotonic() - started) / 60
            print(f"{handled} rows handled, {statuses['ok']} pages saved, {minutes:.1f} min")

    def jobs():
        """Rows to fetch; repeats of a URL are indexed here instead."""
        with open(args.input, newline="", encoding="utf-8") as f:
            for row_index, row in enumerate(csv.DictReader(f)):
                if args.limit is not None and row_index >= args.limit:
                    return
                if row_index in done:
                    continue
                url = row["page_url"]
                if url in first_row_for_url:
                    record(
                        {
                            "row_index": row_index,
                            "page_url": url,
                            "status": "duplicate_url",
                            "detail": first_row_for_url[url],
                        }
                    )
                    continue
                first_row_for_url[url] = row_index
                yield row_index, url, str(args.pages_dir), args.timeout

    pool = ProcessPoolExecutor(args.workers, initializer=init_worker, initargs=(args.scraper_dir,))
    job_iter = jobs()
    pending = set()
    interrupted = False
    try:
        while True:
            while len(pending) < args.workers * 2:
                job = next(job_iter, None)
                if job is None:
                    break
                pending.add(pool.submit(fetch_row, *job))
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                record(future.result())
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted; rerun the same command to resume")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        index_file.close()

    print(f"this run: {dict(statuses)}")
    print(f"pages and index are in {args.pages_dir}")
    return 1 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
