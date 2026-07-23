"""Synthetic Pakistani bank-transaction generator.

All PII is synthetic (Faker + hand-rolled locale formats). CNICs are generated
with valid structure but random digits — they do not correspond to real people.
The free-text `memo` field intermittently embeds PII in natural language so the
stream also exercises Tier 2+ later, not just structured-field regex.
"""
from __future__ import annotations

import random

from faker import Faker

fake = Faker()

# Roman-Urdu / Pakistani first and last names — the locale differentiator.
# Deliberately includes names that are also common words/ambiguous tokens
# (e.g. "Iman", "Noor") to stress Tier 2 later.
FIRST_NAMES = [
    "Affan", "Ahmed", "Muhammad", "Ali", "Hassan", "Hussain", "Usman", "Bilal",
    "Hamza", "Zain", "Fahad", "Imran", "Kamran", "Salman", "Noman", "Rizwan",
    "Ayesha", "Fatima", "Zainab", "Maryam", "Khadija", "Amna", "Hira", "Sana",
    "Iman", "Noor", "Mahnoor", "Areeba", "Laiba", "Eman",
]
LAST_NAMES = [
    "Khan", "Ahmed", "Malik", "Butt", "Chaudhry", "Sheikh", "Qureshi", "Syed",
    "Baig", "Mirza", "Awan", "Bhatti", "Javed", "Iqbal", "Raza", "Abbasi",
    "Basra", "Gill", "Cheema", "Warraich",
]

BANK_CODES = ["HABB", "UNIL", "MEZN", "ALFH", "NBPA", "BAHL", "SCBL", "FAYS"]

MEMO_TEMPLATES = [
    "Transfer to {name}",
    "Rent payment from {name}, contact {phone}",
    "Zakat contribution",
    "Utility bill payment",
    "Sent by {name} (CNIC {cnic}) for invoice #{inv}",
    "Salary for {name}",
    "Refund processed, notify at {email}",
    "Eidi for {name}",
    "Loan installment",
    "Payment against order #{inv}, contact {phone}",
]


def make_cnic() -> str:
    """13-digit CNIC, canonical XXXXX-XXXXXXX-X. First digit 1-7 (valid
    province code) so generated CNICs pass Tier 1 validation."""
    first = random.randint(1, 7)
    return f"{first}{random.randint(1000, 9999)}-{random.randint(1000000, 9999999)}-{random.randint(0, 9)}"


def make_phone() -> str:
    """Pakistani mobile, mixed formats on purpose (0300..., +923...)."""
    prefix = random.choice(["+92 3", "03"])
    return f"{prefix}{random.randint(0, 4)}{random.randint(0, 9)}{'' if prefix.startswith('0') else ' '}{random.randint(1000000, 9999999)}"


def make_iban() -> str:
    """PK IBAN: PK + 2 check digits + 4-char bank code + 16-digit account."""
    return f"PK{random.randint(10, 99)}{random.choice(BANK_CODES)}{random.randint(10**15, 10**16 - 1)}"


def make_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def make_transaction() -> dict:
    name = make_name()
    memo = random.choice(MEMO_TEMPLATES).format(
        name=make_name(),
        phone=make_phone(),
        cnic=make_cnic(),
        email=fake.email(),
        inv=random.randint(1000, 99999),
    )
    return {
        "account_holder": name,
        "cnic": make_cnic(),
        "iban_from": make_iban(),
        "iban_to": make_iban(),
        "phone": make_phone(),
        "email": fake.email(),
        "amount_pkr": round(random.uniform(100, 500_000), 2),
        "channel": random.choice(["mobile_app", "branch", "atm", "raast"]),
        "memo": memo,
    }
