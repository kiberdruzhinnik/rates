#!/usr/bin/env python3

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = "https://iss.moex.com/iss/apps/infogrid/equities/rates.json"

OUTPUT = Path("data/rates.json")
TEMP = Path("data/rates.json.tmp")

MAX_RETRIES = 5
RETRY_DELAY = 10
EXPECTED_COLUMN_COUNT = 50


def fetch_json(start: int) -> dict:
    url = BASE_URL + "?" + urllib.parse.urlencode({"start": start})

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "github-actions-moex-fetch/1.0",
            "Accept": "application/json",
        },
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Fetching start={start}: {url}")

            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")

                body = response.read()

            if not body:
                raise RuntimeError("Empty response")

            return json.loads(body)

        except Exception as exc:
            last_error = exc

            if attempt == MAX_RETRIES:
                break

            print(
                f"Attempt {attempt} failed: {exc}; "
                f"retrying in {RETRY_DELAY}s"
            )
            time.sleep(RETRY_DELAY)

    raise RuntimeError(
        f"Failed to fetch start={start}: {last_error}"
    )


def get_cursor(document: dict) -> dict:
    try:
        cursor_block = document["rates.cursor"]
        columns = cursor_block["columns"]
        data = cursor_block["data"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "Response has no valid rates.cursor"
        ) from exc

    if not isinstance(columns, list):
        raise RuntimeError("Invalid rates.cursor.columns")

    if not isinstance(data, list) or len(data) != 1:
        raise RuntimeError(
            f"Unexpected rates.cursor.data: {data!r}"
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


def validate_rows(rows: list, column_count: int, start: int) -> None:
    if not isinstance(rows, list):
        raise RuntimeError(
            f"Invalid rates.data at start={start}"
        )

    for row_number, row in enumerate(rows):
        if not isinstance(row, list):
            raise RuntimeError(
                f"Invalid row at start={start}, row={row_number}"
            )

        if len(row) != column_count:
            raise RuntimeError(
                "Column count mismatch at "
                f"start={start}, row={row_number}: "
                f"{len(row)} != {column_count}"
            )


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    first = fetch_json(0)

    if "rates" not in first:
        raise RuntimeError("Response does not contain 'rates'")

    rates = first["rates"]

    columns = rates.get("columns")
    first_rows = rates.get("data")

    if not isinstance(columns, list) or not columns:
        raise RuntimeError("Invalid rates.columns")

    column_count = len(columns)

    if column_count != EXPECTED_COLUMN_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_COLUMN_COUNT} rates columns, "
            f"received {column_count}"
        )

    validate_rows(first_rows, column_count, 0)

    first_cursor = get_cursor(first)

    if first_cursor["INDEX"] != 0:
        raise RuntimeError(
            f"First page INDEX is {first_cursor['INDEX']}, expected 0"
        )

    total = first_cursor["TOTAL"]
    page_size = first_cursor["PAGESIZE"]

    if not isinstance(total, int) or total < 0:
        raise RuntimeError(f"Invalid TOTAL: {total!r}")

    if not isinstance(page_size, int) or page_size <= 0:
        raise RuntimeError(
            f"Invalid PAGESIZE: {page_size!r}"
        )

    print(
        f"Cursor: TOTAL={total}, "
        f"PAGESIZE={page_size}, "
        f"COLUMNS={column_count}"
    )

    all_rows = []
    start = 0

    while start < total:
        page = first if start == 0 else fetch_json(start)

        cursor = get_cursor(page)

        if cursor["INDEX"] != start:
            raise RuntimeError(
                "Cursor INDEX mismatch: "
                f"requested {start}, received {cursor['INDEX']}"
            )

        if cursor["TOTAL"] != total:
            raise RuntimeError(
                "TOTAL changed during pagination: "
                f"{total} -> {cursor['TOTAL']}"
            )

        if cursor["PAGESIZE"] != page_size:
            raise RuntimeError(
                "PAGESIZE changed during pagination: "
                f"{page_size} -> {cursor['PAGESIZE']}"
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

        validate_rows(page_rows, column_count, start)

        print(
            f"Page start={start}: {len(page_rows)} rows"
        )

        all_rows.extend(page_rows)

        start += page_size

    if len(all_rows) != total:
        raise RuntimeError(
            "Incomplete pagination: "
            f"received {len(all_rows)} rows, "
            f"expected TOTAL={total}"
        )

    result = first
    result["rates"]["data"] = all_rows

    # The stored file is a fully assembled local snapshot.
    result["rates.cursor"]["data"] = [
        [0, total, total]
    ]

    with TEMP.open("w", encoding="utf-8") as f:
        json.dump(
            result,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        f.write("\n")

    # Validate the exact file we are about to publish.
    with TEMP.open("r", encoding="utf-8") as f:
        check = json.load(f)

    written_rows = check["rates"]["data"]

    if len(written_rows) != total:
        raise RuntimeError(
            "Post-write validation failed: "
            f"{len(written_rows)} != {total}"
        )

    if check["rates"]["columns"] != columns:
        raise RuntimeError(
            "Post-write columns validation failed"
        )

    TEMP.replace(OUTPUT)

    print()
    print("Complete MOEX rates snapshot written successfully")
    print(f"Rows:    {total}")
    print(f"Columns: {column_count}")
    print(f"Output:  {OUTPUT}")


if __name__ == "__main__":
    main()
