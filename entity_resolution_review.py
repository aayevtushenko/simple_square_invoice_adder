#!/usr/bin/env python3
"""Review invoice JSON and flag likely duplicate customers via light entity resolution."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class InvoiceCustomer:
    invoice_number: str
    customer_name: str
    customer_email: str
    customer_phone: str
    customer_address: str
    raw: Dict[str, Any]
    entity_id: Optional[str] = None


def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _normalize_phone(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _normalize_address(value: Optional[str]) -> str:
    text = _normalize_text(value)
    replacements = {
        " street": " st",
        " avenue": " ave",
        " boulevard": " blvd",
        " road": " rd",
        " apartment": " apt",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return re.sub(r"[^a-z0-9 ]", "", text)


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _flatten_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {item.get("key"): item.get("value") for item in record.get("results", [])}


def load_invoice_customers(path: Path) -> List[InvoiceCustomer]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ground_truth = {
        str(item["invoice_number"]): item["entity_id"]
        for item in payload.get("entity_resolution_ground_truth", [])
    }

    customers: List[InvoiceCustomer] = []
    for record in payload.get("records", []):
        flat = _flatten_record(record)
        invoice_number = str(flat.get("Invoice_Number") or "UNKNOWN")
        customers.append(
            InvoiceCustomer(
                invoice_number=invoice_number,
                customer_name=str(flat.get("Customer_Name") or ""),
                customer_email=str(flat.get("Customer_Email") or ""),
                customer_phone=str(flat.get("Customer_Phone_Number") or ""),
                customer_address=str(flat.get("Customer_Address") or ""),
                raw=record,
                entity_id=ground_truth.get(invoice_number),
            )
        )
    return customers


def score_pair(left: InvoiceCustomer, right: InvoiceCustomer, mode: str = "hybrid") -> Tuple[float, Dict[str, float]]:
    norm_email_left = _normalize_text(left.customer_email)
    norm_email_right = _normalize_text(right.customer_email)
    norm_phone_left = _normalize_phone(left.customer_phone)
    norm_phone_right = _normalize_phone(right.customer_phone)

    email_exact = 1.0 if norm_email_left and norm_email_left == norm_email_right else 0.0
    phone_exact = 1.0 if norm_phone_left and norm_phone_left == norm_phone_right else 0.0
    email_sim = _sim(norm_email_left, norm_email_right)
    phone_sim = _sim(norm_phone_left, norm_phone_right)
    name_sim = _sim(_normalize_text(left.customer_name), _normalize_text(right.customer_name))
    address_sim = _sim(_normalize_address(left.customer_address), _normalize_address(right.customer_address))

    if mode == "strict":
        score = max(email_exact, phone_exact)
    elif mode == "fuzzy":
        score = 0.55 * name_sim + 0.45 * address_sim
    elif mode == "contact":
        score = 0.5 * email_exact + 0.3 * phone_exact + 0.2 * max(email_sim, phone_sim)
    else:
        contact_anchor = max(email_exact, phone_exact)
        if contact_anchor:
            score = 0.82 + 0.12 * name_sim + 0.06 * address_sim
        else:
            score = 0.5 * max(email_sim, phone_sim) + 0.3 * name_sim + 0.2 * address_sim

    return score, {
        "email_exact": email_exact,
        "phone_exact": phone_exact,
        "email_similarity": round(email_sim, 4),
        "phone_similarity": round(phone_sim, 4),
        "name_similarity": round(name_sim, 4),
        "address_similarity": round(address_sim, 4),
    }


def review(customers: List[InvoiceCustomer], mode: str, threshold: float) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for left, right in combinations(customers, 2):
        score, diagnostics = score_pair(left, right, mode)
        if score >= threshold:
            candidates.append(
                {
                    "left_invoice": left.invoice_number,
                    "right_invoice": right.invoice_number,
                    "score": round(score, 4),
                    "diagnostics": diagnostics,
                    "left_name": left.customer_name,
                    "right_name": right.customer_name,
                }
            )

    return {
        "mode": mode,
        "threshold": threshold,
        "total_records": len(customers),
        "candidate_matches": sorted(candidates, key=lambda x: x["score"], reverse=True),
    }


def _pairwise_truth(customers: List[InvoiceCustomer]) -> set[Tuple[str, str]]:
    truth = set()
    labeled = [c for c in customers if c.entity_id]
    for left, right in combinations(labeled, 2):
        if left.entity_id == right.entity_id:
            truth.add(tuple(sorted((left.invoice_number, right.invoice_number))))
    return truth


def evaluate(customers: List[InvoiceCustomer], modes: List[str], threshold: float) -> Dict[str, Any]:
    truth = _pairwise_truth(customers)
    results: List[Dict[str, Any]] = []

    for mode in modes:
        predicted = {
            tuple(sorted((match["left_invoice"], match["right_invoice"])))
            for match in review(customers, mode, threshold)["candidate_matches"]
        }

        true_pos = len(predicted & truth)
        false_pos = len(predicted - truth)
        false_neg = len(truth - predicted)
        precision = true_pos / (true_pos + false_pos) if (true_pos + false_pos) else 0.0
        recall = true_pos / (true_pos + false_neg) if (true_pos + false_neg) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        results.append(
            {
                "mode": mode,
                "predicted_pairs": len(predicted),
                "truth_pairs": len(truth),
                "true_positive": true_pos,
                "false_positive": false_pos,
                "false_negative": false_neg,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
            }
        )

    return {"threshold": threshold, "results": results}


def _record_from_template(template: Dict[str, Any], overrides: Dict[str, str]) -> Dict[str, Any]:
    cloned = {"results": [dict(item) for item in template.get("results", [])]}
    key_to_item = {item["key"]: item for item in cloned["results"]}
    for key, value in overrides.items():
        if key in key_to_item:
            key_to_item[key]["value"] = value
    return cloned


def generate_synthetic_set(template_path: Path, output_path: Path, seed: int = 7) -> Dict[str, Any]:
    random.seed(seed)
    source = json.loads(template_path.read_text(encoding="utf-8"))
    templates = source.get("records", [])
    if not templates:
        raise ValueError("Template input has no records")

    typo_sets = [
        {
            "name": ["Ken Chung", "Kenneth Chung", "Ken Chugn", "K. Chung"],
            "address": ["249 4th St. Jersey City, NJ 07302", "249 4th Street Jersey City NJ 07302", "249 4th St Jersey Cty, NJ 07302", "249 4th St. Jersey City NJ 7302"],
            "phone": ["6467100278", "(646)710-0278", "646-710-0278", "6467100287"],
            "email": ["kennethwkabcchung@gmail.com", "kennethwkabcchung+legacy@gmail.com", "kenethwkabcchung@gmail.com", "kennethwkabcchung@gmail.com"],
        },
        {
            "name": ["Shmiegel Precious", "Smiegel Precious", "Shmiegel P.", "Shmiegal Precious"],
            "address": ["999999 4th St.Bayonne, NJ 07302", "999999 4th St Bayonne NJ 07302", "999999 4th Street Bayonne, NJ 07302", "999999 4th St. Bayone, NJ 07302"],
            "phone": ["2014544449", "201-454-4449", "201454449", "2014544449"],
            "email": ["beeboop@gmail.com", "beeboop@gmail.com", "beeboop+old@gmail.com", "beeboop@gmail.com"],
        },
        {
            "name": ["Rosalie Chung", "Rosalee Chung", "R. Chung", "Rosalie Cung"],
            "address": ["349 4th St. HOBOKEN, NJ 07302", "349 4th Street Hoboken NJ 07302", "349 4th St Hoboken, NJ 07302", "349 4th St. Hoboken NJ 7302"],
            "phone": ["2016744334", "(201)674-4334", "201-674-4334", "2016744335"],
            "email": ["aayevtushenko@gmail.com", "aayevtushenko+legacy@gmail.com", "aayevtushenko@gmail.co", "aayevtushenko@gmail.com"],
        },
    ]

    records = []
    truth = []
    invoice_counter = 4000

    for idx, typo in enumerate(typo_sets):
        template = templates[idx % len(templates)]
        entity_id = f"entity-{idx + 1}"
        for variant in range(4):
            invoice_counter += 1
            invoice_number = str(invoice_counter)
            rec = _record_from_template(
                template,
                {
                    "Invoice_Number": invoice_number,
                    "Customer_Name": typo["name"][variant],
                    "Customer_Email": typo["email"][variant],
                    "Customer_Phone_Number": typo["phone"][variant],
                    "Customer_Address": typo["address"][variant],
                },
            )
            records.append(rec)
            truth.append({"invoice_number": invoice_number, "entity_id": entity_id})

    payload = {"records": records, "entity_resolution_ground_truth": truth}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"output": str(output_path), "records": len(records), "entities": len(typo_sets)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Light entity resolution review for invoice JSON inputs.")
    sub = parser.add_subparsers(dest="command", required=True)

    review_cmd = sub.add_parser("review", help="Find likely duplicate customers.")
    review_cmd.add_argument("--input", required=True)
    review_cmd.add_argument("--mode", choices=["strict", "contact", "fuzzy", "hybrid"], default="hybrid")
    review_cmd.add_argument("--threshold", type=float, default=0.8)
    review_cmd.add_argument("--output")

    eval_cmd = sub.add_parser("evaluate", help="Evaluate matching modes using embedded ground truth.")
    eval_cmd.add_argument("--input", required=True)
    eval_cmd.add_argument("--threshold", type=float, default=0.8)
    eval_cmd.add_argument("--modes", nargs="+", default=["strict", "contact", "fuzzy", "hybrid"])

    synth_cmd = sub.add_parser("generate-synthetic", help="Create synthetic typo-heavy test data from existing input template.")
    synth_cmd.add_argument("--input", required=True)
    synth_cmd.add_argument("--output", required=True)
    synth_cmd.add_argument("--seed", type=int, default=7)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "generate-synthetic":
        summary = generate_synthetic_set(Path(args.input), Path(args.output), seed=args.seed)
        print(json.dumps(summary, indent=2))
        return 0

    customers = load_invoice_customers(Path(args.input))

    if args.command == "review":
        result = review(customers, mode=args.mode, threshold=args.threshold)
        text = json.dumps(result, indent=2)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"Wrote review report to {args.output}")
        else:
            print(text)
        return 0

    if args.command == "evaluate":
        result = evaluate(customers, modes=args.modes, threshold=args.threshold)
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
