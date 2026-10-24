"""Send one stage's requests as a Message Batch (half price), and keep the
day's spending under a cap.

`run` submits every request, waits for the batch to end, and resubmits once
whatever failed (errored, expired, refused, cut off, unparseable, or rejected
by the caller's `valid` check). Requests that fail twice come back as
(None, reason, tokens) and the caller records them as not rated.

A batch that has not ended after TIMEOUT_SECONDS is canceled; what it did
finish is kept, and each request it never got to is sent as a regular
(non-batch) request at full price, if the day's cap still allows. Added
2026-10-24: that day's run sat 5 hours on a 35-request verify batch that
finished 16 (the three batches before it took ~3 minutes each), hit the old
5-hour timeout, and failed the whole day.

`Budget` adds up what each stage actually cost (batch or full prices, from
the token counts the API reports). Before a stage, `fit` estimates each request's cost
and keeps only as many as the money left allows, in the order given (callers
put the most suspicious first); the rest are recorded as not rated.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import rate

DAILY_CAP_USD = float(os.environ.get("BAFFLING_DAILY_CAP_USD", "3"))
POLL_SECONDS = 30
TIMEOUT_SECONDS = int(os.environ.get("BAFFLING_BATCH_TIMEOUT_SECONDS", str(45 * 60)))
CANCEL_WAIT_SECONDS = 10 * 60
TIMED_OUT = "batch_timeout"
CHARS_PER_TOKEN = 3.5  # for the pre-stage estimate only; actual cost uses reported tokens
# expected output tokens per request, from the 2026-10-19..21 runs (rounded up)
EXPECTED_OUT = {"prescreen": 1500, "pass1": 1500, "pass2": 1200, "verify": 1000}

log = logging.getLogger(__name__)


def estimate(p: dict, stage: str, batch: bool = True) -> float:
    chars = sum(len(m["content"]) for m in p["messages"])
    return rate.cost(p["model"], [int(chars / CHARS_PER_TOKEN), EXPECTED_OUT[stage]], batch=batch)


class Budget:
    def __init__(self, cap: float = DAILY_CAP_USD):
        self.cap, self.spent, self.by_stage = cap, 0.0, {}

    def add(self, stage: str, model: str, tokens: list[int], batch: bool = True) -> None:
        c = rate.cost(model, tokens, batch=batch)
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
    """{custom_id: (parsed or None, stop or failure reason, tokens)}. A request
    the batch never finished because it timed out comes back as TIMED_OUT."""
    batch = c.messages.batches.create(requests=[{"custom_id": cid, "params": p} for cid, p in reqs.items()])
    log.info("batch %s: %d requests", batch.id, len(reqs))
    start, timed_out = time.monotonic(), False
    while batch.processing_status != "ended":
        if not timed_out and time.monotonic() - start > TIMEOUT_SECONDS:
            log.warning("batch %s not done after %ds; canceling it and keeping what it finished",
                        batch.id, TIMEOUT_SECONDS)
            c.messages.batches.cancel(batch.id)
            timed_out, start = True, time.monotonic()
        elif timed_out and time.monotonic() - start > CANCEL_WAIT_SECONDS:
            log.warning("batch %s still not ended %ds after cancel; treating it as unfinished",
                        batch.id, CANCEL_WAIT_SECONDS)
            break
        time.sleep(POLL_SECONDS)
        batch = c.messages.batches.retrieve(batch.id)
    out = {}
    if batch.processing_status == "ended":
        for r in c.messages.batches.results(batch.id):
            if r.result.type == "succeeded":
                out[r.custom_id] = rate.parse(r.result.message)
            elif r.result.type == "canceled" and timed_out:
                out[r.custom_id] = (None, TIMED_OUT, [0, 0])
            else:
                out[r.custom_id] = (None, r.result.type, [0, 0])
    for cid in reqs:  # a request missing from the results counts as failed
        out.setdefault(cid, (None, TIMED_OUT if timed_out else "missing", [0, 0]))
    return out


def _direct(c, p: dict) -> tuple[dict | None, str, list[int]]:
    try:
        return rate.parse(c.messages.create(**p))
    except Exception as e:  # noqa: BLE001 - recorded as this request's failure, never dropped
        return None, f"error: {type(e).__name__}", [0, 0]


def _send_timed_out(c, reqs: dict[str, dict], results: dict, stage: str, budget: "Budget") -> set[str]:
    """Send every TIMED_OUT request as a regular request, as far as the cap
    allows (full price); the rest stay failed. Returns the ids sent."""
    todo = [(cid, reqs[cid]) for cid, r in results.items() if r[1] == TIMED_OUT]
    if not todo:
        return set()
    left, send = budget.cap - budget.spent, []
    for cid, p in todo:
        e = estimate(p, stage, batch=False) * 1.1
        if e <= left:
            send.append((cid, p))
            left -= e
        else:
            results[cid] = (None, "daily budget", results[cid][2])
    log.info("%s: sending %d timed-out requests directly (full price); %d left unrated by the cap",
             stage, len(send), len(todo) - len(send))
    with ThreadPoolExecutor(4) as pool:
        for (cid, p), r in zip(send, pool.map(lambda x: _direct(c, x[1]), send)):
            results[cid] = r
    return {cid for cid, _ in send}


def run(c, reqs: dict[str, dict], stage: str, budget: Budget,
        valid: Callable[[str, dict], bool] | None = None) -> dict:
    """{custom_id: (parsed or None, stop or failure reason, [in, out] tokens summed over tries)}"""
    if not reqs:
        return {}
    results = _submit_and_wait(c, reqs)
    for cid, (_, _, tok) in results.items():
        budget.add(stage, reqs[cid]["model"], tok)
    for cid in _send_timed_out(c, reqs, results, stage, budget):
        budget.add(stage, reqs[cid]["model"], results[cid][2], batch=False)

    def failed(cid):
        parsed = results[cid][0]
        return parsed is None or (valid is not None and not valid(cid, parsed))

    # Not a request the cap kept from being sent: retrying it would spend past the cap.
    retry = {cid: reqs[cid] for cid in reqs if failed(cid) and results[cid][1] != "daily budget"}
    if retry:
        log.info("%s: retrying %d failed requests", stage, len(retry))
        again = _submit_and_wait(c, retry)
        for cid, (_, _, tok) in again.items():
            budget.add(stage, reqs[cid]["model"], tok)
        for cid in _send_timed_out(c, retry, again, stage, budget):
            budget.add(stage, reqs[cid]["model"], again[cid][2], batch=False)
        for cid, (parsed, stop, tok) in again.items():
            prev = results[cid][2]
            ok = parsed is not None and (valid is None or valid(cid, parsed))
            results[cid] = (parsed if ok else None, stop if ok or parsed is None else "invalid",
                            [prev[0] + tok[0], prev[1] + tok[1]])
    return results
