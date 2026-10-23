"""New contracting postings from SAM.gov's public Contract Opportunities extract.

The extract (ContractOpportunitiesFullCSV.csv, ~200 MB, refreshed daily, no
key) holds every active notice. Each notice version is its own row; an
amendment repeats the solicitation number (Sol#) with a later PostedDate.

A posting is new when we have not seen its solicitation number (or NoticeId
when Sol# is blank) before (data/seen.csv, seeded once with everything posted
before the project started). It is filed under the day the run covers; its own
first-posted time, the earliest row for that solicitation, is kept. PostedDate has no
offset and is Eastern: checked against SAM's API, 2026-10-21 17:11:16 in the
extract is 21:11:16Z. Only solicitation-type notices count (TYPES).

The response window is measured from the FIRST posting to the latest deadline
on any version, so an amendment that re-posts an old solicitation does not
look rushed.

Limits: a solicitation whose earlier versions left the active file before we
first saw it can look new. A posting that is archived the day it is posted is
missed.
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

EXTRACT_URL = ("https://s3.amazonaws.com/falextracts/Contract%20Opportunities/"
               "datagov/ContractOpportunitiesFullCSV.csv")
ET = ZoneInfo("America/New_York")
TYPES = ("Solicitation", "Combined Synopsis/Solicitation", "Sources Sought", "Presolicitation")
USER_AGENT = "baffling-rfps (https://github.com/abigailhaddad/baffling-rfps)"

csv.field_size_limit(sys.maxsize)


def download(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with requests.get(EXTRACT_URL, stream=True, timeout=(30, 600), headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def read_rows(path: Path) -> list[dict]:
    # the extract is Windows-1252, not UTF-8 (b"\x93Toby\x94" is “Toby”); latin-1
    # would turn curly quotes and dashes into control characters
    with path.open(encoding="cp1252", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def posted(r: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(r["PostedDate"]).replace(tzinfo=ET)


def deadline(r: dict) -> dt.datetime | None:
    s = (r.get("ResponseDeadLine") or "").strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if len(s) == 10:  # a bare date: the end of that day, not its first minute
        d = d.replace(hour=23, minute=59)
    return d if d.tzinfo else d.replace(tzinfo=ET)  # no offset given: Eastern


def group_key(r: dict) -> str:
    sol = (r.get("Sol#") or "").strip().upper()
    return f"sol:{sol}" if sol else f"id:{r['NoticeId']}"


def groups(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        try:
            posted(r)
        except ValueError:
            continue
        out.setdefault(group_key(r), []).append(r)
    for rs in out.values():
        rs.sort(key=posted)
    return out


def load_seen(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["key"]: r["first_posted"] for r in csv.DictReader(f)}


def save_seen(seen: dict[str, str], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "first_posted"])
        for k in sorted(seen):
            w.writerow([k, seen[k]])
    tmp.replace(path)


def mark_seen(seen: dict[str, str], gs: dict[str, list[dict]], *, before: dt.date | None = None) -> int:
    """Add every group (or only those first posted before `before`) to seen."""
    n = 0
    for k, rs in gs.items():
        p = posted(rs[0])
        if before and p.date() >= before:
            continue
        if k not in seen:
            seen[k] = p.isoformat()
            n += 1
    return n


def posting(rs: list[dict]) -> dict:
    first, latest = rs[0], rs[-1]
    ds = [d for d in (deadline(r) for r in rs) if d]
    d = max(ds) if ds else None
    p = posted(first)
    return {
        "notice_id": latest["NoticeId"], "first_notice_id": first["NoticeId"], "sol": first["Sol#"].strip(),
        "type": first["Type"], "title": latest["Title"], "agency": latest["Department/Ind.Agency"],
        "subtier": latest["Sub-Tier"], "office": latest["Office"], "naics": latest["NaicsCode"],
        "psc": latest["ClassificationCode"], "set_aside": latest["SetASide"],
        "first_posted": p.isoformat(), "versions": len(rs),
        "deadline": d.isoformat() if d else None,
        "window_days": round((d - p).total_seconds() / 86400, 2) if d else None,
        "place": ", ".join(x for x in [latest["PopCity"], latest["PopState"]] if x),
        "link": f"https://sam.gov/opp/{latest['NoticeId']}/view",
        "description": latest["Description"],
    }


def new_postings(gs: dict[str, list[dict]], day: dt.date, seen: dict[str, str]) -> tuple[list[dict], dict]:
    """Solicitations first posted on or before `day` that we have not seen.

    Not just those posted ON `day`: the extract is refreshed around 11:30 PM
    Eastern, so a posting made late on one day first appears in the next
    day's file. `late` counts those (first posted before `day`)."""
    out, skipped = [], {"not_solicitation_type": 0, "late": 0, "deadline_before_first_posting": 0}
    for k, rs in gs.items():
        if k in seen or posted(rs[0]).date() > day:
            continue
        if rs[0]["Type"] not in TYPES:
            skipped["not_solicitation_type"] += 1
            continue
        p = posting(rs)
        if p["window_days"] is not None and p["window_days"] < 0:
            # an update to an older solicitation whose first versions left the public file
            # (SAM shows them as version 2+, often already archived): its real history is unknown
            skipped["deadline_before_first_posting"] += 1
            continue
        if posted(rs[0]).date() < day:
            skipped["late"] += 1  # counted, and still included
        out.append({"key": k, **p})
    out.sort(key=lambda x: (x["first_posted"], x["key"]))
    return out, skipped
