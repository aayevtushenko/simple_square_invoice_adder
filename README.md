# Simple Square Invoice Backloader

This repo now contains a simple Python CLI to import historical invoice data (matching `results.json` structure) into Square while working around common Square invoice constraints.

## Why this workflow is needed

Square Invoices does not allow creating a brand-new invoice directly in a "paid" state. A practical migration sequence is:

1. Create customer (or reuse by email).
2. Create order with line items.
3. Create draft invoice tied to the order.
4. Publish invoice with safe settings.
5. Record an external payment on the related order (Payments API) so the invoice is effectively closed.

## Built-in limitations/workarounds implemented

The script applies the following defaults to avoid outbound customer communication and preserve payment tracking:

- **Due date set to today** for balance request (`payment_requests[0].due_date`), approximating due-on-receipt behavior for legacy imports.
- **Accepted payment mode includes card** (`accepted_payment_methods.card=true`) so payment settings are valid.
- **No reminders configured** (the `reminders` field is omitted because current Invoice API versions reject it on create).
- **Manual sharing only** (`delivery_method=SHARE_MANUALLY`) so Square does not email/text customers automatically.
- **Immediate external payment record** after publish using `POST /v2/payments` (`source_id=EXTERNAL`, tied to the invoice order) so the invoice closes in Square records.

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
