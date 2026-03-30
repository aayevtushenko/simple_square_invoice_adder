#!/usr/bin/env python3
"""Review invoice JSON for likely duplicate customers (light entity resolution)."""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class CustomerRecord:
    record_id: str
    source_invoice: str
    name: str
    email: str
    phone: str
    address: str
    cluster_id: Optional[str] = None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", normalize_text(value))


def normalize_address(value: str) -> str:
    value = normalize_text(value)
    replacements = {
        " street": " st",
        " avenue": " ave",
        " road": " rd",
        " boulevard": " blvd",
        " apartment": " apt",
        ".": "",
        ",": "",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    return re.sub(r"\s+", " ", value).strip()


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def flatten_record(record: Dict[str, Any]) -> Dict[str, str]:
    return {item.get("key"): str(item.get("value") or "") for item in record.get("results", [])}


def parse_input_records(path: Path) -> List[CustomerRecord]:
    with path.open() as handle:
        payload = json.load(handle)

    if isinstance(payload, dict) and "records" in payload:
        invoices = payload.get("records", [])
        output: List[CustomerRecord] = []
        for index, invoice in enumerate(invoices):
            fields = flatten_record(invoice)
            output.append(
                CustomerRecord(
                    record_id=f"source-{index}",
                    source_invoice=fields.get("Invoice_Number", ""),
                    name=fields.get("Customer_Name", ""),
                    email=fields.get("Customer_Email", ""),
                    phone=fields.get("Customer_Phone_Number", ""),
                    address=fields.get("Customer_Address", ""),
                    cluster_id=None,
                )
            )
        return output

    if isinstance(payload, list):
        return [CustomerRecord(**item) for item in payload]

    raise ValueError("Unsupported input format. Expected {'records': [...]} or a list of customer records.")


def pairwise_similarity(left: CustomerRecord, right: CustomerRecord) -> Dict[str, float]:
    name_score = SequenceMatcher(None, normalize_name(left.name), normalize_name(right.name)).ratio()
    address_score = SequenceMatcher(None, normalize_address(left.address), normalize_address(right.address)).ratio()
    phone_l = normalize_phone(left.phone)
    phone_r = normalize_phone(right.phone)
    phone_score = 1.0 if phone_l and phone_l == phone_r else 0.0
    email_score = 1.0 if normalize_text(left.email) and normalize_text(left.email) == normalize_text(right.email) else 0.0
    return {
        "name": name_score,
        "address": address_score,
        "phone": phone_score,
        "email": email_score,
    }


def decision_exact_key(left: CustomerRecord, right: CustomerRecord) -> bool:
    return (
        normalize_name(left.name) == normalize_name(right.name)
        and normalize_phone(left.phone)
        and normalize_phone(left.phone) == normalize_phone(right.phone)
        and normalize_address(left.address) == normalize_address(right.address)
    )


def decision_contact_match(left: CustomerRecord, right: CustomerRecord) -> bool:
    scores = pairwise_similarity(left, right)
    return scores["email"] == 1.0 or scores["phone"] == 1.0


def decision_light_weighted(left: CustomerRecord, right: CustomerRecord) -> bool:
    scores = pairwise_similarity(left, right)
    if scores["email"] == 1.0 or scores["phone"] == 1.0:
        if scores["name"] >= 0.62:
            return True
    weighted = (0.45 * scores["name"]) + (0.35 * scores["address"]) + (0.10 * scores["phone"]) + (0.10 * scores["email"])
    return weighted >= 0.78 and scores["name"] >= 0.68


def cluster_records(records: Sequence[CustomerRecord], decision_fn: Callable[[CustomerRecord, CustomerRecord], bool]) -> List[List[CustomerRecord]]:
    parent = list(range(len(records)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, j in combinations(range(len(records)), 2):
        if decision_fn(records[i], records[j]):
            union(i, j)

    grouped: Dict[int, List[CustomerRecord]] = {}
    for idx, record in enumerate(records):
        grouped.setdefault(find(idx), []).append(record)
    return list(grouped.values())


def build_pair_set(groups: List[List[CustomerRecord]]) -> set[Tuple[str, str]]:
    pairs: set[Tuple[str, str]] = set()
    for group in groups:
        ids = sorted(item.record_id for item in group)
        for a, b in combinations(ids, 2):
            pairs.add((a, b))
    return pairs


def evaluate(records: Sequence[CustomerRecord], approach: str) -> Dict[str, float]:
    decision_lookup = {
        "exact": decision_exact_key,
        "contact": decision_contact_match,
        "light": decision_light_weighted,
    }
    decision_fn = decision_lookup[approach]

    predicted_groups = cluster_records(records, decision_fn)
    predicted_pairs = build_pair_set(predicted_groups)

    truth_groups: Dict[str, List[CustomerRecord]] = {}
    for record in records:
        if not record.cluster_id:
            continue
        truth_groups.setdefault(record.cluster_id, []).append(record)
    truth_pairs = build_pair_set(list(truth_groups.values()))

    tp = len(predicted_pairs & truth_pairs)
    fp = len(predicted_pairs - truth_pairs)
    fn = len(truth_pairs - predicted_pairs)

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "pairs_predicted": len(predicted_pairs),
        "pairs_truth": len(truth_pairs),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    }


def typo_mutation(value: str, rng: random.Random) -> str:
    if not value:
        return value
    ops = ["drop", "swap", "repeat", "space"]
    op = rng.choice(ops)
    chars = list(value)
    if len(chars) < 2:
        return value
    idx = rng.randrange(0, len(chars) - 1)
    if op == "drop":
        del chars[idx]
    elif op == "swap":
        chars[idx], chars[idx + 1] = chars[idx + 1], chars[idx]
    elif op == "repeat":
        chars.insert(idx, chars[idx])
    elif op == "space":
        chars[idx] = " " if chars[idx] != " " else chars[idx]
    return "".join(chars)


def generate_synthetic_dataset(records: Sequence[CustomerRecord], variants_per_customer: int, seed: int) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    synthetic: List[Dict[str, Any]] = []

    for base_idx, record in enumerate(records):
        cluster_id = f"cluster-{base_idx}"
        canonical = {
            "record_id": f"{cluster_id}-canonical",
            "source_invoice": record.source_invoice,
            "name": record.name,
            "email": record.email,
            "phone": record.phone,
            "address": record.address,
            "cluster_id": cluster_id,
        }
        synthetic.append(canonical)

        for variant_idx in range(variants_per_customer):
            synthetic.append(
                {
                    "record_id": f"{cluster_id}-variant-{variant_idx}",
                    "source_invoice": f"{record.source_invoice}-v{variant_idx}",
                    "name": typo_mutation(record.name, rng),
                    "email": record.email if rng.random() > 0.35 else typo_mutation(record.email, rng),
                    "phone": record.phone if rng.random() > 0.45 else typo_mutation(record.phone, rng),
                    "address": record.address if rng.random() > 0.30 else typo_mutation(record.address, rng),
                    "cluster_id": cluster_id,
                }
            )

    lookalikes = [
        {
            "record_id": "lookalike-1",
            "source_invoice": "demo-1",
            "name": "Ken Chang",
            "email": "ken.chang@example.com",
            "phone": "6467100279",
            "address": "251 4th st jersey city nj 07302",
            "cluster_id": "lookalike-1",
        },
        {
            "record_id": "lookalike-2",
            "source_invoice": "demo-2",
            "name": "Rosalee Chung",
            "email": "rosalee+work@example.com",
            "phone": "2016744335",
            "address": "349 fourth street hoboken nj 07302",
            "cluster_id": "lookalike-2",
        },
    ]
    synthetic.extend(lookalikes)
    return synthetic


def print_groups(groups: List[List[CustomerRecord]]) -> None:
    duplicate_groups = [group for group in groups if len(group) > 1]
    print(f"Potential duplicate groups: {len(duplicate_groups)}")
    for idx, group in enumerate(sorted(duplicate_groups, key=len, reverse=True), start=1):
        print(f"\nGroup {idx} ({len(group)} records)")
        for record in group:
            print(
                f"  - {record.record_id}: name={record.name!r} | email={record.email!r} | "
                f"phone={record.phone!r} | address={record.address!r}"
            )


def cmd_generate(args: argparse.Namespace) -> None:
    records = parse_input_records(Path(args.input))
    synthetic = generate_synthetic_dataset(records, args.variants_per_customer, args.seed)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(synthetic, indent=2))
    print(f"Wrote {len(synthetic)} synthetic customer records to {output_path}")


def cmd_review(args: argparse.Namespace) -> None:
    records = parse_input_records(Path(args.input))
    decision_lookup = {
        "exact": decision_exact_key,
        "contact": decision_contact_match,
        "light": decision_light_weighted,
    }
    groups = cluster_records(records, decision_lookup[args.approach])
    print_groups(groups)


def cmd_evaluate(args: argparse.Namespace) -> None:
    records = parse_input_records(Path(args.input))
    approaches = ["exact", "contact", "light"] if args.approach == "all" else [args.approach]
    for approach in approaches:
        metrics = evaluate(records, approach)
        print(f"\nApproach: {approach}")
        for key, value in metrics.items():
            print(f"  {key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Light entity resolution for invoice customer records")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-test-set", help="Generate synthetic typo-heavy customer dataset")
    generate.add_argument("--input", required=True, help="Path to source results.json style file")
    generate.add_argument("--output", required=True, help="Output path for generated synthetic dataset")
    generate.add_argument("--variants-per-customer", type=int, default=5)
    generate.add_argument("--seed", type=int, default=11)
    generate.set_defaults(func=cmd_generate)

    review = subparsers.add_parser("review", help="Review likely duplicate customers")
    review.add_argument("--input", required=True, help="Path to source file")
    review.add_argument("--approach", choices=["exact", "contact", "light"], default="light")
    review.set_defaults(func=cmd_review)

    evaluate_parser = subparsers.add_parser("evaluate", help="Evaluate an approach on labeled synthetic data")
    evaluate_parser.add_argument("--input", required=True, help="Path to labeled synthetic dataset")
    evaluate_parser.add_argument("--approach", choices=["exact", "contact", "light", "all"], default="all")
    evaluate_parser.set_defaults(func=cmd_evaluate)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
