#!/usr/bin/env python3
"""Delete previously imported legacy invoices from Square.

This utility reads the same JSON format used by `square_invoice_backloader.py` and
attempts to find matching Square invoices by invoice number (and optionally explicit
Square invoice IDs if present in the input data).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

import requests
from requests import RequestException

SQUARE_API_BASE = "https://connect.squareup.com"
LEGACY_PREFIX = "LEGACY-"


class SquareAPIError(RuntimeError):
    pass


@dataclass
class CleanupTargets:
    invoice_numbers: Set[str]
    invoice_ids: Set[str]


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

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            response = self.session.request(
                method=method,
                url=f"{SQUARE_API_BASE}{path}",
                params=params,
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

    def test_connection(self) -> Dict[str, Any]:
        return self._request("GET", "/v2/locations")

    def list_all_invoices(self) -> List[Dict[str, Any]]:
        invoices: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"location_id": self.location_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            response = self._request("GET", "/v2/invoices", params=params)
            invoices.extend(response.get("invoices") or [])
            cursor = response.get("cursor")
            if not cursor:
                break
        return invoices

    def retrieve_invoice(self, invoice_id: str) -> Dict[str, Any]:
        response = self._request("GET", f"/v2/invoices/{invoice_id}")
        return response["invoice"]

    def delete_invoice(self, invoice_id: str, version: int) -> Dict[str, Any]:
        params = {"version": version}
        response = self._request("DELETE", f"/v2/invoices/{invoice_id}", params=params)
        return response.get("invoice") or {}


def get_client_from_env() -> SquareClient:
    token = os.getenv("SQUARE_ACCESS_TOKEN")
    location_id = os.getenv("SQUARE_LOCATION_ID")
    if not token or not location_id:
        raise SystemExit("Missing SQUARE_ACCESS_TOKEN or SQUARE_LOCATION_ID environment variables.")
    return SquareClient(access_token=token, location_id=location_id)


def _flatten_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {item["key"]: item.get("value") for item in record.get("results", []) if isinstance(item, dict) and "key" in item}


def _extract_invoice_number(flattened: Dict[str, Any]) -> Optional[str]:
    number = flattened.get("Invoice_Number")
    if number is None:
        return None
    number_str = str(number).strip()
    return number_str if number_str else None


def _extract_invoice_id(flattened: Dict[str, Any]) -> Optional[str]:
    for key in ("Square_Invoice_ID", "Invoice_ID", "invoice_id"):
        raw = flattened.get(key)
        if raw:
            value = str(raw).strip()
            if value:
                return value
    return None


def load_cleanup_targets(path: str) -> CleanupTargets:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    invoice_numbers: Set[str] = set()
    invoice_ids: Set[str] = set()

    for record in raw.get("records") or []:
        flattened = _flatten_record(record)
        invoice_number = _extract_invoice_number(flattened)
        if invoice_number:
            invoice_numbers.add(invoice_number)
            invoice_numbers.add(f"{LEGACY_PREFIX}{invoice_number}")

        invoice_id = _extract_invoice_id(flattened)
        if invoice_id:
            invoice_ids.add(invoice_id)

    return CleanupTargets(invoice_numbers=invoice_numbers, invoice_ids=invoice_ids)


def run_test_connection() -> int:
    client = get_client_from_env()
    result = client.test_connection()
    locations = result.get("locations", [])
    print(f"Connection successful. Visible locations: {len(locations)}")
    for loc in locations:
        print(f"- {loc.get('id')} :: {loc.get('name')}")
    return 0


def run_cleanup(path: str, dry_run: bool) -> int:
    client = get_client_from_env()
    targets = load_cleanup_targets(path)

    print(f"Loaded cleanup targets from {path}")
    print(f"- invoice numbers to match: {len(targets.invoice_numbers)}")
    print(f"- explicit invoice IDs: {len(targets.invoice_ids)}")

    invoices = client.list_all_invoices()
    invoices_by_number = {str(inv.get('invoice_number')): inv for inv in invoices if inv.get('invoice_number')}

    to_delete: Dict[str, Dict[str, Any]] = {}

    for invoice_id in targets.invoice_ids:
        try:
            invoice = client.retrieve_invoice(invoice_id)
        except SquareAPIError as exc:
            print(f"WARN: could not retrieve invoice id {invoice_id}: {exc}")
            continue
        to_delete[invoice["id"]] = invoice

    for number in targets.invoice_numbers:
        invoice = invoices_by_number.get(number)
        if invoice:
            to_delete[invoice["id"]] = invoice

    if not to_delete:
        print("No matching invoices found; nothing to delete.")
        return 0

    print(f"Found {len(to_delete)} invoices to delete.")
    deleted_count = 0

    for invoice in to_delete.values():
        invoice_id = invoice["id"]
        invoice_number = invoice.get("invoice_number")
        version = invoice["version"]
        status = invoice.get("status")
        print(f"- target id={invoice_id} number={invoice_number} status={status} version={version}")

        if dry_run:
            print("  [DRY-RUN] skipping delete")
            continue

        try:
            client.delete_invoice(invoice_id, version)
            deleted_count += 1
            print("  deleted")
        except SquareAPIError as exc:
            print(f"  ERROR deleting invoice: {exc}")

    if dry_run:
        print("Dry run complete. No invoices were deleted.")
    else:
        print(f"Deleted {deleted_count} invoices.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete legacy-imported invoices from Square.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    test_cmd = subparsers.add_parser("test-connection", help="Validate Square auth and location access.")
    test_cmd.set_defaults(func=lambda args: run_test_connection())

    cleanup_cmd = subparsers.add_parser("cleanup", help="Delete invoices matching IDs/numbers from JSON input.")
    cleanup_cmd.add_argument("--input", required=True, help="Path to JSON payload (same structure as results.json)")
    cleanup_cmd.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    cleanup_cmd.set_defaults(func=lambda args: run_cleanup(args.input, args.dry_run))

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
