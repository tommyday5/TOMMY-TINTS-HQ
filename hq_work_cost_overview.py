"""Single-transaction owner-only costing overview of canonical HQ workshop ledgers.

This is a *recorded labour + consumed materials* subtotal, not final job cost,
profit, invoice, bank settlement, inventory movement or a replacement for the
legacy job-cost log. Both ledgers and the job revision are read in one SQLite
snapshot to avoid displaying mismatched revisions. Never assume missing = 0.
"""
import re
import sqlite3

from customer_request_store import connect
from hq_core_records import _schema
from hq_work_time import _tables as _time_tables
from hq_work_materials import _tables as _material_tables
from hq_work_expenses import _tables as _expense_tables

_ID = re.compile(r"[0-9a-f]{32}\Z")
_APPROVED = frozenset(("approved", "in_progress", "completed"))


def cost_overview_snapshot(work_id):
    """Read a point-in-time, non-mutating cost summary from canonical HQ storage."""
    if not isinstance(work_id, str) or _ID.fullmatch(work_id) is None:
        raise ValueError("Invalid HQ work reference")
    with connect() as db:
        _schema(db)
        _time_tables(db)
        _material_tables(db)
        _expense_tables(db)
        db.execute("BEGIN")
        work = db.execute("""SELECT w.id,w.kind,w.status,w.revision,w.service,
            w.price_pence,w.deposit_pence,w.source_request_id,
            c.name AS customer_name,v.make,v.model,v.registration
            FROM hq_work_records w
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            WHERE w.id=?""", (work_id,)).fetchone()
        if work is None:
            raise LookupError("HQ work not found")
        time = db.execute("""SELECT COUNT(*) AS entries,
            COALESCE(SUM(minutes),0) AS minutes,
            COALESCE(SUM(CASE WHEN hourly_cost_pence IS NULL THEN 1 ELSE 0 END),0) AS unknown,
            SUM(CASE WHEN hourly_cost_pence IS NOT NULL
                THEN (minutes * hourly_cost_pence + 30)/60 END) AS known_pence
            FROM hq_work_time_entries WHERE work_id=? AND voided_at IS NULL""",
            (work_id,)).fetchone()
        materials = db.execute("""SELECT COUNT(*) AS entries,
            COALESCE(SUM(CASE WHEN actual_cost_pence IS NULL THEN 1 ELSE 0 END),0) AS unknown,
            SUM(actual_cost_pence) AS known_pence
            FROM hq_work_material_entries WHERE work_id=? AND voided_at IS NULL""",
            (work_id,)).fetchone()
        expenses = db.execute("""SELECT COUNT(*) AS entries,
            COALESCE(SUM(CASE WHEN allocated_cost_pence IS NULL THEN 1 ELSE 0 END),0) AS unknown,
            SUM(allocated_cost_pence) AS known_pence
            FROM hq_work_expense_entries WHERE work_id=? AND voided_at IS NULL""",
            (work_id,)).fetchone()

    labour_count, material_count = time["entries"], materials["entries"]
    expense_count = expenses["entries"]
    unknown_labour, unknown_materials = time["unknown"], materials["unknown"]
    unknown_expenses = expenses["unknown"]
    labour_known = time["known_pence"]
    materials_known = materials["known_pence"]
    expenses_known = expenses["known_pence"]
    # Display independently verified *entered* amounts, but never a fictitious
    # combined complete ledger when any active cost is unknown or absent.
    known_subtotal = (
        (labour_known or 0) + (materials_known or 0)
        if labour_known is not None or materials_known is not None else None
    )
    captured_complete = (labour_count > 0 and material_count > 0
                         and unknown_labour == 0 and unknown_materials == 0)
    # This is still only the complete *recorded* triple-ledger allocation,
    # never proof that every real cost or historic log was entered.
    all_recorded = (captured_complete and expense_count > 0 and unknown_expenses == 0)
    known_three = (
        (known_subtotal or 0) + (expenses_known or 0)
        if known_subtotal is not None or expenses_known is not None else None
    )
    work_data = dict(work)
    work_data.update({
        "labour_entry_count": labour_count,
        "labour_minutes": time["minutes"],
        "labour_unknown_count": unknown_labour,
        "labour_known_pence": labour_known,
        "labour_recorded_total_pence": (labour_known or 0)
            if labour_count and unknown_labour == 0 else None,
        "material_entry_count": material_count,
        "material_unknown_count": unknown_materials,
        "material_known_pence": materials_known,
        "material_recorded_total_pence": (materials_known or 0)
            if material_count and unknown_materials == 0 else None,
        "partial_known_subtotal_pence": known_subtotal,
        "recorded_labour_material_complete": captured_complete,
        "recorded_labour_material_total_pence": known_subtotal if captured_complete else None,
        "expense_entry_count": expense_count,
        "expense_unknown_count": unknown_expenses,
        "expense_known_pence": expenses_known,
        "expense_recorded_total_pence": (expenses_known or 0)
            if expense_count and unknown_expenses == 0 else None,
        "partial_known_three_ledger_pence": known_three,
        "recorded_three_ledger_complete": all_recorded,
        "recorded_three_ledger_total_pence": known_three if all_recorded else None,
        "approved_sale_price": (work["price_pence"] if work["status"] in _APPROVED
                                and work["price_pence"] is not None else None),
        # Even a complete log is only a subset of actual total job expenses.
        "total_job_cost_pence": None,
        "final_profit_pence": None,
        "margin_percent": None,
        "payment_verified": False,
        "historical_costs_reconciled": False,
        "missing_sources": [
            "Expenses and overhead not yet captured or verified against supplier evidence",
            "Supplier invoices, stock movements and subcontractor matching",
            "Historical Google Sheets and existing manual profitability logs",
        ],
    })
    return work_data
