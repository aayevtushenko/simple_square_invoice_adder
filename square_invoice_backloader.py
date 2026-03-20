#!/usr/bin/env python3
"""Backload legacy invoices into Square with batch-scoped human review."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests import RequestException

# Toggle this constant when switching between the live Square API and sandbox.
# SQUARE_API_BASE = "https://connect.squareup.com"
SQUARE_API_BASE = "https://connect.squareupsandbox.com"

DEFAULT_CURRENCY = "USD"
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
BATCH_REGISTRY_PATH = os.path.join(APP_ROOT, "batch_registry.json")
REVIEW_CSV_NAME = "review.csv"
FAILURES_CSV_NAME = "failures.csv"


class SquareAPIError(RuntimeError):
    """Raised when a Square API request fails or returns an error response."""

    pass


@dataclass
class NormalizedLineItem:
    """Order-ready line item after validation and any human review resolution."""

    name: str
    quantity: Decimal
    unit_price: Decimal
    note: Optional[str] = None


@dataclass
class LegacyInvoice:
    """Normalized invoice data ready to upload to Square."""

    invoice_id: str
    invoice_number: str
    invoice_date: str
    due_date: str
    customer_name: str
    customer_email: Optional[str]
    customer_phone: Optional[str]
    customer_address: Optional[str]
    service_notes: Optional[str]
    line_items: List[NormalizedLineItem]
    subtotal: Optional[Decimal]
    sales_tax: Decimal


@dataclass
class ReviewRow:
    """One human-review action item written to `review.csv`."""

    invoice_id: str
    field_name: str
    input_value: str
    fallback: str
    use_fallback: str = ""
    manual_adjustment: str = ""

    def to_csv_row(self) -> Dict[str, str]:
        """Convert the row to the CSV column layout used on disk."""

        return {
            "invoice_id": self.invoice_id,
            "field_name": self.field_name,
            "input": self.input_value,
            "fallback": self.fallback,
            "use_fallback": self.use_fallback,
            "manual_adjustment": self.manual_adjustment,
        }


@dataclass
class FailureRow:
    """One permanent-failure record written to `failures.csv`."""

    invoice_id: str
    file_label: str
    invoice_number: str
    failure_reason: str
    customer_name: str
    line_items_input: str

    def to_csv_row(self) -> Dict[str, str]:
        """Convert the row to the CSV column layout used on disk."""

        return {
            "invoice_id": self.invoice_id,
            "file_label": self.file_label,
            "invoice_number": self.invoice_number,
            "failure_reason": self.failure_reason,
            "customer_name": self.customer_name,
            "line_items_input": self.line_items_input,
        }


@dataclass
class LineItemAnalysis:
    """Parsed state for one raw legacy line item."""

    index: int
    name: str
    raw_quantity: Any
    raw_unit_price: Any
    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    quantity_problem: bool
    unit_price_problem: bool
    quantity_label: str


@dataclass
class InspectionResult:
    """Classification result for one invoice during batch inspection."""

    invoice_id: str
    file_label: str
    invoice_number: str
    review_rows: List[ReviewRow] = field(default_factory=list)
    failure_row: Optional[FailureRow] = None
    clean_invoice: Optional[LegacyInvoice] = None

    @property
    def is_clean(self) -> bool:
        """Return `True` when the invoice can be processed without review."""

        return self.failure_row is None and not self.review_rows and self.clean_invoice is not None

    @property
    def is_reviewable(self) -> bool:
        """Return `True` when the invoice requires human review before upload."""

        return self.failure_row is None and bool(self.review_rows)

    @property
    def is_failure(self) -> bool:
        """Return `True` when the invoice is permanently unprocessable."""

        return self.failure_row is not None


@dataclass
class BatchPaths:
    """Filesystem paths for one batch's persisted review artifacts."""

    batch_id: str
    batch_dir: str
    review_csv_path: str
    failures_csv_path: str


def money_to_cents(value: Decimal) -> int:
    """Convert a decimal dollar amount into the integer cents Square expects.

    Args:
        value: Currency amount in dollars represented as a `Decimal`.
    """

    return int((value * 100).quantize(Decimal("1")))


def parse_decimal(value: str, *, default: str = "0") -> Decimal:
    """Safely parse a decimal string, falling back when input is blank or invalid.

    Args:
        value: Raw numeric string read from the legacy data source.
        default: Decimal string to use when `value` cannot be parsed.
    """

    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return Decimal(default)


def parse_iso_date(value: Optional[str], *, fallback: dt.date) -> dt.date:
    """Parse an ISO date string and return a fallback date when parsing fails.

    Args:
        value: Optional date string in ISO format such as `YYYY-MM-DD`.
        fallback: Date to return when the input is blank or invalid.
    """

    if not value:
        return fallback
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return fallback


def normalize_due_date(due_date: dt.date, *, today: Optional[dt.date] = None) -> dt.date:
    """Return an API-safe due date for manual-share invoices.

    When `delivery_method=SHARE_MANUALLY`, Square does not use a send date. Setting a
    future `scheduled_at` leaves the invoice in a "scheduled" state after publish.
    To mimic Dashboard behavior ("Due today"), clamp due date to today-or-later and
    omit `scheduled_at` completely.

    Args:
        due_date: Due date parsed from the legacy invoice.
        today: Optional override used to compare against the current date.
    """

    if today is None:
        today = dt.date.today()
    return max(due_date, today)


def flatten_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the legacy `results` key/value pairs into a simple dictionary.

    Args:
        record: One raw record object from the legacy export JSON.
    """

    return {item["key"]: item.get("value") for item in record.get("results", [])}


def is_missing_value(value: Any) -> bool:
    """Return `True` when a raw field value should be treated as missing.

    Args:
        value: Raw field value from the legacy export.
    """

    return value is None or (isinstance(value, str) and not value.strip())


def try_parse_decimal(value: Any) -> Optional[Decimal]:
    """Attempt to parse a decimal value without applying fallback behavior.

    Args:
        value: Raw numeric value from the legacy export.
    """

    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def try_parse_iso_date(value: Any) -> Optional[dt.date]:
    """Attempt to parse an ISO date string without applying fallback behavior.

    Args:
        value: Raw date value from the legacy export.
    """

    if is_missing_value(value):
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def try_parse_line_items(value: Any) -> tuple[Optional[List[Any]], bool]:
    """Parse the raw line-item JSON string into a list when possible.

    Args:
        value: Raw `Line_Items` value from the legacy export.
    """

    if is_missing_value(value):
        return None, False
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return None, True
    if not isinstance(parsed, list):
        return None, True
    return parsed, False


def normalize_optional_string(value: Any) -> Optional[str]:
    """Trim a raw string value and return `None` when it is blank.

    Args:
        value: Raw field value from the legacy export.
    """

    if is_missing_value(value):
        return None
    return str(value).strip()


def stringify_value(value: Any) -> str:
    """Render a raw value into a stable CSV-friendly string.

    Args:
        value: Raw or derived value that should be written to CSV.
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def decimal_to_text(value: Decimal) -> str:
    """Format a decimal for CSV and human-readable summaries.

    Args:
        value: Decimal value to render.
    """

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def quantity_to_text(value: Decimal) -> str:
    """Format a quantity decimal the way Square expects it.

    Args:
        value: Decimal quantity to render.
    """

    return decimal_to_text(value)


def get_record_file_label(record: Dict[str, Any], index: int) -> str:
    """Build a human-readable label for one source file/record.

    Args:
        record: One raw record object from the legacy export JSON.
        index: Zero-based position of the record in the source file.
    """

    document_path = record.get("document_path")
    if isinstance(document_path, str) and document_path.strip():
        return os.path.basename(document_path)
    file_name = record.get("file_name")
    if isinstance(file_name, str) and file_name.strip():
        return file_name
    return f"record-{index + 1}"


def build_review_row(invoice_id: str, field_name: str, input_value: Any, fallback: Any = "") -> ReviewRow:
    """Create one review row with consistent string normalization.

    Args:
        invoice_id: Batch-local invoice ID.
        field_name: Human-readable field name used in `review.csv`.
        input_value: Raw or derived value shown to the reviewer.
        fallback: Proposed fallback value that can be approved with `x`.
    """

    if isinstance(fallback, Decimal):
        fallback_text = decimal_to_text(fallback)
    else:
        fallback_text = stringify_value(fallback)
    return ReviewRow(
        invoice_id=invoice_id,
        field_name=field_name,
        input_value=stringify_value(input_value),
        fallback=fallback_text,
    )


def build_failure_row(
    invoice_id: str,
    file_label: str,
    invoice_number: str,
    failure_reason: str,
    customer_name: Optional[str],
    line_items_input: Any,
) -> FailureRow:
    """Create one permanent-failure row for `failures.csv`.

    Args:
        invoice_id: Batch-local invoice ID.
        file_label: Human-readable source label for the record.
        invoice_number: Legacy invoice number when available.
        failure_reason: Explanation of why the invoice cannot be processed.
        customer_name: Raw customer name value.
        line_items_input: Raw `Line_Items` source value.
    """

    return FailureRow(
        invoice_id=invoice_id,
        file_label=file_label,
        invoice_number=invoice_number,
        failure_reason=failure_reason,
        customer_name=customer_name or "",
        line_items_input=stringify_value(line_items_input),
    )


def analyze_line_item(raw_item: Any, index: int) -> LineItemAnalysis:
    """Parse one raw legacy line item into validation metadata.

    Args:
        raw_item: One element from the legacy `Line_Items` JSON array.
        index: One-based line-item index used in field names.
    """

    if not isinstance(raw_item, dict):
        return LineItemAnalysis(
            index=index,
            name=f"Line {index}",
            raw_quantity=None,
            raw_unit_price=None,
            quantity=None,
            unit_price=None,
            quantity_problem=True,
            unit_price_problem=True,
            quantity_label="quantity unknown",
        )

    name = str(raw_item.get("Description") or f"Line {index}")
    raw_quantity = raw_item.get("Quantity")
    raw_unit_price = raw_item.get("Unit Price")
    quantity = try_parse_decimal(raw_quantity)
    unit_price = try_parse_decimal(raw_unit_price)

    quantity_problem = is_missing_value(raw_quantity) or quantity is None or quantity <= 0
    unit_price_problem = is_missing_value(raw_unit_price) or unit_price is None or unit_price < 0

    if quantity_problem:
        quantity_label = "quantity unknown"
    else:
        quantity_label = f"qty {quantity_to_text(quantity)}"

    return LineItemAnalysis(
        index=index,
        name=name,
        raw_quantity=raw_quantity,
        raw_unit_price=raw_unit_price,
        quantity=quantity if not quantity_problem else None,
        unit_price=unit_price if not unit_price_problem else None,
        quantity_problem=quantity_problem,
        unit_price_problem=unit_price_problem,
        quantity_label=quantity_label,
    )


def build_combined_line_description(items: List[LineItemAnalysis]) -> str:
    """Summarize broken line items for the synthetic combined-line review row.

    Args:
        items: Broken line items being merged into one fallback line.
    """

    parts = [f"{item.name} ({item.quantity_label})" for item in items]
    return "; ".join(parts) if parts else "missing line items"


def build_clean_invoice_from_flattened(
    invoice_id: str,
    flattened: Dict[str, Any],
    line_items: List[NormalizedLineItem],
) -> LegacyInvoice:
    """Build a `LegacyInvoice` when no human review is required.

    Args:
        invoice_id: Batch-local invoice ID.
        flattened: Flattened key/value map for the record.
        line_items: Already validated line items ready for upload.
    """

    today = dt.date.today()
    invoice_date_raw = normalize_optional_string(flattened.get("Invoice_Date"))
    due_date_raw = normalize_optional_string(flattened.get("Due_Date"))
    invoice_date = parse_iso_date(invoice_date_raw, fallback=today).isoformat()
    due_date = parse_iso_date(due_date_raw, fallback=today).isoformat()

    subtotal_raw = flattened.get("Subtotal")
    subtotal = try_parse_decimal(subtotal_raw)
    if subtotal is not None and subtotal < 0:
        subtotal = None

    return LegacyInvoice(
        invoice_id=invoice_id,
        invoice_number=str(flattened.get("Invoice_Number") or "UNKNOWN"),
        invoice_date=invoice_date,
        due_date=due_date,
        customer_name=str(flattened.get("Customer_Name") or "Unknown Customer"),
        customer_email=normalize_optional_string(flattened.get("Customer_Email")),
        customer_phone=normalize_optional_string(flattened.get("Customer_Phone_Number")),
        customer_address=normalize_optional_string(flattened.get("Customer_Address")),
        service_notes=normalize_optional_string(flattened.get("Service_Notes")),
        line_items=line_items,
        subtotal=subtotal,
        sales_tax=parse_decimal(str(flattened.get("Sales_Tax") or "0")),
    )


def inspect_record(record: Dict[str, Any], index: int) -> InspectionResult:
    """Classify one record as clean, reviewable, or permanently failed.

    Args:
        record: One raw record object from the legacy export JSON.
        index: Zero-based position of the record in the source file.
    """

    invoice_id = str(index + 1)
    flattened = flatten_record(record)
    file_label = get_record_file_label(record, index)
    invoice_number = str(flattened.get("Invoice_Number") or "UNKNOWN")
    customer_name = normalize_optional_string(flattened.get("Customer_Name"))
    review_rows: List[ReviewRow] = []
    review_fields: set[str] = set()
    today_text = dt.date.today().isoformat()

    def add_review(field_name: str, input_value: Any, fallback: Any = "") -> None:
        """Add a review row once per field name for this invoice."""

        if field_name in review_fields:
            return
        review_fields.add(field_name)
        review_rows.append(build_review_row(invoice_id, field_name, input_value, fallback))

    line_items_raw = flattened.get("Line_Items")
    subtotal_raw = flattened.get("Subtotal")
    subtotal_missing = is_missing_value(subtotal_raw)
    subtotal_value = try_parse_decimal(subtotal_raw)
    subtotal_invalid = not subtotal_missing and (subtotal_value is None or subtotal_value < 0)

    invoice_date_raw = flattened.get("Invoice_Date")
    due_date_raw = flattened.get("Due_Date")
    if not is_missing_value(invoice_date_raw) and try_parse_iso_date(invoice_date_raw) is None:
        add_review("Invoice Date", invoice_date_raw, today_text)
    if not is_missing_value(due_date_raw) and try_parse_iso_date(due_date_raw) is None:
        add_review("Due Date", due_date_raw, today_text)

    if customer_name is None:
        return InspectionResult(
            invoice_id=invoice_id,
            file_label=file_label,
            invoice_number=invoice_number,
            failure_row=build_failure_row(
                invoice_id,
                file_label,
                invoice_number,
                "missing customer name",
                customer_name,
                line_items_raw,
            ),
        )

    parsed_items, line_items_invalid = try_parse_line_items(line_items_raw)
    if line_items_invalid or parsed_items is None or not parsed_items:
        if subtotal_missing:
            return InspectionResult(
                invoice_id=invoice_id,
                file_label=file_label,
                invoice_number=invoice_number,
                failure_row=build_failure_row(
                    invoice_id,
                    file_label,
                    invoice_number,
                    "missing line items and subtotal",
                    customer_name,
                    line_items_raw,
                ),
            )
        if subtotal_invalid:
            add_review("Subtotal", subtotal_raw)
            return InspectionResult(invoice_id, file_label, invoice_number, review_rows=review_rows)

        add_review("Combined Line Item Amount", "missing line items", subtotal_value)
        return InspectionResult(invoice_id, file_label, invoice_number, review_rows=review_rows)

    analyses = [analyze_line_item(item, item_index) for item_index, item in enumerate(parsed_items, start=1)]
    unit_price_broken = any(analysis.unit_price_problem for analysis in analyses)

    if unit_price_broken:
        if subtotal_missing:
            return InspectionResult(
                invoice_id=invoice_id,
                file_label=file_label,
                invoice_number=invoice_number,
                failure_row=build_failure_row(
                    invoice_id,
                    file_label,
                    invoice_number,
                    "missing line-item amount and subtotal",
                    customer_name,
                    line_items_raw,
                ),
            )
        if subtotal_invalid:
            add_review("Subtotal", subtotal_raw)
            return InspectionResult(invoice_id, file_label, invoice_number, review_rows=review_rows)

        valid_sum = Decimal("0")
        broken_group: List[LineItemAnalysis] = []
        for analysis in analyses:
            if analysis.unit_price_problem or analysis.quantity_problem:
                broken_group.append(analysis)
                continue
            valid_sum += analysis.quantity * analysis.unit_price

        combined_amount = subtotal_value - valid_sum
        combined_description = build_combined_line_description(broken_group)
        add_review("Combined Line Item Amount", combined_description, combined_amount)
        return InspectionResult(invoice_id, file_label, invoice_number, review_rows=review_rows)

    clean_line_items: List[NormalizedLineItem] = []
    for analysis in analyses:
        if analysis.quantity_problem:
            add_review(f"Line {analysis.index} Quantity", analysis.raw_quantity, "1")
            continue
        clean_line_items.append(
            NormalizedLineItem(
                name=analysis.name,
                quantity=analysis.quantity,
                unit_price=analysis.unit_price,
            )
        )

    if review_rows:
        return InspectionResult(invoice_id, file_label, invoice_number, review_rows=review_rows)

    return InspectionResult(
        invoice_id=invoice_id,
        file_label=file_label,
        invoice_number=invoice_number,
        clean_invoice=build_clean_invoice_from_flattened(invoice_id, flattened, clean_line_items),
    )


def review_row_is_resolved(row: ReviewRow) -> bool:
    """Return `True` when a review row has a usable human decision.

    Args:
        row: Review row loaded from or written to `review.csv`.
    """

    if row.manual_adjustment.strip():
        return True
    return row.use_fallback.strip().lower() == "x" and bool(row.fallback.strip())


def review_row_selected_value(row: ReviewRow) -> Optional[str]:
    """Return the resolved value selected by the reviewer, if any.

    Args:
        row: Review row loaded from or written to `review.csv`.
    """

    manual_value = row.manual_adjustment.strip()
    if manual_value:
        return manual_value
    if row.use_fallback.strip().lower() == "x" and row.fallback.strip():
        return row.fallback.strip()
    return None


def review_rows_to_map(rows: List[ReviewRow]) -> Dict[str, Dict[str, ReviewRow]]:
    """Index review rows by invoice ID and field name.

    Args:
        rows: Review rows loaded from `review.csv`.
    """

    indexed: Dict[str, Dict[str, ReviewRow]] = {}
    for row in rows:
        indexed.setdefault(row.invoice_id, {})[row.field_name] = row
    return indexed


def resolve_date_for_runtime(raw_value: Any, field_name: str, review_lookup: Dict[str, ReviewRow]) -> Optional[str]:
    """Resolve a date field using raw data plus any reviewer decision.

    Args:
        raw_value: Raw source value for the date field.
        field_name: Review CSV field name for this date.
        review_lookup: Resolved review rows for the invoice keyed by field name.
    """

    chosen_value = raw_value
    if field_name in review_lookup:
        chosen_value = review_row_selected_value(review_lookup[field_name])
    if is_missing_value(chosen_value):
        return dt.date.today().isoformat()
    parsed = try_parse_iso_date(chosen_value)
    if parsed is None:
        return None
    return parsed.isoformat()


def resolve_subtotal_for_runtime(flattened: Dict[str, Any], review_lookup: Dict[str, ReviewRow]) -> Optional[Decimal]:
    """Resolve subtotal from raw data or a manual review override.

    Args:
        flattened: Flattened key/value map for the record.
        review_lookup: Resolved review rows for the invoice keyed by field name.
    """

    chosen_value: Any = flattened.get("Subtotal")
    if "Subtotal" in review_lookup:
        chosen_value = review_row_selected_value(review_lookup["Subtotal"])
    if is_missing_value(chosen_value):
        return None
    parsed = try_parse_decimal(chosen_value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def resolve_quantity_for_runtime(analysis: LineItemAnalysis, review_lookup: Dict[str, ReviewRow]) -> Optional[Decimal]:
    """Resolve a line-item quantity from raw data or a review decision.

    Args:
        analysis: Parsed line-item analysis from the raw record.
        review_lookup: Resolved review rows for the invoice keyed by field name.
    """

    if not analysis.quantity_problem:
        return analysis.quantity

    field_name = f"Line {analysis.index} Quantity"
    row = review_lookup.get(field_name)
    if row is None:
        return None
    selected_value = review_row_selected_value(row)
    if selected_value is None:
        return None
    parsed = try_parse_decimal(selected_value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def resolve_combined_amount_for_runtime(
    default_amount: Decimal,
    review_lookup: Dict[str, ReviewRow],
) -> Optional[Decimal]:
    """Resolve the synthetic combined-line amount from review decisions.

    Args:
        default_amount: Computed subtotal remainder when no manual override is used.
        review_lookup: Resolved review rows for the invoice keyed by field name.
    """

    row = review_lookup.get("Combined Line Item Amount")
    if row is None:
        return default_amount
    selected_value = review_row_selected_value(row)
    if selected_value is None:
        return None
    parsed = try_parse_decimal(selected_value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def build_invoice_from_record(
    record: Dict[str, Any],
    index: int,
    review_lookup: Optional[Dict[str, ReviewRow]] = None,
) -> Optional[LegacyInvoice]:
    """Build an uploadable invoice from raw data plus any resolved review rows.

    Args:
        record: One raw record object from the legacy export JSON.
        index: Zero-based position of the record in the source file.
        review_lookup: Review rows for this invoice keyed by field name.
    """

    if review_lookup is None:
        review_lookup = {}

    invoice_id = str(index + 1)
    flattened = flatten_record(record)
    customer_name = normalize_optional_string(flattened.get("Customer_Name"))
    if customer_name is None:
        return None

    invoice_date = resolve_date_for_runtime(flattened.get("Invoice_Date"), "Invoice Date", review_lookup)
    due_date = resolve_date_for_runtime(flattened.get("Due_Date"), "Due Date", review_lookup)
    if invoice_date is None or due_date is None:
        return None

    subtotal = resolve_subtotal_for_runtime(flattened, review_lookup)
    parsed_items, line_items_invalid = try_parse_line_items(flattened.get("Line_Items"))
    final_line_items: List[NormalizedLineItem] = []

    if line_items_invalid or parsed_items is None or not parsed_items:
        if subtotal is None:
            return None
        combined_amount = resolve_combined_amount_for_runtime(subtotal, review_lookup)
        if combined_amount is None:
            return None
        final_line_items.append(
            NormalizedLineItem(
                name="combined line item",
                quantity=Decimal("1"),
                unit_price=combined_amount,
                note="missing line items",
            )
        )
    else:
        analyses = [analyze_line_item(item, item_index) for item_index, item in enumerate(parsed_items, start=1)]
        unit_price_broken = any(analysis.unit_price_problem for analysis in analyses)

        if unit_price_broken:
            if subtotal is None:
                return None
            valid_sum = Decimal("0")
            broken_group: List[LineItemAnalysis] = []
            for analysis in analyses:
                if analysis.unit_price_problem or analysis.quantity_problem:
                    broken_group.append(analysis)
                    continue
                valid_sum += analysis.quantity * analysis.unit_price
                final_line_items.append(
                    NormalizedLineItem(
                        name=analysis.name,
                        quantity=analysis.quantity,
                        unit_price=analysis.unit_price,
                    )
                )
            combined_amount = resolve_combined_amount_for_runtime(subtotal - valid_sum, review_lookup)
            if combined_amount is None:
                return None
            final_line_items.append(
                NormalizedLineItem(
                    name="combined line item",
                    quantity=Decimal("1"),
                    unit_price=combined_amount,
                    note=build_combined_line_description(broken_group),
                )
            )
        else:
            for analysis in analyses:
                quantity = resolve_quantity_for_runtime(analysis, review_lookup)
                if quantity is None:
                    return None
                final_line_items.append(
                    NormalizedLineItem(
                        name=analysis.name,
                        quantity=quantity,
                        unit_price=analysis.unit_price,
                    )
                )

    return LegacyInvoice(
        invoice_id=invoice_id,
        invoice_number=str(flattened.get("Invoice_Number") or "UNKNOWN"),
        invoice_date=invoice_date,
        due_date=due_date,
        customer_name=customer_name,
        customer_email=normalize_optional_string(flattened.get("Customer_Email")),
        customer_phone=normalize_optional_string(flattened.get("Customer_Phone_Number")),
        customer_address=normalize_optional_string(flattened.get("Customer_Address")),
        service_notes=normalize_optional_string(flattened.get("Service_Notes")),
        line_items=final_line_items,
        subtotal=subtotal,
        sales_tax=parse_decimal(str(flattened.get("Sales_Tax") or "0")),
    )


class SquareClient:
    """Small wrapper around the Square REST API used by this script."""

    def __init__(self, access_token: str, location_id: str, timeout: int = 30) -> None:
        """Create a reusable API session configured for one Square location.

        Args:
            access_token: Square personal access token or OAuth access token.
            location_id: Square location where imported records should be created.
            timeout: Request timeout in seconds for each API call.
        """

        self.location_id = location_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Square-Version": "2024-12-18",
            }
        )

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send one HTTP request to Square and raise a friendly error on failure.

        Args:
            method: HTTP method such as `GET` or `POST`.
            path: API path appended to `SQUARE_API_BASE`.
            payload: Optional JSON body to send with the request.
        """

        try:
            response = self.session.request(
                method=method,
                url=f"{SQUARE_API_BASE}{path}",
                json=payload,
                timeout=self.timeout,
            )
        except RequestException as exc:
            raise SquareAPIError(f"{method} {path} request failed: {exc}") from exc
        body: Dict[str, Any] = {}
        if response.content:
            body = response.json()
        if response.status_code >= 400:
            raise SquareAPIError(f"{method} {path} failed: {response.status_code} {body}")
        return body

    @staticmethod
    def _strip_none(value: Any) -> Any:
        """Recursively remove `None` values before sending payloads to Square.

        Args:
            value: Nested payload fragment that may contain dictionaries, lists, or scalars.
        """

        if isinstance(value, dict):
            return {k: SquareClient._strip_none(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [SquareClient._strip_none(item) for item in value]
        return value

    def test_connection(self) -> Dict[str, Any]:
        """Verify credentials by listing locations visible to the access token."""

        return self._request("GET", "/v2/locations")

    def upsert_customer(self, invoice: LegacyInvoice) -> str:
        """Find an existing customer by email or create a new one for the invoice.

        Args:
            invoice: Parsed legacy invoice containing customer details to match or create.
        """

        if invoice.customer_email:
            search_payload = {
                "query": {"filter": {"email_address": {"exact": invoice.customer_email}}},
                "limit": 1,
            }
            result = self._request("POST", "/v2/customers/search", search_payload)
            customers = result.get("customers") or []
            if customers:
                return customers[0]["id"]

        create_payload = {
            "idempotency_key": str(uuid.uuid4()),
            "given_name": invoice.customer_name,
            "email_address": invoice.customer_email,
            "phone_number": invoice.customer_phone,
            "address": {"address_line_1": invoice.customer_address} if invoice.customer_address else None,
            "reference_id": f"legacy-customer-{invoice.invoice_number}",
        }
        create_payload = {k: v for k, v in create_payload.items() if v}
        result = self._request("POST", "/v2/customers", create_payload)
        return result["customer"]["id"]

    def create_order(self, invoice: LegacyInvoice, customer_id: str) -> str:
        """Create the Square order that the invoice will bill against.

        Args:
            invoice: Parsed legacy invoice whose line items will populate the order.
            customer_id: Square customer ID that should own the order.
        """

        lines = []
        for item in invoice.line_items:
            lines.append(
                {
                    "name": item.name or "Service",
                    "quantity": quantity_to_text(item.quantity),
                    "base_price_money": {
                        "amount": money_to_cents(item.unit_price),
                        "currency": DEFAULT_CURRENCY,
                    },
                }
            )

        if not lines and invoice.subtotal is not None:
            # Keep a final subtotal-based safeguard if upstream logic ever hands us no lines.
            lines.append(
                {
                    "name": "combined line item",
                    "quantity": "1",
                    "base_price_money": {
                        "amount": money_to_cents(invoice.subtotal),
                        "currency": DEFAULT_CURRENCY,
                    },
                }
            )

        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "order": {
                "location_id": self.location_id,
                "customer_id": customer_id,
                "line_items": lines,
                "reference_id": f"legacy-invoice-{invoice.invoice_number}",
            },
        }
        response = self._request("POST", "/v2/orders", payload)
        return response["order"]["id"]

    def create_invoice(self, invoice: LegacyInvoice, order_id: str, customer_id: str) -> Dict[str, Any]:
        """Create a draft Square invoice linked to the previously created order.

        Args:
            invoice: Parsed legacy invoice containing dates, notes, and invoice metadata.
            order_id: Square order ID created for the invoice charges.
            customer_id: Square customer ID for the invoice recipient.
        """

        today = dt.date.today()
        due_date_obj = parse_iso_date(invoice.due_date, fallback=today)
        adjusted_due_date = normalize_due_date(due_date_obj, today=today)
        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "invoice": {
                "location_id": self.location_id,
                "order_id": order_id,
                "primary_recipient": {"customer_id": customer_id},
                "payment_requests": [
                    {
                        "request_type": "BALANCE",
                        "due_date": adjusted_due_date.isoformat(),
                        "tipping_enabled": False,
                        "automatic_payment_source": "NONE",
                    }
                ],
                "delivery_method": "SHARE_MANUALLY",
                "title": f"Legacy Invoice {invoice.invoice_number}",
                "description": invoice.service_notes or "Imported from historical invoice records.",
                "invoice_number": f"LEGACY-{invoice.invoice_number}-{int(time.time())}",
                "accepted_payment_methods": {
                    "card": True,
                    "square_gift_card": False,
                    "bank_account": False,
                    "buy_now_pay_later": False,
                    "cash_app_pay": False,
                },
                "sale_or_service_date": invoice.invoice_date,
            },
        }
        payload = self._strip_none(payload)
        response = self._request("POST", "/v2/invoices", payload)
        return response["invoice"]

    def publish_invoice(self, invoice_id: str, version: int) -> Dict[str, Any]:
        """Publish a draft invoice so it becomes an active Square invoice.

        Args:
            invoice_id: Square invoice ID for the draft invoice.
            version: Current optimistic-lock version of the invoice object.
        """

        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "version": version,
        }
        response = self._request("POST", f"/v2/invoices/{invoice_id}/publish", payload)
        return response["invoice"]

    def cancel_invoice(self, invoice_id: str, version: int) -> Dict[str, Any]:
        """Cancel a published invoice to mark the imported record as closed out.

        Args:
            invoice_id: Square invoice ID for the published invoice.
            version: Current optimistic-lock version of the invoice object.
        """

        payload = {
            "version": version,
        }
        response = self._request("POST", f"/v2/invoices/{invoice_id}/cancel", payload)
        return response["invoice"]


def load_legacy_records(path: str) -> List[Dict[str, Any]]:
    """Load the raw legacy export records from disk.

    Args:
        path: Filesystem path to the JSON export file.
    """

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw.get("records") or []


def get_client_from_env() -> SquareClient:
    """Build a `SquareClient` from the required environment variables."""

    token = os.getenv("SQUARE_ACCESS_TOKEN")
    location_id = os.getenv("SQUARE_LOCATION_ID")
    if not token or not location_id:
        raise SystemExit("Missing SQUARE_ACCESS_TOKEN or SQUARE_LOCATION_ID environment variables.")
    return SquareClient(access_token=token, location_id=location_id)


def load_batch_registry() -> Dict[str, Any]:
    """Load the batch registry JSON from the app root."""

    if not os.path.exists(BATCH_REGISTRY_PATH):
        return {}
    with open(BATCH_REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_batch_registry(registry: Dict[str, Any]) -> None:
    """Persist the batch registry JSON to the app root.

    Args:
        registry: Registry payload to save.
    """

    with open(BATCH_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, sort_keys=True)


def ensure_batch_paths(batch_id: str, batch_root: str) -> BatchPaths:
    """Resolve and create filesystem paths for one batch.

    Args:
        batch_id: User-entered batch identifier.
        batch_root: Directory under which batch folders are created.
    """

    batch_dir = os.path.abspath(os.path.join(batch_root, batch_id))
    os.makedirs(batch_dir, exist_ok=True)
    return BatchPaths(
        batch_id=batch_id,
        batch_dir=batch_dir,
        review_csv_path=os.path.join(batch_dir, REVIEW_CSV_NAME),
        failures_csv_path=os.path.join(batch_dir, FAILURES_CSV_NAME),
    )


def validate_existing_batch(
    registry_entry: Dict[str, Any],
    batch_paths: BatchPaths,
    input_path: str,
    record_count: int,
) -> None:
    """Fail fast when a batch is rerun against a different input shape.

    Args:
        registry_entry: Existing batch metadata loaded from the registry.
        batch_paths: Filesystem paths resolved for the current batch invocation.
        input_path: Absolute input JSON path passed to the CLI.
        record_count: Number of records loaded from that input.
    """

    if registry_entry.get("input_path") != input_path:
        raise SystemExit("Batch ID already exists for a different input file.")
    if registry_entry.get("record_count") != record_count:
        raise SystemExit("Batch ID already exists with a different record count.")
    if registry_entry.get("batch_dir") != batch_paths.batch_dir:
        raise SystemExit("Batch ID already exists in a different batch directory.")


def upsert_batch_registry_entry(
    registry: Dict[str, Any],
    batch_paths: BatchPaths,
    input_path: str,
    record_count: int,
    stored_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Create or update the registry entry for one batch.

    Args:
        registry: Entire batch registry object.
        batch_paths: Filesystem paths resolved for the current batch invocation.
        input_path: Absolute input JSON path passed to the CLI.
        record_count: Number of records loaded from that input.
        stored_summary: Optional summary payload to persist for later dry-run reuse.
    """

    entry = registry.setdefault(batch_paths.batch_id, {})
    entry.update(
        {
            "batch_id": batch_paths.batch_id,
            "batch_dir": batch_paths.batch_dir,
            "input_path": input_path,
            "record_count": record_count,
            "invoice_ids": [str(index + 1) for index in range(record_count)],
            "review_csv_path": batch_paths.review_csv_path,
            "failures_csv_path": batch_paths.failures_csv_path,
        }
    )
    if stored_summary is not None:
        entry["stored_summary"] = stored_summary


def write_review_csv(path: str, rows: List[ReviewRow]) -> None:
    """Write `review.csv`, preserving only the columns used by the workflow.

    Args:
        path: Destination CSV path.
        rows: Review rows to write.
    """

    fieldnames = ["invoice_id", "field_name", "input", "fallback", "use_fallback", "manual_adjustment"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_row())


def write_failures_csv(path: str, rows: List[FailureRow]) -> None:
    """Write `failures.csv` for permanently failed invoices.

    Args:
        path: Destination CSV path.
        rows: Failure rows to write.
    """

    fieldnames = [
        "invoice_id",
        "file_label",
        "invoice_number",
        "failure_reason",
        "customer_name",
        "line_items_input",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_row())


def load_review_csv(path: str) -> List[ReviewRow]:
    """Load review rows from an existing `review.csv`.

    Args:
        path: CSV path to load.
    """

    if not os.path.exists(path):
        return []
    rows: List[ReviewRow] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                ReviewRow(
                    invoice_id=row.get("invoice_id", ""),
                    field_name=row.get("field_name", ""),
                    input_value=row.get("input", ""),
                    fallback=row.get("fallback", ""),
                    use_fallback=row.get("use_fallback", ""),
                    manual_adjustment=row.get("manual_adjustment", ""),
                )
            )
    return rows


def load_failures_csv(path: str) -> List[FailureRow]:
    """Load permanent-failure rows from an existing `failures.csv`.

    Args:
        path: CSV path to load.
    """

    if not os.path.exists(path):
        return []
    rows: List[FailureRow] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                FailureRow(
                    invoice_id=row.get("invoice_id", ""),
                    file_label=row.get("file_label", ""),
                    invoice_number=row.get("invoice_number", ""),
                    failure_reason=row.get("failure_reason", ""),
                    customer_name=row.get("customer_name", ""),
                    line_items_input=row.get("line_items_input", ""),
                )
            )
    return rows


def classify_records(records: List[Dict[str, Any]]) -> List[InspectionResult]:
    """Inspect every record in the input file.

    Args:
        records: Raw records loaded from the legacy JSON input.
    """

    return [inspect_record(record, index) for index, record in enumerate(records)]


def collect_review_rows(results: List[InspectionResult]) -> List[ReviewRow]:
    """Flatten review rows across all inspected invoices.

    Args:
        results: Batch inspection results.
    """

    return [row for result in results for row in result.review_rows]


def collect_failure_rows(results: List[InspectionResult]) -> List[FailureRow]:
    """Flatten failure rows across all inspected invoices.

    Args:
        results: Batch inspection results.
    """

    return [result.failure_row for result in results if result.failure_row is not None]


def build_dry_run_summary(results: List[InspectionResult]) -> Dict[str, Any]:
    """Create the summary payload printed and stored for dry-run.

    Args:
        results: Batch inspection results.
    """

    return {
        "mode": "dry-run",
        "files_processed": len(results),
        "clean_invoices": sum(1 for result in results if result.is_clean),
        "queued_for_review": sum(1 for result in results if result.is_reviewable),
        "permanent_failures": sum(1 for result in results if result.is_failure),
    }


def print_summary(summary: Dict[str, Any]) -> None:
    """Print a compact terminal summary for dry-run or production.

    Args:
        summary: Summary payload to render.
    """

    title = "Dry-run summary:" if summary.get("mode") == "dry-run" else "Import summary:"
    print(title)
    for key, value in summary.items():
        if key == "mode":
            continue
        label = key.replace("_", " ").capitalize()
        print(f"- {label}: {value}")


def run_test_connection() -> int:
    """CLI handler for validating Square credentials and location access."""

    client = get_client_from_env()
    result = client.test_connection()
    locations = result.get("locations", [])
    print(f"Connection successful. Visible locations: {len(locations)}")
    for loc in locations:
        print(f"- {loc.get('id')} :: {loc.get('name')}")
    return 0


def run_initial_dry_run(
    records: List[Dict[str, Any]],
    registry: Dict[str, Any],
    batch_paths: BatchPaths,
    input_path: str,
) -> int:
    """Perform the first dry-run for a batch and persist review artifacts.

    Args:
        records: Raw legacy records loaded from input.
        registry: Loaded batch registry object.
        batch_paths: Filesystem paths for the batch.
        input_path: Absolute input JSON path.
    """

    results = classify_records(records)
    review_rows = collect_review_rows(results)
    failure_rows = collect_failure_rows(results)
    summary = build_dry_run_summary(results)

    write_review_csv(batch_paths.review_csv_path, review_rows)
    write_failures_csv(batch_paths.failures_csv_path, failure_rows)
    upsert_batch_registry_entry(registry, batch_paths, input_path, len(records), stored_summary=summary)
    save_batch_registry(registry)

    print_summary(summary)
    return 1 if failure_rows else 0


def run_dry_run(
    records: List[Dict[str, Any]],
    registry: Dict[str, Any],
    batch_paths: BatchPaths,
    input_path: str,
) -> int:
    """Run the batch dry-run workflow.

    Args:
        records: Raw legacy records loaded from input.
        registry: Loaded batch registry object.
        batch_paths: Filesystem paths for the batch.
        input_path: Absolute input JSON path.
    """

    entry = registry.get(batch_paths.batch_id)
    if entry is not None:
        validate_existing_batch(entry, batch_paths, input_path, len(records))
        if os.path.exists(batch_paths.review_csv_path):
            stored_summary = entry.get("stored_summary")
            if stored_summary is None:
                raise SystemExit("Batch exists but has no stored summary to reuse.")
            print_summary(stored_summary)
            return 0

    return run_initial_dry_run(records, registry, batch_paths, input_path)


def run_production_import(
    records: List[Dict[str, Any]],
    registry: Dict[str, Any],
    batch_paths: BatchPaths,
    input_path: str,
) -> int:
    """Run the production import workflow with persisted human review state.

    Args:
        records: Raw legacy records loaded from input.
        registry: Loaded batch registry object.
        batch_paths: Filesystem paths for the batch.
        input_path: Absolute input JSON path.
    """

    entry = registry.get(batch_paths.batch_id)
    if entry is not None:
        validate_existing_batch(entry, batch_paths, input_path, len(records))

    if entry is None or not os.path.exists(batch_paths.review_csv_path):
        # First production run still classifies the batch so unresolved issues never reach the API.
        results = classify_records(records)
        write_review_csv(batch_paths.review_csv_path, collect_review_rows(results))
        write_failures_csv(batch_paths.failures_csv_path, collect_failure_rows(results))
        upsert_batch_registry_entry(
            registry,
            batch_paths,
            input_path,
            len(records),
            stored_summary=build_dry_run_summary(results),
        )
        save_batch_registry(registry)
    else:
        upsert_batch_registry_entry(registry, batch_paths, input_path, len(records))
        save_batch_registry(registry)

    failure_rows = load_failures_csv(batch_paths.failures_csv_path)
    review_rows = load_review_csv(batch_paths.review_csv_path)
    failure_ids = {row.invoice_id for row in failure_rows}
    review_lookup_by_invoice = review_rows_to_map(review_rows)

    client: Optional[SquareClient] = None
    processed = 0
    queued_for_review = 0
    skipped_due_to_unresolved_review_rows = 0
    skipped_due_to_failures = 0
    permanent_failures = len(failure_rows)
    api_errors = 0

    for index, record in enumerate(records):
        invoice_id = str(index + 1)
        if invoice_id in failure_ids:
            skipped_due_to_failures += 1
            continue

        inspection = inspect_record(record, index)
        if inspection.is_failure:
            # Existing failures are already captured on first classification.
            skipped_due_to_failures += 1
            continue

        invoice_review_lookup = review_lookup_by_invoice.get(invoice_id, {})
        if inspection.review_rows:
            unresolved = False
            for expected_row in inspection.review_rows:
                stored_row = invoice_review_lookup.get(expected_row.field_name)
                if stored_row is None or not review_row_is_resolved(stored_row):
                    unresolved = True
                    break
            if unresolved:
                queued_for_review += 1
                skipped_due_to_unresolved_review_rows += 1
                continue

        invoice = inspection.clean_invoice or build_invoice_from_record(record, index, invoice_review_lookup)
        if invoice is None:
            queued_for_review += 1
            skipped_due_to_unresolved_review_rows += 1
            continue

        try:
            if client is None:
                client = get_client_from_env()
            customer_id = client.upsert_customer(invoice)
            created_order_id = client.create_order(invoice, customer_id)
            draft = client.create_invoice(invoice, created_order_id, customer_id)
            published = client.publish_invoice(draft["id"], draft["version"])
            client.cancel_invoice(published["id"], published["version"])
            processed += 1
        except SquareAPIError:
            api_errors += 1

    summary = {
        "mode": "production",
        "files_processed": len(records),
        "processed": processed,
        "queued_for_review": queued_for_review,
        "skipped_due_to_unresolved_review_rows": skipped_due_to_unresolved_review_rows,
        "skipped_due_to_failures": skipped_due_to_failures,
        "permanent_failures": permanent_failures,
        "api_errors": api_errors,
    }
    print_summary(summary)
    return 1 if skipped_due_to_failures or skipped_due_to_unresolved_review_rows or api_errors else 0


def run_import(path: str, dry_run: bool, batch_id: str, batch_root: str) -> int:
    """CLI handler that validates/imports a batch of legacy invoices.

    Args:
        path: Filesystem path to the legacy invoice JSON file.
        dry_run: When `True`, build review artifacts without calling Square APIs.
        batch_id: User-entered batch identifier that groups review artifacts together.
        batch_root: Directory under which batch folders are created.
    """

    records = load_legacy_records(path)
    input_path = os.path.abspath(path)
    batch_paths = ensure_batch_paths(batch_id, batch_root)
    registry = load_batch_registry()

    if dry_run:
        return run_dry_run(records, registry, batch_paths, input_path)
    return run_production_import(records, registry, batch_paths, input_path)


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface for the utility."""

    parser = argparse.ArgumentParser(description="Backload legacy invoices to Square.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_cmd = subparsers.add_parser("test-connection", help="Validate Square auth and location access.")
    test_cmd.set_defaults(func=lambda args: run_test_connection())

    import_cmd = subparsers.add_parser("import", help="Import legacy invoice JSON into Square.")
    import_cmd.add_argument("--input", required=True, help="Path to JSON payload (same structure as results.json)")
    import_cmd.add_argument("--dry-run", action="store_true", help="Build review files without calling Square APIs")
    import_cmd.add_argument("--batch-id", required=True, help="User-defined batch identifier for this import set")
    import_cmd.add_argument("--batch-root", required=True, help="Directory where batch review artifacts are stored")
    import_cmd.set_defaults(func=lambda args: run_import(args.input, args.dry_run, args.batch_id, args.batch_root))

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Parse CLI arguments and dispatch to the selected subcommand.

    Args:
        argv: Optional iterable of command-line arguments for testing or embedding.
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
