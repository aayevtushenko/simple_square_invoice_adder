#!/usr/bin/env python3
"""Backload legacy invoices into Square and optionally mark them paid."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests import RequestException

SQUARE_API_BASE = "https://connect.squareup.com"
DEFAULT_CURRENCY = "USD"


class SquareAPIError(RuntimeError):
    pass


@dataclass
class LegacyInvoice:
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


def money_to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1")))


def parse_decimal(value: str, *, default: str = "0") -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return Decimal(default)


def parse_iso_date(value: Optional[str], *, fallback: dt.date) -> dt.date:
    if not value:
        return fallback
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return fallback


def parse_record(record: Dict[str, Any]) -> LegacyInvoice:
    flattened = {item["key"]: item.get("value") for item in record.get("results", [])}

    line_items_raw = flattened.get("Line_Items") or "[]"
    try:
        line_items = json.loads(line_items_raw)
        if not isinstance(line_items, list):
            line_items = []
    except json.JSONDecodeError:
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
    def __init__(self, access_token: str, location_id: str, timeout: int = 30) -> None:
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
        if isinstance(value, dict):
            return {k: SquareClient._strip_none(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [SquareClient._strip_none(item) for item in value]
        return value

    def test_connection(self) -> Dict[str, Any]:
        return self._request("GET", "/v2/locations")

    def upsert_customer(self, invoice: LegacyInvoice) -> str:
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
            lines.append(
                {
                    "name": "Imported legacy invoice total",
                    "quantity": "1",
                    "base_price_money": {
                        "amount": money_to_cents(invoice.total_due),
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
        today = dt.date.today()
        invoice_date = parse_iso_date(invoice.invoice_date, fallback=today)
        due_date_obj = parse_iso_date(invoice.due_date, fallback=today)
        scheduled_date = min(invoice_date, due_date_obj)
        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "invoice": {
                "location_id": self.location_id,
                "order_id": order_id,
                "primary_recipient": {"customer_id": customer_id},
                "scheduled_at": f"{scheduled_date.isoformat()}T00:00:00Z",
                "payment_requests": [
                    {
                        "request_type": "BALANCE",
                        "due_date": due_date_obj.isoformat(),
                        "tipping_enabled": False,
                        "automatic_payment_source": "NONE",
                    }
                ],
                "delivery_method": "SHARE_MANUALLY",
                "title": f"Legacy Invoice {invoice.invoice_number}",
                "description": invoice.service_notes or "Imported from historical invoice records.",
                "invoice_number": f"LEGACY-{invoice.invoice_number}",
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
        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "version": version,
        }
        response = self._request("POST", f"/v2/invoices/{invoice_id}/publish", payload)
        return response["invoice"]

    def record_external_payment(self, invoice_id: str, amount: Decimal) -> Dict[str, Any]:
        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "payment": {
                "payment_type": "EXTERNAL",
                "external_details": {
                    "type": "CASH",
                    "source": "Legacy migration",
                },
                "amount_money": {
                    "amount": money_to_cents(amount),
                    "currency": DEFAULT_CURRENCY,
                },
            },
        }
        return self._request("POST", f"/v2/invoices/{invoice_id}/payments", payload)


def load_legacy_invoices(path: str) -> List[LegacyInvoice]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    records = raw.get("records") or []
    return [parse_record(record) for record in records]


def get_client_from_env() -> SquareClient:
    token = os.getenv("SQUARE_ACCESS_TOKEN")
    location_id = os.getenv("SQUARE_LOCATION_ID")
    if not token or not location_id:
        raise SystemExit("Missing SQUARE_ACCESS_TOKEN or SQUARE_LOCATION_ID environment variables.")
    return SquareClient(access_token=token, location_id=location_id)


def run_test_connection() -> int:
    client = get_client_from_env()
    result = client.test_connection()
    locations = result.get("locations", [])
    print(f"Connection successful. Visible locations: {len(locations)}")
    for loc in locations:
        print(f"- {loc.get('id')} :: {loc.get('name')}")
    return 0


def run_import(path: str, dry_run: bool) -> int:
    client = get_client_from_env()
    invoices = load_legacy_invoices(path)
    print(f"Loaded {len(invoices)} invoices from {path}")

    for invoice in invoices:
        print(f"\nProcessing invoice #{invoice.invoice_number} for {invoice.customer_name}")
        if dry_run:
            print("[DRY-RUN] would create customer, order, invoice, publish, and mark paid.")
            continue

        customer_id = client.upsert_customer(invoice)
        order_id = client.create_order(invoice, customer_id)
        draft = client.create_invoice(invoice, order_id, customer_id)
        published = client.publish_invoice(draft["id"], draft["version"])
        client.record_external_payment(published["id"], invoice.total_due)
        print(f"Invoice {published['id']} published and marked paid.")

    return 0


def build_parser() -> argparse.ArgumentParser:
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
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SquareAPIError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
