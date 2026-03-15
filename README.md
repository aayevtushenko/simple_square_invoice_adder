# Simple Square Invoice Backloader

This repo now contains a simple Python CLI to import historical invoice data (matching `results.json` structure) into Square while working around common Square invoice constraints.

## Why this workflow is needed

Square Invoices does not allow creating a brand-new invoice directly in a "paid" state. A practical migration sequence is:

1. Create customer (or reuse by email).
2. Create order with line items.
3. Create draft invoice tied to the order.
4. Publish invoice with safe settings.
5. Record a manual payment on the invoice order using `POST /v2/payments` (defaults to `source_id=CASH`) so the invoice closes in Square.

## Built-in limitations/workarounds implemented

The script applies the following defaults to avoid outbound customer communication and preserve payment tracking:

- **Due date set to today** for balance request (`payment_requests[0].due_date`), approximating due-on-receipt behavior for legacy imports.
- **Accepted payment mode includes card** (`accepted_payment_methods.card=true`) so payment settings are valid.
- **No reminders configured** (the `reminders` field is omitted because current Invoice API versions reject it on create).
- **Manual sharing only** (`delivery_method=SHARE_MANUALLY`) so Square does not email/text customers automatically.
- **Immediate manual payment record** after publish using `POST /v2/payments` on the invoice order. Default mode mirrors Dashboard “Mark as paid” with `source_id=CASH` (override via `SQUARE_MANUAL_PAYMENT_METHOD=EXTERNAL`).


## Square API requirements and setup

To reliably replicate Dashboard behavior (due today + mark as paid), your token/application must satisfy these requirements:

- **Same application ownership across objects**: Orders and invoice actions must be performed by the same Square application/access token. The previous `POST /v2/payments` call failed because it attempted to pay an order owned by another app (`AUTHENTICATION_ERROR` / `FORBIDDEN`).
- **Invoices API access**: Required for creating, publishing, and recording invoice payments.
- **Customers API access**: Required for customer lookup/create before invoice creation.
- **Orders API access**: Required because invoice creation references an order.
- **Payments API access**: Required to call `POST /v2/payments` when recording manual payments for imported invoices.
- **Location access**: `SQUARE_LOCATION_ID` must be visible to the token (`test-connection` validates this).
- **Endpoint compatibility**: Some accounts/API versions return `404 NOT_FOUND` for `POST /v2/invoices/{invoice_id}/payments`; this script intentionally uses the generally available Payments API path.

Behavioral constraints the script now enforces:

- For manual-share invoices (`delivery_method=SHARE_MANUALLY`), the script omits `scheduled_at` so publish does not leave invoices in a future `SCHEDULED` state.
- Due date is normalized to today-or-later to remain API-valid while matching "Due today" behavior when source dates are historical.

## Prerequisites

Set environment variables:

- `SQUARE_ACCESS_TOKEN`
- `SQUARE_LOCATION_ID`

Install dependency:

```bash
pip install requests
```

## CLI usage

### 1) Connectivity test utility

Use this before imports to confirm auth + API reachability:

```bash
python square_invoice_backloader.py test-connection
```

### 2) Dry-run import

Validates input parsing and shows what would happen without mutating Square:

```bash
python square_invoice_backloader.py import --input results.json --dry-run
```

### 3) Real import

```bash
python square_invoice_backloader.py import --input results.json
```

### 4) Cleanup utility (delete imported invoices)

If you need to re-run imports and reuse legacy invoice numbers, use the cleanup tool:

```bash
python square_invoice_cleanup.py cleanup --input results.json --dry-run
python square_invoice_cleanup.py cleanup --input results.json
```

Notes:
- It matches invoices by `Invoice_Number` from input (and automatically checks `LEGACY-<Invoice_Number>`).
- If your JSON also includes `Square_Invoice_ID`/`Invoice_ID`, those IDs are deleted directly.
- This only deletes invoices in Square; it does not remove customers or orders.


## Input format expectations

The input JSON should contain:

- top-level `records` array
- each `record.results` as key/value entries where `key` includes:
  - `Invoice_Number`, `Invoice_Date`, `Due_Date`
  - `Customer_Name`, `Customer_Email`, etc.
  - `Line_Items` as JSON-stringified array
  - `Total_Amount_Due`

This exactly matches the example file already in the repo.

## Notes

- Keep `--dry-run` as your default while iterating.
- Use a Square sandbox or controlled account when first running real imports.
- Depending on Square account rules, you may need to adjust accepted payment methods or external payment detail type.
