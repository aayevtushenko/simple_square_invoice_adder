#!/usr/bin/env python3
"""Backload legacy invoices into Square and close them out by cancellation."""

# TODO: add default phone number for when the number fails validation

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import json
import os
import time
import sys
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


class SquareAPIError(RuntimeError):
    """Raised when a Square API request fails or returns an error response."""

    pass


@dataclass
class LegacyInvoice:
    """Normalized invoice data extracted from the legacy JSON export."""

    invoice_number: str
    invoice_date: str
    due_date: str
    customer_name: str
    customer_email: Optional[str]
    customer_phone: Optional[str]
    customer_address: Optional[str]
    service_notes: Optional[str]
    line_items: List[Dict[str, Any]]
    subtotal: Decimal
    sales_tax: Decimal
    total_due: Decimal


@dataclass
class DryRunRecordReport:
    """Validation results for one legacy record during dry-run execution."""

    file_label: str
    invoice_number: str
    is_failure: bool = False
    failure_reasons: List[str] = field(default_factory=list)
    fallback_events: List[str] = field(default_factory=list)
    missing_events: List[str] = field(default_factory=list)
    report_only_events: List[str] = field(default_factory=list)
    invalid_events: List[str] = field(default_factory=list)
    negative_events: List[str] = field(default_factory=list)

    def add_failure_reason(self, reason: str) -> None:
        """Record one failure reason and mark the report as failed.

        Args:
            reason: Human-readable explanation of why the record is not processable.
        """

        self.is_failure = True
        if reason not in self.failure_reasons:
            self.failure_reasons.append(reason)

    def add_fallback_event(self, event: str) -> None:
        """Record that a present value would trigger parser fallback.

        Args:
            event: Field identifier used in the summary output.
        """

        if event not in self.fallback_events:
            self.fallback_events.append(event)

    def add_missing_event(self, event: str, *, report_only: bool = False) -> None:
        """Record that a required or notable value is missing.

        Args:
            event: Field identifier used in the summary output.
            report_only: Whether the missing value should be tracked without failing the record.
        """

        if event not in self.missing_events:
            self.missing_events.append(event)
        if report_only and event not in self.report_only_events:
            self.report_only_events.append(event)

    def add_invalid_event(self, event: str) -> None:
        """Record that a field contains an invalid non-missing value.

        Args:
            event: Field identifier used in the summary output.
        """

        if event not in self.invalid_events:
            self.invalid_events.append(event)

    def add_negative_event(self, event: str) -> None:
        """Record that a numeric field contains a negative value.

        Args:
            event: Field identifier used in the summary output.
        """

        if event not in self.negative_events:
            self.negative_events.append(event)


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


def validate_optional_date_field(value: Any, field_name: str, report: DryRunRecordReport) -> None:
    """Track parse fallback for an optional date field when a present value is invalid.

    Args:
        value: Raw date value to validate.
        field_name: Summary/report field name.
        report: Dry-run report being populated.
    """

    if is_missing_value(value):
        return
    if try_parse_iso_date(value) is None:
        report.add_fallback_event(field_name)


def validate_nonnegative_decimal_field(
    value: Any,
    field_name: str,
    report: DryRunRecordReport,
    *,
    missing_behavior: str,
    context_label: str,
) -> None:
    """Validate one numeric field under the dry-run rules.

    Args:
        value: Raw numeric value to validate.
        field_name: Summary/report field name.
        report: Dry-run report being populated.
        missing_behavior: Either `fail` or `report_only` for missing values.
        context_label: Human-readable field label for per-record failure messages.
    """

    if is_missing_value(value):
        report.add_missing_event(field_name, report_only=missing_behavior == "report_only")
        if missing_behavior == "fail":
            report.add_failure_reason(f"missing {context_label}")
        return

    parsed_value = try_parse_decimal(value)
    if parsed_value is None:
        report.add_fallback_event(field_name)
        report.add_invalid_event(field_name)
        report.add_failure_reason(f"invalid {context_label}")
        return

    if parsed_value < 0:
        report.add_negative_event(field_name)
        report.add_failure_reason(f"negative {context_label}")


def validate_line_items(value: Any, report: DryRunRecordReport) -> None:
    """Validate line-item structure and required numeric fields for dry-run.

    Args:
        value: Raw `Line_Items` value from the legacy export.
        report: Dry-run report being populated.
    """

    if is_missing_value(value):
        report.add_missing_event("Line_Items")
        report.add_failure_reason("missing Line_Items")
        return

    parsed_items, is_invalid = try_parse_line_items(value)
    if is_invalid or parsed_items is None:
        report.add_invalid_event("Line_Items")
        report.add_failure_reason("invalid Line_Items")
        return

    if not parsed_items:
        report.add_invalid_event("Line_Items")
        report.add_failure_reason("Line_Items is empty")
        return

    for index, item in enumerate(parsed_items, start=1):
        if not isinstance(item, dict):
            report.add_invalid_event("Line_Items")
            report.add_failure_reason(f"line item {index} is not an object")
            continue

        validate_nonnegative_decimal_field(
            item.get("Quantity"),
            "Line_Items.Quantity",
            report,
            missing_behavior="fail",
            context_label=f"line item {index} Quantity",
        )
        validate_nonnegative_decimal_field(
            item.get("Unit Price"),
            "Line_Items.Unit Price",
            report,
            missing_behavior="fail",
            context_label=f"line item {index} Unit Price",
        )
        validate_nonnegative_decimal_field(
            item.get("Line Total"),
            "Line_Items.Line Total",
            report,
            missing_behavior="fail",
            context_label=f"line item {index} Line Total",
        )


def validate_record_for_dry_run(record: Dict[str, Any], index: int) -> DryRunRecordReport:
    """Validate one raw record and return the dry-run reporting data.

    Args:
        record: One raw record object from the legacy export JSON.
        index: Zero-based position of the record in the source file.
    """

    flattened = flatten_record(record)
    invoice_number = str(flattened.get("Invoice_Number") or "UNKNOWN")
    report = DryRunRecordReport(
        file_label=get_record_file_label(record, index),
        invoice_number=invoice_number,
    )

    validate_optional_date_field(flattened.get("Invoice_Date"), "Invoice_Date", report)
    validate_optional_date_field(flattened.get("Due_Date"), "Due_Date", report)
    validate_nonnegative_decimal_field(
        flattened.get("Subtotal"),
        "Subtotal",
        report,
        missing_behavior="fail",
        context_label="Subtotal",
    )
    validate_nonnegative_decimal_field(
        flattened.get("Total_Amount_Due"),
        "Total_Amount_Due",
        report,
        missing_behavior="report_only",
        context_label="Total_Amount_Due",
    )
    validate_line_items(flattened.get("Line_Items"), report)
    return report


def format_record_status(report: DryRunRecordReport) -> str:
    """Format a compact dry-run status line for one record.

    Args:
        report: Dry-run report for one record.
    """

    status = "FAIL" if report.is_failure else "OK"
    details: List[str] = []

    if report.is_failure and report.failure_reasons:
        preview = report.failure_reasons[:3]
        reason_text = "; ".join(preview)
        if len(report.failure_reasons) > 3:
            reason_text += f"; +{len(report.failure_reasons) - 3} more"
        details.append(reason_text)
    if report.fallback_events:
        details.append(f"fallbacks: {', '.join(report.fallback_events)}")
    if report.report_only_events:
        details.append(f"report-only: {', '.join(report.report_only_events)}")

    suffix = f" :: {' | '.join(details)}" if details else ""
    return f"[DRY-RUN] {status} {report.file_label} (invoice #{report.invoice_number}){suffix}"


def print_dry_run_summary(reports: List[DryRunRecordReport]) -> None:
    """Print overall and per-field dry-run validation totals.

    Args:
        reports: All dry-run reports generated for the import input.
    """

    fallback_counts = Counter(event for report in reports for event in report.fallback_events)
    missing_counts = Counter(event for report in reports for event in report.missing_events)
    invalid_counts = Counter(event for report in reports for event in report.invalid_events)
    negative_counts = Counter(event for report in reports for event in report.negative_events)
    report_only_counts = Counter(event for report in reports for event in report.report_only_events)

    print("\nDry-run summary:")
    print(f"- Files processed: {len(reports)}")
    print(f"- Success: {sum(1 for report in reports if not report.is_failure)}")
    print(f"- Failed: {sum(1 for report in reports if report.is_failure)}")
    print(f"- Files with parse fallbacks: {sum(1 for report in reports if report.fallback_events)}")

    if fallback_counts:
        print("- Fallback counts:")
        for field_name in sorted(fallback_counts):
            print(f"  - {field_name}: {fallback_counts[field_name]}")

    if missing_counts:
        print("- Missing counts:")
        for field_name in sorted(missing_counts):
            suffix = " (report-only)" if field_name in report_only_counts else ""
            print(f"  - {field_name}: {missing_counts[field_name]}{suffix}")

    if invalid_counts:
        print("- Invalid counts:")
        for field_name in sorted(invalid_counts):
            print(f"  - {field_name}: {invalid_counts[field_name]}")

    if negative_counts:
        print("- Negative counts:")
        for field_name in sorted(negative_counts):
            print(f"  - {field_name}: {negative_counts[field_name]}")


def parse_record(record: Dict[str, Any]) -> LegacyInvoice:
    """Flatten one legacy export record into a strongly typed `LegacyInvoice`.

    Args:
        record: One raw record object from the legacy export JSON.
    """

    flattened = flatten_record(record)
    line_items, _ = try_parse_line_items(flattened.get("Line_Items"))
    if line_items is None:
        line_items = []

    return LegacyInvoice(
        invoice_number=str(flattened.get("Invoice_Number") or "UNKNOWN"),
        invoice_date=str(flattened.get("Invoice_Date") or dt.date.today().isoformat()),
        due_date=str(flattened.get("Due_Date") or dt.date.today().isoformat()),
        customer_name=str(flattened.get("Customer_Name") or "Unknown Customer"),
        customer_email=flattened.get("Customer_Email"),
        customer_phone=flattened.get("Customer_Phone_Number"),
        customer_address=flattened.get("Customer_Address"),
        service_notes=flattened.get("Service_Notes"),
        line_items=line_items,
        subtotal=parse_decimal(flattened.get("Subtotal")),
        sales_tax=parse_decimal(flattened.get("Sales_Tax")),
        total_due=parse_decimal(flattened.get("Total_Amount_Due")),
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
            # Email is the best stable identifier available in the legacy data.
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
        for raw in invoice.line_items:
            qty = str(raw.get("Quantity") or "1")
            unit_price = parse_decimal(str(raw.get("Unit Price") or "0"))
            lines.append(
                {
                    "name": str(raw.get("Description") or "Service"),
                    "quantity": qty,
                    "base_price_money": {
                        "amount": money_to_cents(unit_price),
                        "currency": DEFAULT_CURRENCY,
                    },
                }
            )

        if not lines:
            # If the legacy payload has no structured line items, preserve the total anyway.
            lines.append(
                {
                    "name": "Imported legacy invoice total",
                    "quantity": "1",
                    "base_price_money": {
                        "amount": money_to_cents(invoice.total_due), #TODO: set to subtotal to avoid double taxation. if not subtotal use total_due
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


def load_legacy_invoices(path: str) -> List[LegacyInvoice]:
    """Load the legacy export file and parse each record into a `LegacyInvoice`.

    Args:
        path: Filesystem path to the JSON export file.
    """

    records = load_legacy_records(path)
    return [parse_record(record) for record in records]


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


def run_test_connection() -> int:
    """CLI handler for validating Square credentials and location access."""

    client = get_client_from_env()
    result = client.test_connection()
    locations = result.get("locations", [])
    print(f"Connection successful. Visible locations: {len(locations)}")
    for loc in locations:
        print(f"- {loc.get('id')} :: {loc.get('name')}")
    return 0


def run_import(path: str, dry_run: bool) -> int:
    """CLI handler that imports each legacy invoice through the full Square flow.

    Args:
        path: Filesystem path to the legacy invoice JSON file.
        dry_run: When `True`, print intended actions without calling Square APIs.
    """

    records = load_legacy_records(path)
    print(f"Loaded {len(records)} invoices from {path}")

    if dry_run:
        reports = [validate_record_for_dry_run(record, index) for index, record in enumerate(records)]
        for report in reports:
            print(format_record_status(report))
        print_dry_run_summary(reports)
        return 1 if any(report.is_failure for report in reports) else 0

    client = get_client_from_env()
    invoices = [parse_record(record) for record in records]

    succeeded = 0
    failed = 0

    for invoice in invoices:
        print(f"\nProcessing invoice #{invoice.invoice_number} for {invoice.customer_name}")
        if dry_run:
            # Dry-run mode confirms parsing and intended actions without mutating Square.
            print("[DRY-RUN] would create customer, order, invoice, publish, and cancel (close-out strategy).")
            succeeded += 1
            continue

        try:
            # The import strategy is: ensure customer -> create order -> draft invoice
            # -> publish invoice -> cancel invoice so the historical record ends closed.
            customer_id = client.upsert_customer(invoice)
            created_order_id = client.create_order(invoice, customer_id)
            draft = client.create_invoice(invoice, created_order_id, customer_id)
            published = client.publish_invoice(draft["id"], draft["version"])
            canceled = client.cancel_invoice(published["id"], published["version"])

            print(f"Invoice {published['id']} published then canceled (status={canceled.get('status')}).")
            succeeded += 1
        except SquareAPIError as exc:
            failed += 1
            print(f"ERROR: {exc}")

    print(f"\nImport complete. Success: {succeeded}, Failed: {failed}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface for the utility."""

    parser = argparse.ArgumentParser(description="Backload legacy invoices to Square.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_cmd = subparsers.add_parser("test-connection", help="Validate Square auth and location access.")
    test_cmd.set_defaults(func=lambda args: run_test_connection())

    import_cmd = subparsers.add_parser("import", help="Import legacy invoice JSON into Square.")
    import_cmd.add_argument("--input", required=True, help="Path to JSON payload (same structure as results.json)")
    import_cmd.add_argument("--dry-run", action="store_true", help="Parse and print actions without calling Square APIs")
    import_cmd.set_defaults(func=lambda args: run_import(args.input, args.dry_run))

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
