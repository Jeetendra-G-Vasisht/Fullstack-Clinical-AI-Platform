"""
Generates the labeled validation set for the review pipeline: a synthetic
document-to-patient-record matching task (the pipeline's own domain).

Every "person" here is entirely fabricated (no real patient data). Each
case is one document extraction + a small candidate pool + the ground-
truth patient_id it should match (or null for a genuine no-match case).

Deterministic (seeded RNG) so re-running this regenerates byte-identical
dataset.jsonl -- the committed dataset.jsonl in this directory is exactly
this script's output, so run_eval.py doesn't depend on regenerating it.

Categories, roughly matching what a real intake/matching queue sees:
  clean          -- document ~= one candidate, pool is otherwise distinct.
  noisy_correct  -- one field is corrupted (typo/transposition/missing),
                    but two of three still clearly agree on the same person.
  ambiguous      -- pool contains a genuine near-duplicate (e.g. two
                    similarly-named people) and the document is missing
                    enough evidence that telling them apart isn't safe.
  no_match       -- the document doesn't belong to anyone in its pool.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

FIRST_NAMES = [
    "James", "Maria", "Robert", "Linda", "Michael", "Patricia", "David", "Barbara",
    "John", "Elizabeth", "Carlos", "Nancy", "Wei", "Susan", "Ahmed", "Jessica",
    "Thomas", "Karen", "Daniel", "Lisa", "Sofia", "Mark", "Priya", "Kevin",
]
LAST_NAMES = [
    "Smith", "Garcia", "Johnson", "Martinez", "Brown", "Davis", "Lee", "Wilson",
    "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "White", "Harris", "Clark",
    "Lewis", "Young", "Walker", "Hall", "Allen", "King", "Wright", "Patel",
]


def random_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def random_dob(rng: random.Random) -> str:
    year = rng.randint(1945, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def random_mrn(rng: random.Random) -> str:
    return f"{rng.randint(100000, 999999)}"


def typo(name: str, rng: random.Random) -> str:
    if len(name) < 4:
        return name
    i = rng.randint(1, len(name) - 2)
    return name[:i] + rng.choice("aeiourstln") + name[i + 1:]


def swap_day_month(dob: str) -> str:
    year, month, day = dob.split("-")
    if int(day) > 12:
        return dob
    return f"{year}-{day}-{month}"


def make_person(rng: random.Random, patient_id: str) -> dict:
    return {
        "patient_id": patient_id,
        "name": random_name(rng),
        "dob": random_dob(rng),
        "mrn": random_mrn(rng),
    }


def distractor_pool(rng: random.Random, n: int, start_id: int) -> list[dict]:
    return [make_person(rng, f"p{start_id + i}") for i in range(n)]


def build_clean_case(rng: random.Random, case_id: int, pid_start: int) -> dict:
    candidates = distractor_pool(rng, rng.randint(1, 2), pid_start)
    target = make_person(rng, f"p{pid_start + len(candidates)}")
    candidates.append(target)
    rng.shuffle(candidates)
    document = {
        "doc_id": f"doc{case_id}",
        "name": target["name"],
        "dob": target["dob"],
        "mrn": target["mrn"],
    }
    return {"case_id": f"clean_{case_id}", "category": "clean",
            "document": document, "candidates": candidates, "expected_patient_id": target["patient_id"]}


def build_noisy_correct_case(rng: random.Random, case_id: int, pid_start: int) -> dict:
    candidates = distractor_pool(rng, rng.randint(1, 2), pid_start)
    target = make_person(rng, f"p{pid_start + len(candidates)}")
    candidates.append(target)
    rng.shuffle(candidates)

    corruption = rng.choice(["name_typo", "dob_swap", "missing_mrn"])
    document = {"doc_id": f"doc{case_id}", "name": target["name"], "dob": target["dob"], "mrn": target["mrn"]}
    if corruption == "name_typo":
        document["name"] = typo(target["name"], rng)
    elif corruption == "dob_swap":
        document["dob"] = swap_day_month(target["dob"])
    else:
        document["mrn"] = ""

    return {"case_id": f"noisy_{case_id}", "category": "noisy_correct",
            "document": document, "candidates": candidates, "expected_patient_id": target["patient_id"]}


def build_ambiguous_case(rng: random.Random, case_id: int, pid_start: int) -> dict:
    # Two near-duplicate people: same name, DOBs one day apart (a real
    # confusable pair -- e.g. twins, or a shared common name).
    shared_name = random_name(rng)
    dob_a = random_dob(rng)
    year, month, day = dob_a.split("-")
    dob_b = f"{year}-{month}-{min(28, int(day) + 1):02d}"

    person_a = {"patient_id": f"p{pid_start}", "name": shared_name, "dob": dob_a, "mrn": random_mrn(rng)}
    person_b = {"patient_id": f"p{pid_start + 1}", "name": shared_name, "dob": dob_b, "mrn": random_mrn(rng)}
    extra = distractor_pool(rng, rng.randint(0, 1), pid_start + 2)
    candidates = [person_a, person_b] + extra
    rng.shuffle(candidates)

    target = rng.choice([person_a, person_b])
    # Document has the right name but no identifier and an ambiguous DOB
    # (matches neither exactly) -- there just isn't enough evidence here
    # to safely tell person_a and person_b apart.
    document = {"doc_id": f"doc{case_id}", "name": shared_name, "dob": "", "mrn": ""}

    return {"case_id": f"ambiguous_{case_id}", "category": "ambiguous",
            "document": document, "candidates": candidates, "expected_patient_id": target["patient_id"]}


def build_no_match_case(rng: random.Random, case_id: int, pid_start: int) -> dict:
    candidates = distractor_pool(rng, rng.randint(2, 3), pid_start)
    stranger = make_person(rng, "unrelated")
    document = {"doc_id": f"doc{case_id}", "name": stranger["name"], "dob": stranger["dob"], "mrn": stranger["mrn"]}
    return {"case_id": f"nomatch_{case_id}", "category": "no_match",
            "document": document, "candidates": candidates, "expected_patient_id": None}


def generate(seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    cases = []
    pid = 0
    case_id = 0
    builders = (
        [build_clean_case] * 20
        + [build_noisy_correct_case] * 15
        + [build_ambiguous_case] * 15
        + [build_no_match_case] * 10
    )
    rng.shuffle(builders)
    for builder in builders:
        case = builder(rng, case_id, pid)
        cases.append(case)
        pid += len(case["candidates"]) + 1
        case_id += 1
    return cases


if __name__ == "__main__":
    dataset = generate()
    out_path = Path(__file__).parent / "dataset.jsonl"
    with out_path.open("w") as f:
        for case in dataset:
            f.write(json.dumps(case) + "\n")
    print(f"Wrote {len(dataset)} cases to {out_path}")
