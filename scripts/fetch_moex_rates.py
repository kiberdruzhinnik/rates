#!/usr/bin/env python3

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = "https://iss.moex.com/iss/apps/infogrid/equities/rates.json"

OUTPUT = Path("data/rates.json")
TEMP = Path("data/rates.json.tmp")

# Retries for each individual HTTP request.
MAX_RETRIES = 5
RETRY_DELAY = 10

# Network timeout for each individual HTTP request.
REQUEST_TIMEOUT = 60

# Expected schema of the MOEX rates table.
EXPECTED_COLUMN_COUNT = 50


def fetch_json(start: int) -> dict:
    """
    Fetch one page of rates.json from MOEX.

    Individual HTTP requests are retried MAX_RETRIES times.
    If all attempts fail, the exception propagates to main(), causing
    the whole Python process to fail. GitHub Actions will then restart
    the complete fetch from start=0.
    """
    url = BASE_URL + "?" + urllib.parse.urlencode({"start": start})

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(
                f"Fetching start={start}, "
                f"HTTP attempt {attempt}/{MAX_RETRIES}: {url}",
                flush=True,
            )

            # Build a fresh Request object for every attempt.
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "github-actions-moex-fetch/1.0",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )

                body = response.read()

            if not body:
                raise RuntimeError("Empty response")

            document = json.loads(body)

            if not isinstance(document, dict):
                raise RuntimeError(
                    "Top-level JSON value is not an object"
                )

            return document

        except Exception as exc:
            last_error = exc

            print(
                f"HTTP attempt {attempt}/{MAX_RETRIES} "
                f"failed for start={start}: {exc}",
                flush=True,
            )

            if attempt == MAX_RETRIES:
                break

            print(
                f"Retrying HTTP request in {RETRY_DELAY}s...",
                flush=True,
            )
            time.sleep(RETRY_DELAY)

    raise RuntimeError(
        f"Failed to fetch start={start} after "
        f"{MAX_RETRIES} attempts: {last_error}"
    )


def get_cursor(document: dict) -> dict:
    """
    Extract and validate the rates.cursor block.
    """
    try:
        cursor_block = document["rates.cursor"]
        columns = cursor_block["columns"]
        data = cursor_block["data"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "Response has no valid rates.cursor"
        ) from exc

    if not isinstance(cursor_block, dict):
        raise RuntimeError(
            "rates.cursor is not an object"
        )

    if not isinstance(columns, list):
        raise RuntimeError(
            "Invalid rates.cursor.columns"
        )

    if not isinstance(data, list) or len(data) != 1:
        raise RuntimeError(
            f"Unexpected rates.cursor.data: {data!r}"
        )

    if not isinstance(data[0], list):
        raise RuntimeError(
            "rates.cursor.data[0] is not a list"
        )

    if len(columns) != len(data[0]):
        raise RuntimeError(
            "rates.cursor columns/data length mismatch"
        )

    cursor = dict(zip(columns, data[0]))

    required = {"INDEX", "TOTAL", "PAGESIZE"}
    missing = required - set(cursor)

    if missing:
        raise RuntimeError(
            f"Missing cursor fields: {sorted(missing)}"
        )

    return cursor


def validate_rows(
    rows: list,
    column_count: int,
    start: int,
) -> None:
    """
    Validate the structure of all rows in a single page.
    """
    if not isinstance(rows, list):
        raise RuntimeError(
            f"Invalid rates.data at start={start}"
        )

    for row_number, row in enumerate(rows):
        if not isinstance(row, list):
            raise RuntimeError(
                f"Invalid row at start={start}, "
                f"row={row_number}"
            )

        if len(row) != column_count:
            raise RuntimeError(
                "Column count mismatch at "
                f"start={start}, row={row_number}: "
                f"{len(row)} != {column_count}"
            )


def main() -> None:
    """
    Download every page of MOEX rates.json, validate the complete
    dataset, write it to a temporary file, validate the serialized
    file, and atomically replace data/rates.json.
    """
    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Do not allow a stale temporary file from an earlier execution
    # to be mistaken for output from this execution.
    TEMP.unlink(missing_ok=True)

    print(
        "Starting complete MOEX rates download",
        flush=True,
    )
    print(
        f"Source: {BASE_URL}",
        flush=True,
    )

    # ------------------------------------------------------------
    # First page
    # ------------------------------------------------------------

    first = fetch_json(0)

    if "rates" not in first:
        raise RuntimeError(
            "Response does not contain 'rates'"
        )

    rates = first["rates"]

    if not isinstance(rates, dict):
        raise RuntimeError(
            "'rates' is not an object"
        )

    columns = rates.get("columns")
    first_rows = rates.get("data")

    if not isinstance(columns, list) or not columns:
        raise RuntimeError(
            "Invalid rates.columns"
        )

    column_count = len(columns)

    if column_count != EXPECTED_COLUMN_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_COLUMN_COUNT} "
            f"rates columns, received {column_count}"
        )

    validate_rows(
        first_rows,
        column_count,
        0,
    )

    first_cursor = get_cursor(first)

    if first_cursor["INDEX"] != 0:
        raise RuntimeError(
            "First page INDEX is "
            f"{first_cursor['INDEX']}, expected 0"
        )

    total = first_cursor["TOTAL"]
    page_size = first_cursor["PAGESIZE"]

    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
    ):
        raise RuntimeError(
            f"Invalid TOTAL: {total!r}"
        )

    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size <= 0
    ):
        raise RuntimeError(
            f"Invalid PAGESIZE: {page_size!r}"
        )

    print(
        f"Cursor: TOTAL={total}, "
        f"PAGESIZE={page_size}, "
        f"COLUMNS={column_count}",
        flush=True,
    )

    # ------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------

    all_rows = []
    start = 0

    while start < total:
        if start == 0:
            page = first
        else:
            page = fetch_json(start)

        cursor = get_cursor(page)

        if cursor["INDEX"] != start:
            raise RuntimeError(
                "Cursor INDEX mismatch: "
                f"requested {start}, "
                f"received {cursor['INDEX']}"
            )

        if cursor["TOTAL"] != total:
            raise RuntimeError(
                "TOTAL changed during pagination: "
                f"{total} -> {cursor['TOTAL']}"
            )

        if cursor["PAGESIZE"] != page_size:
            raise RuntimeError(
                "PAGESIZE changed during pagination: "
                f"{page_size} -> "
                f"{cursor['PAGESIZE']}"
            )

        page_rates = page.get("rates")

        if not isinstance(page_rates, dict):
            raise RuntimeError(
                f"Missing rates block at start={start}"
            )

        page_columns = page_rates.get("columns")
        page_rows = page_rates.get("data")

        if page_columns != columns:
            raise RuntimeError(
                f"rates.columns changed at start={start}"
            )

        validate_rows(
            page_rows,
            column_count,
            start,
        )

        print(
            f"Page start={start}: "
            f"{len(page_rows)} rows",
            flush=True,
        )

        all_rows.extend(page_rows)

        start += page_size

    # ------------------------------------------------------------
    # Complete-dataset validation
    # ------------------------------------------------------------

    if len(all_rows) != total:
        raise RuntimeError(
            "Incomplete pagination: "
            f"received {len(all_rows)} rows, "
            f"expected TOTAL={total}"
        )

    # Ensure we have not accidentally accumulated the same list
    # object as returned in the first page.
    result = first

    result["rates"]["data"] = all_rows

    # This is now a locally assembled complete snapshot rather than
    # an individual MOEX page. Represent its cursor accordingly.
    #
    # The expected cursor columns from MOEX are:
    # INDEX, TOTAL, PAGESIZE
    cursor_columns = result["rates.cursor"]["columns"]

    cursor_values = {
        "INDEX": 0,
        "TOTAL": total,
        "PAGESIZE": total,
    }

    result["rates.cursor"]["data"] = [
        [
            cursor_values[column]
            for column in cursor_columns
        ]
    ]

    # ------------------------------------------------------------
    # Write temporary snapshot
    # ------------------------------------------------------------

    with TEMP.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            result,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        f.write("\n")

        # Ensure Python has handed all data to the operating system
        # before validating/publishing the file.
        f.flush()
        os.fsync(f.fileno())

    # ------------------------------------------------------------
    # Validate exact serialized file
    # ------------------------------------------------------------

    with TEMP.open(
        "r",
        encoding="utf-8",
    ) as f:
        check = json.load(f)

    if not isinstance(check, dict):
        raise RuntimeError(
            "Post-write JSON root is invalid"
        )

    check_rates = check.get("rates")

    if not isinstance(check_rates, dict):
        raise RuntimeError(
            "Post-write rates block is invalid"
        )

    written_columns = check_rates.get("columns")
    written_rows = check_rates.get("data")

    if written_columns != columns:
        raise RuntimeError(
            "Post-write columns validation failed"
        )

    if not isinstance(written_rows, list):
        raise RuntimeError(
            "Post-write rates.data is invalid"
        )

    if len(written_rows) != total:
        raise RuntimeError(
            "Post-write validation failed: "
            f"{len(written_rows)} != {total}"
        )

    validate_rows(
        written_rows,
        column_count,
        0,
    )

    # ------------------------------------------------------------
    # Atomic publication
    # ------------------------------------------------------------

    #
    # rates.json remains the previous known-good version until every
    # download and validation step above has completed successfully.
    #
    os.replace(TEMP, OUTPUT)

    print()
    print(
        "Complete MOEX rates snapshot written successfully",
        flush=True,
    )
    print(
        f"Rows:     {total}",
        flush=True,
    )
    print(
        f"Columns:  {column_count}",
        flush=True,
    )
    print(
        f"Output:   {OUTPUT}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Make the failure particularly visible in GitHub Actions.
        print()
        print(
            f"FATAL: {type(exc).__name__}: {exc}",
            flush=True,
        )

        # Never leave an incomplete temporary snapshot around.
        TEMP.unlink(missing_ok=True)

        raise
