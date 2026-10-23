"""Send one stage's requests as a Message Batch (half price), and keep the
day's spending under a cap.

`run` submits every request, waits for the batch to end, and resubmits once
whatever failed (errored, expired, refused, cut off, unparseable, or rejected
by the caller's `valid` check). Requests that fail twice come back as
(None, reason, tokens) and the caller records them as not rated.

`Budget` adds up what each stage actually cost (batch prices, from the token
counts the API reports). Before a stage, `fit` estimates each request's cost
and keeps only as many as the money left allows, in the order given (callers
put the most suspicious first); the rest are recorded as not rated.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from . import rate

DAILY_CAP_USD = float(os.environ.get("BAFFLING_DAILY_CAP_USD", "3"))
POLL_SECONDS = 30
TIMEOUT_SECONDS = 5 * 3600
CHARS_PER_TOKEN = 3.5  # for the pre-stage estimate only; actual cost uses reported tokens
# expected output tokens per request, from the 2026-10-19..21 runs (rounded up)
EXPECTED_OUT = {"prescreen": 1500, "pass1": 1500, "pass2": 1200, "verify": 1000}

log = logging.getLogger(__name__)


def estimate(p: dict, stage: str) -> float:
    chars = sum(len(m["content"]) for m in p["messages"])
    return rate.cost(p["model"], [int(chars / CHARS_PER_TOKEN), EXPECTED_OUT[stage]], batch=True)


class Budget:
    def __init__(self, cap: float = DAILY_CAP_USD):
        self.cap, self.spent, self.by_stage = cap, 0.0, {}

    def add(self, stage: str, model: str, tokens: list[int]) -> None:
        c = rate.cost(model, tokens, batch=True)
        self.spent += c
        self.by_stage[stage] = round(self.by_stage.get(stage, 0.0) + c, 4)

    def fit(self, reqs: list[tuple[str, dict]], stage: str) -> tuple[list[tuple[str, dict]], list[str]]:
        """(requests that fit in what is left, ids left out). Estimates allow for one retry of 10%."""
        left, keep, cut = self.cap - self.spent, [], []
        for cid, p in reqs:
            e = estimate(p, stage) * 1.1
            if e <= left:
                keep.append((cid, p))
                left -= e
            else:
                cut.append(cid)
        return keep, cut


def _submit_and_wait(c, reqs: dict[str, dict]) -> dict:
    batch = c.messages.batches.create(requests=[{"custom_id": cid, "params": p} for cid, p in reqs.items()])
    log.info("batch %s: %d requests", batch.id, len(reqs))
    start = time.monotonic()
    while batch.processing_status != "ended":
        if time.monotonic() - start > TIMEOUT_SECONDS:
            c.messages.batches.cancel(batch.id)
            raise RuntimeError(f"batch {batch.id} did not finish in {TIMEOUT_SECONDS}s; canceled")
        time.sleep(POLL_SECONDS)
        batch = c.messages.batches.retrieve(batch.id)
    out = {}
    for r in c.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            out[r.custom_id] = rate.parse(r.result.message)
        else:
            out[r.custom_id] = (None, r.result.type, [0, 0])
    for cid in reqs:  # a request missing from the results counts as failed
        out.setdefault(cid, (None, "missing", [0, 0]))
    return out


def run(c, reqs: dict[str, dict], stage: str, budget: Budget,
        valid: Callable[[str, dict], bool] | None = None) -> dict:
    """{custom_id: (parsed or None, stop or failure reason, [in, out] tokens summed over tries)}"""
    if not reqs:
        return {}
    results = _submit_and_wait(c, reqs)
    for cid, (_, _, tok) in results.items():
        budget.add(stage, reqs[cid]["model"], tok)

    def failed(cid):
        parsed = results[cid][0]
        return parsed is None or (valid is not None and not valid(cid, parsed))

    retry = {cid: reqs[cid] for cid in reqs if failed(cid)}
    if retry:
        log.info("%s: retrying %d failed requests", stage, len(retry))
        again = _submit_and_wait(c, retry)
        for cid, (parsed, stop, tok) in again.items():
            budget.add(stage, reqs[cid]["model"], tok)
            prev = results[cid][2]
            ok = parsed is not None and (valid is None or valid(cid, parsed))
            results[cid] = (parsed if ok else None, stop if ok or parsed is None else "invalid",
                            [prev[0] + tok[0], prev[1] + tok[1]])
    return results
