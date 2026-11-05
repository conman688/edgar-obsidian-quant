"""Tone of a text: the share of its words on the Loughran-McDonald word lists.

The Master Dictionary marks each word with the year it joined a list, or minus the year it
left one; only words currently on a list count. The dictionary is licensed by its authors
and is not part of this repository: put the CSV at `config.LM_DICT` (see README). Without
it, tone is simply not shown.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

LISTS = {"negative": "Negative", "litigious": "Litigious", "uncertainty": "Uncertainty"}
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def load_lists(path: Path) -> dict[str, set[str]] | None:
    if not Path(path).is_file():
        return None
    out: dict[str, set[str]] = {k: set() for k in LISTS}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            for key, col in LISTS.items():
                if int(float(row[col] or 0)) > 0:
                    out[key].add(row["Word"].upper())
    return out


def tone(text: str, lists: dict[str, set[str]]) -> dict[str, float | None]:
    words = [w.upper() for w in _WORD.findall(text)]
    if not words:
        return {k: None for k in lists}
    return {k: sum(w in s for w in words) / len(words) for k, s in lists.items()}
