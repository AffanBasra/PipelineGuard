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


# Share of IBANs emitted with deliberately wrong check digits. These are
# structurally valid but fail mod-97, so Tier 1 flags them at reduced
# confidence and the processor routes them to quarantine — that's how the
# quarantine path gets exercised on a synthetic stream.
CORRUPT_IBAN_RATE = 0.02


def _iban_check_digits(bban: str, country: str = "PK") -> str:
    """ISO 7064 mod-97-10: rearrange as BBAN + country + '00', map letters to
    digits (A=10 ... Z=35), then check = 98 - (n mod 97)."""
    rearranged = f"{bban}{country}00"
    numeric = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    return f"{98 - int(numeric) % 97:02d}"


def make_iban() -> str:
    """PK IBAN: PK + 2 check digits + 4-char bank code + 16-digit account.

    Check digits are computed, not random, so a correct mod-97 validator
    accepts them — except for CORRUPT_IBAN_RATE of them, on purpose.
    """
    bban = f"{random.choice(BANK_CODES)}{random.randint(10**15, 10**16 - 1)}"
    check = _iban_check_digits(bban)
    if random.random() < CORRUPT_IBAN_RATE:
        # Shift the check digits so the value stays 2-digit but fails mod-97.
        check = f"{(int(check) + 1) % 100:02d}"
    return f"PK{check}{bban}"


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
