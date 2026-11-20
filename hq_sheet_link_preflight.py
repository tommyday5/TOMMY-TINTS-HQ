"""Read-only, fail-closed Jobs-sheet linkage preflight; no provider writes.

Accepts a complete, caller-fetched Sheets GridData snapshot containing
userEnteredValue for A:AJ from row 4 to the end of the Jobs grid. It never
calls Google, changes an HQ record, stores personal data or creates links.
"""
import re
import unicodedata

from hq_job_sync import INPUT_COLUMNS, WORKBOOK

JOBS_SHEET_ID = 330634230
FIRST_HEADER_ROW = 4
FIRST_DATA_ROW = 5
MAX_ROWS = 2000
MIN_COLUMNS = 36  # AJ, the highest mapped input column.

# Already documented by the existing read-only Jobs analytics adapter.
# Other headings, especially B:E, need separate owner verification.
VERIFIED_HEADERS = {
    0: "Job ID",
    **{index: f"Service {index - 4}" for index in range(5, 10)},
    16: "Quoted Price",
    17: "Status",
    32: "Actual Hours",
    33: "Actual Materials £",
    34: "Actual Consumables £",
    35: "Actual Extra Costs £",
}


def _index(column):
    number = 0
    for char in column:
        number = number * 26 + ord(char) - ord("A") + 1
    return number - 1


def _input_indexes():
    columns = []
    for value in INPUT_COLUMNS.values():
        columns.extend(value if isinstance(value, list) else [value])
    return frozenset(_index(column) for column in columns)


_INPUT_INDEXES = _input_indexes()


def _entered(row, index):
    if not isinstance(row, dict) or not isinstance(row.get("values", []), list):
        raise ValueError("Malformed Sheets row evidence")
    cells = row.get("values", [])
    if len(cells) > 94:
        raise ValueError("Sheets row exceeds documented worksheet width")
    cell = cells[index] if index < len(cells) else {}
    if not isinstance(cell, dict):
        raise ValueError("Malformed Sheets cell evidence")
    if ("userEnteredValue" not in cell and
            (cell.get("formattedValue") or cell.get("effectiveValue"))):
        raise ValueError("Missing raw Sheets entered-value evidence")
    value = cell.get("userEnteredValue")
    if value is not None and (not isinstance(value, dict) or len(value) != 1):
        raise ValueError("Ambiguous Sheets entered value")
    if value and not set(value).issubset(
            {"stringValue", "numberValue", "boolValue", "formulaValue"}):
        raise ValueError("Unsupported Sheets entered value")
    return value


def _text(value, label):
    if value is None:
        return ""
    if set(value) != {"stringValue"} or not isinstance(value["stringValue"], str):
        raise ValueError(f"{label} must be a literal text cell")
    return value["stringValue"]


def preflight_job_link(snapshot, *, job_ref, target_row, mode):
    """Check an owner-selected J reference and row, not an HQ-to-Sheets link.

    For new links the reference must not exist and mapped input cells must be
    empty. For historical links the requested reference must match the chosen
    row exactly and uniquely. Neither result proves HQ identity, baseline,
    permission, consent, payment, or reconciliation.
    """
    if type(job_ref) is not str or not re.fullmatch(r"J[0-9]{4,8}", job_ref):
        raise ValueError("A manually reviewed J reference is required")
    if type(target_row) is not int or mode not in ("new", "existing"):
        raise ValueError("Explicit candidate row and link mode required")
    if not isinstance(snapshot, dict) or snapshot.get("spreadsheetId") != WORKBOOK:
        raise ValueError("Workbook identity is not verified")
    sheets = snapshot.get("sheets")
    if not isinstance(sheets, list):
        raise ValueError("Missing Sheets metadata")
    matches = [sheet for sheet in sheets if isinstance(sheet, dict) and
               isinstance(sheet.get("properties"), dict) and
               sheet["properties"].get("sheetId") == JOBS_SHEET_ID]
    if len(matches) != 1 or matches[0]["properties"].get("title") != "Jobs":
        raise ValueError("Expected Jobs tab identity not confirmed")
    sheet = matches[0]
    grid = sheet["properties"].get("gridProperties")
    if not isinstance(grid, dict):
        raise ValueError("Missing worksheet dimensions")
    count, width = grid.get("rowCount"), grid.get("columnCount")
    if (type(count) is not int or not FIRST_DATA_ROW <= count <= MAX_ROWS or
            type(width) is not int or not MIN_COLUMNS <= width <= 94):
        raise ValueError("Unsupported worksheet dimensions")
    if not FIRST_DATA_ROW <= target_row <= count:
        raise ValueError("Candidate row is outside Jobs data")
    blocks = sheet.get("data")
    if not isinstance(blocks, list) or len(blocks) != 1:
        raise ValueError("A single complete GridData snapshot is required")
    block = blocks[0]
    if (not isinstance(block, dict) or block.get("startRow") != FIRST_HEADER_ROW - 1
            or block.get("startColumn", 0) != 0):
        raise ValueError("GridData must start at Jobs row 4, column A")
    rows = block.get("rowData")
    if not isinstance(rows, list) or len(rows) != count - FIRST_HEADER_ROW + 1:
        raise ValueError("Truncated Jobs reference scan; no uniqueness proof")

    seen_headers = set()
    for index in range(MIN_COLUMNS):
        name = _text(_entered(rows[0], index), "Column heading")
        if name:
            folded = unicodedata.normalize("NFKC", name).strip().casefold()
            if folded in seen_headers:
                raise ValueError("Ambiguous duplicate Jobs headings")
            seen_headers.add(folded)
        if index in VERIFIED_HEADERS and name != VERIFIED_HEADERS[index]:
            raise ValueError("Jobs writable column schema changed")

    seen_refs, found_rows = set(), []
    candidate_ref = job_ref.casefold()
    for number, row in enumerate(rows[1:], start=FIRST_DATA_ROW):
        ref = _text(_entered(row, 0), "Job ID")
        if ref:
            if (unicodedata.normalize("NFKC", ref) != ref or
                    ref != ref.strip() or not ref.isascii() or not ref.isprintable()):
                raise ValueError("Ambiguous existing Job ID")
            canonical = ref.casefold()
            if canonical in seen_refs:
                raise ValueError("Duplicate Job IDs prevent safe linking")
            seen_refs.add(canonical)
            # Allocation must use the same comparison rule as the global
            # uniqueness scan.  Otherwise a historical "j0083" can be
            # silently reused as a new "J0083" on a different row.
            if canonical == candidate_ref:
                if ref != job_ref and mode == "existing":
                    raise ValueError("Historical Job ID casing does not match")
                found_rows.append(number)

    target = rows[target_row - FIRST_HEADER_ROW]
    for index in _INPUT_INDEXES:
        value = _entered(target, index)
        if value is not None and "formulaValue" in value:
            raise ValueError("Mapped input contains a formula; do not overwrite")
    if mode == "new":
        if found_rows:
            raise ValueError("Proposed new Job ID already exists in the workbook")
        if any(_entered(target, index) is not None for index in _INPUT_INDEXES):
            raise ValueError("Candidate row already contains entered input data")
    elif found_rows != [target_row]:
        raise ValueError("Historical Job ID must uniquely match the chosen row")

    return {
        "preflight": "sheet_snapshot_only",
        "workbook_identity_checked": True,
        "complete_reference_scan": True,
        "writable_header_subset_checked": True,
        "selected_row_formula_safe": True,
        "candidate_kind": mode,
        "historical_mapping_verified": False,
        "baseline_verified": False,
        "owner_write_approval": False,
        "write_authorised": False,
        "live_sync": False,
        "reconciled": False,
        "next_safe_action": (
            "Review and approve the selected row/reference, verify HQ mapping "
            "and baseline, then obtain separate write consent."
        ),
    }
