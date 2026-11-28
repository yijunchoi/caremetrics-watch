"""Named entity matching: the words people use, mapped to the exact values in hospital.duckdb.

People say "the ER", "Blue Cross", "heart failure" and "seniors". The data says
ENCOUNTERCLASS 'emergency', payers.NAME 'Blue Cross Blue Shield',
REASONDESCRIPTION 'Chronic congestive heart failure (disorder)' and AGE_BAND '65-79', '80+'.
expand() finds these names in a question, typos and all, and appends what each one means,
so the Analyst filters on values that exist instead of guessing.
"""

import re
from difflib import get_close_matches

from tools.sql import db

# Where each kind of entity lives in the database.
COLUMNS = {
    "visit type": "encounters.ENCOUNTERCLASS",
    "insurer": "payers.NAME",
    "diagnosis": "encounters.REASONDESCRIPTION",
    "age band": "encounters.AGE_BAND",
}

# Everyday names -> the values in the data. The exact names from the data are added below.
VISIT_TYPES = {
    "emergency room": "emergency", "emergency department": "emergency", "er": "emergency",
    "ed": "emergency", "urgent care": "urgentcare", "admissions": "inpatient",
    "hospitalizations": "inpatient", "check-ups": "wellness", "checkups": "wellness", "check ups": "wellness",
    "well visits": "wellness", "physicals": "wellness",
}
INSURERS = {
    "blue cross": "Blue Cross Blue Shield", "bcbs": "Blue Cross Blue Shield",
    "united": "UnitedHealthcare", "united healthcare": "UnitedHealthcare", "uhc": "UnitedHealthcare",
    "cigna": "Cigna Health", "uninsured": "NO_INSURANCE", "no insurance": "NO_INSURANCE",
    "self-pay": "NO_INSURANCE", "self pay": "NO_INSURANCE", "dual eligible": "Dual Eligible",
}
DIAGNOSES = {
    "heart failure": ["Chronic congestive heart failure (disorder)"],
    "chf": ["Chronic congestive heart failure (disorder)"],
    "bronchitis": ["Acute bronchitis (disorder)"],
    "pregnancy": ["Normal pregnancy"],
    "pregnancies": ["Normal pregnancy"],
    "overdoses": ["Drug overdose"],
    "breast cancer": ["Malignant neoplasm of breast (disorder)"],
    "heart attack": ["Myocardial Infarction"],
    "strep throat": ["Streptococcal sore throat (disorder)"],
    "uti": ["Escherichia coli urinary tract infection"],
    "migraine": ["Chronic intractable migraine without aura"],
    "depression": ["Major depression  single episode", "Major depression disorder"],
    "alzheimer's": ["Alzheimer's disease (disorder)", "Familial Alzheimer's disease of early onset (disorder)"],
}
AGE_BANDS = {
    "children": ["0-17"], "kids": ["0-17"], "pediatric": ["0-17"], "minors": ["0-17"],
    "under 18": ["0-17"], "young adults": ["18-34"],
    "seniors": ["65-79", "80+"], "elderly": ["65-79", "80+"], "over 65": ["65-79", "80+"],
    "65+": ["65-79", "80+"], "over 80": ["80+"], "80 and over": ["80+"],
}


def _plain(name: str) -> str:
    """'Viral sinusitis (disorder)' -> 'viral sinusitis': how people would type it."""
    return " ".join(re.sub(r"\(.*?\)", "", name).split()).lower()


# Every entity: lowercase phrase -> (kind, values). The data's own names come first.
TERMS: dict[str, tuple[str, list[str]]] = {}
for (value,) in db.execute("SELECT DISTINCT ENCOUNTERCLASS FROM encounters").fetchall():
    TERMS[value] = ("visit type", [value])
for (value,) in db.execute("SELECT NAME FROM payers").fetchall():
    TERMS[value.lower()] = ("insurer", [value])
for (value,) in db.execute("SELECT DISTINCT REASONDESCRIPTION FROM encounters WHERE REASONDESCRIPTION IS NOT NULL").fetchall():
    TERMS[_plain(value)] = ("diagnosis", [value])
for (value,) in db.execute("SELECT DISTINCT AGE_BAND FROM encounters").fetchall():
    TERMS[value] = ("age band", [value])
TERMS |= {phrase: ("visit type", [value]) for phrase, value in VISIT_TYPES.items()}
TERMS |= {phrase: ("insurer", [value]) for phrase, value in INSURERS.items()}
TERMS |= {phrase: ("diagnosis", values) for phrase, values in DIAGNOSES.items()}
TERMS |= {phrase: ("age band", values) for phrase, values in AGE_BANDS.items()}
_LONGEST = max(len(phrase.split()) for phrase in TERMS)


def _sql_filter(kind: str, values: list[str]) -> str:
    """('insurer', ['Medicare']) -> "payers.NAME = 'Medicare'"."""
    quoted = ["'" + value.replace("'", "''") + "'" for value in values]
    column = COLUMNS[kind]
    return f"{column} = {quoted[0]}" if len(quoted) == 1 else f"{column} IN ({', '.join(quoted)})"


def find(question: str) -> list[str]:
    """Each entity named in the question, as a line saying what it means in the database."""
    words = re.findall(r"[a-z0-9'+_-]+", question.lower())
    used, seen, found = set(), set(), []
    # Try every phrase, longest first, so "acute bronchitis" wins over "bronchitis".
    for n in range(_LONGEST, 0, -1):
        for i in range(len(words) - n + 1):
            span = set(range(i, i + n))
            if span & used:  # these words are already part of a longer match
                continue
            phrase = " ".join(words[i : i + n])
            if len(phrase) <= 3 or any(c.isdigit() for c in phrase):
                # Short names and numbers must match exactly: "ed" is not "er", "over 60" is not "over 65".
                match = phrase if phrase in TERMS else None
            else:
                # 0.85 similarity forgives a typo ("medicaide") but not a different name.
                close = get_close_matches(phrase, TERMS, n=1, cutoff=0.85)
                # Same number of words, so "is heart failure" doesn't count as "heart failure".
                match = close[0] if close and len(close[0].split()) == n else None
            if match is None:
                continue
            used |= span
            kind, values = TERMS[match]
            if (kind, tuple(values)) not in seen:
                seen.add((kind, tuple(values)))
                found.append(f'"{phrase}" is {kind}: {_sql_filter(kind, values)}')
    return found


def expand(question: str) -> str:
    """Append what each entity named in the question means in the database."""
    found = find(question)
    return question + "\n\nMatched in the data:\n- " + "\n- ".join(found) if found else question


if __name__ == "__main__":
    print(expand("How many ER visits did seniors on Medicare have last month?"))
    print(expand("Is heart failure up among blue cross patients over 80?"))
    print(expand("Compare acute bronchitis and chronic obstructive bronchitis in kids"))
    print(expand("medicaide pregnancys in urgent care"))
