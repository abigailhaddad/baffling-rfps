"""Attachments of a SAM.gov notice, downloaded and turned into text.

Two public, keyless endpoints the sam.gov website itself uses:
  GET /api/prod/opps/v3/opportunities/{noticeId}/resources       (file list)
  GET /api/prod/opps/v3/opportunities/resources/files/{id}/download  (303 to S3)
They are undocumented, so every sam.gov request is rate-limited and retried.

PDFs are read with pdftotext (poppler), one string per page so quotes can be
given page numbers; a page with (almost) no text is a scanned image, so it is
rendered and read with tesseract OCR (recorded in "ocr_pages"), and a page OCR
cannot read either is recorded in "unread_pages" so the model is told its
content is unknown. .docx files are read from their XML. Other formats (spreadsheets,
old .doc, zip) and link-type resources are listed but not read, and the model
is told so.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import requests

from .sam import USER_AGENT

RESOURCES_URL = "https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources"
DOWNLOAD_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{resource_id}/download"
MAX_BYTES = 60 * 1024 * 1024
PER_SECOND = 2.0
MIN_PAGE_CHARS = 40  # fewer non-space characters than this: treat the page as an image
OCR_MAX_PAGES = 40   # per file; image pages past this are recorded as unread

_lock = threading.Lock()
_next = [0.0]


def _wait() -> None:
    with _lock:
        now = time.monotonic()
        delay = _next[0] - now
        _next[0] = max(now, _next[0]) + 1 / PER_SECOND
    if delay > 0:
        time.sleep(delay)


def _get(url: str, session: requests.Session, **kw) -> requests.Response:
    for attempt in range(6):
        _wait()
        try:
            r = session.get(url, timeout=(20, 120), headers={"User-Agent": USER_AGENT}, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.RequestException:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def ocr_page(path: Path, n: int) -> str:
    """Text of page n (1-based) of a PDF, by rendering it and running tesseract."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-f", str(n), "-l", str(n), "-r", "200", "-gray", "-png", str(path), f"{d}/p"],
                       check=True, capture_output=True, timeout=180)
        img = next(Path(d).glob("p*.png"))
        return subprocess.run(["tesseract", str(img), "-"], capture_output=True, text=True, timeout=180).stdout


def pdf_pages(path: Path) -> tuple[list[str], list[int], list[int]]:
    """(text of each page, pages read by OCR, pages that could not be read), page numbers 1-based."""
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=180).stdout
    pages = out.split("\f")
    pages = pages[:-1] if pages and not pages[-1].strip() else pages
    ocr, unread = [], []
    for i, pg in enumerate(pages):
        if _chars(pg) >= MIN_PAGE_CHARS:
            continue
        text = ""
        if len(ocr) < OCR_MAX_PAGES:
            try:
                text = ocr_page(path, i + 1)
            except (subprocess.SubprocessError, StopIteration, OSError):
                text = ""
        if _chars(text) >= MIN_PAGE_CHARS:
            pages[i] = text
            ocr.append(i + 1)
        else:  # possibly a near-blank page, but we cannot tell, so its content is called unknown
            unread.append(i + 1)
    return pages, ocr, unread


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def fetch(notice_id: str, workdir: Path, session: requests.Session | None = None) -> dict:
    """{"files": [{"name", "type", "size", "pages": [...]} or {..., "skipped": reason}], "error"?}"""
    s = session or requests.Session()
    out: dict = {"notice_id": notice_id, "files": []}
    try:
        r = _get(RESOURCES_URL.format(notice_id=notice_id), s)
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as e:
        out["error"] = f"resource list failed: {e}"
        return out
    d = workdir / notice_id
    d.mkdir(parents=True, exist_ok=True)
    for lst in (payload or {}).get("_embedded", {}).get("opportunityAttachmentList", []) or []:
        for a in lst.get("attachments", []) or []:
            kind = (a.get("type") or "").lower()
            f = {"name": a.get("name") or a.get("description") or "", "type": kind, "size": int(a.get("size") or 0)}
            out["files"].append(f)
            if kind != "file":
                f["skipped"] = "a link, not a file"
                continue
            if str(a.get("deletedFlag") or "0") == "1":
                f["skipped"] = "deleted"
                continue
            if f["size"] > MAX_BYTES:
                f["skipped"] = "too large"
                continue
            low = f["name"].lower()
            if not (low.endswith(".pdf") or low.endswith(".docx")):
                f["skipped"] = "format not read"
                continue
            path = d / (re.sub(r"[^A-Za-z0-9._-]", "_", f["name"])[:120] or a["resourceId"])
            try:
                r = _get(DOWNLOAD_URL.format(resource_id=a["resourceId"]), s, allow_redirects=False)
                if r.status_code in (301, 302, 303, 307, 308):
                    r = s.get(r.headers["Location"], timeout=(20, 300), headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                path.write_bytes(r.content[:MAX_BYTES])
                if low.endswith(".pdf"):
                    f["pages"], f["ocr_pages"], f["unread_pages"] = pdf_pages(path)
                else:
                    f["pages"] = [docx_text(path)]
            except Exception as e:  # noqa: BLE001 - recorded on the file, never hidden
                f["skipped"] = f"could not read: {type(e).__name__}: {e}"[:200]
    return out
