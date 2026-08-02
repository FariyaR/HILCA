"""Phase 2 of the faithful HILCA flow — context gathering.

Port of the original UiPath activities #19-#40:
  - Download the HILCA MasterReference file -> read its text   (here: local .docx)
  - Web Reader scrape of each intake URL                        (here: urllib + HTML text extraction)
  - Download files from URL cells; PDFs -> extract text         (here: same fetch; pypdf when available)
  - Assemble ContextMaterial = reference text + scraped/extracted text

Everything is stdlib except optional pypdf for PDF text extraction.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from html.parser import HTMLParser
from typing import Callable, List, Optional
from urllib.request import Request, urlopen

# v2 (2026-07: Behavioral Protocol + refined prompt pack + engineering notes) is
# the current master file; the old docx remains as a fallback for existing envs.
_REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference")
_V2 = os.path.join(_REF_DIR, "HILCA_master_reference_v2.txt")
DEFAULT_REFERENCE_PATH = _V2 if os.path.exists(_V2) else os.path.join(
    _REF_DIR, "Copy of HILCA master reference .docx"
)
FETCH_TIMEOUT = 30  # seconds per URL
# ContextMaterial is re-injected into every sub-agent call each round, so an
# unbounded reference/scrape would multiply across the whole run. Cap each
# source and the total (configurable); the original bounded this the same way
# via RAG grounding ("Number of results: 3").
MAX_REFERENCE_CHARS = int(os.getenv("HILCA_MAX_REFERENCE_CHARS", "60000"))
MAX_SOURCE_CHARS = int(os.getenv("HILCA_MAX_SOURCE_CHARS", "20000"))
MAX_CONTEXT_CHARS = int(os.getenv("HILCA_MAX_CONTEXT_CHARS", "120000"))
# Byte cap on each evidence download (read in chunks; a huge or endless stream
# must not OOM the server before the char clip even runs).
MAX_FETCH_BYTES = int(os.getenv("HILCA_MAX_FETCH_BYTES", str(8 * 1024 * 1024)))

_USER_AGENT = "Mozilla/5.0 (compatible; HILCA/1.0; +dialectic-research)"


# --- DOCX -> text (stdlib: a .docx is a zip of XML) ---------------------------
def extract_docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    # Paragraph ends -> newlines, then strip all remaining tags.
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    # Decode the handful of XML entities that appear in Word documents.
    # &amp; must go LAST or double-escaped text (&amp;lt;) would double-decode.
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(ent, ch)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- HTML -> visible text ------------------------------------------------------
class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def extract_html_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # keep whatever was parsed before the malformed markup
    return parser.text()


# --- PDF -> text (pypdf, optional dependency) ---------------------------------
def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # lazy
    except ImportError:
        return "[PDF downloaded but pypdf is not installed; text not extracted]"
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:
        return f"[PDF text extraction failed: {exc}]"


# --- URL fetch: web page OR file (his Web Reader + Download Files steps) ------
def fetch_url_text(url: str) -> str:
    """Fetch one evidence URL and return its extracted text.

    Mirrors the original's two-step treatment (Web Reader scrape + file
    download): PDFs get text-extracted, HTML gets visible-text-extracted,
    anything else is decoded as plain text.

    Only http/https are allowed — evidence URLs are user input, and urlopen
    would otherwise happily read file:// paths off the server's disk.
    The body is read in chunks up to MAX_FETCH_BYTES.
    """
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in ("http", "https"):
        raise ValueError(f"evidence URL scheme not allowed: {scheme or '(none)'} (only http/https)")

    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        chunks, total = [], 0
        while total < MAX_FETCH_BYTES:
            chunk = resp.read(min(65536, MAX_FETCH_BYTES - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        return extract_pdf_text(data)
    text = data.decode("utf-8", errors="replace")
    if "html" in content_type or "<html" in text[:2000].lower():
        return extract_html_text(text)
    return text.strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... truncated ...]"


# --- The assembled ContextMaterial ---------------------------------------------
def load_master_reference(path: Optional[str] = None) -> str:
    """Step: Download File HILCA MasterReference -> ReferenceText."""
    ref_path = path or os.getenv("HILCA_REFERENCE_PATH", DEFAULT_REFERENCE_PATH)
    if not os.path.exists(ref_path):
        raise FileNotFoundError(f"HILCA Master Reference not found at: {ref_path}")
    if ref_path.lower().endswith(".docx"):
        return extract_docx_text(ref_path)
    with open(ref_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def gather_context(
    evidence_urls: List[str],
    reference_path: Optional[str] = None,
    log: Callable[[str], None] = lambda m: None,
) -> str:
    """Run Phase 2 end-to-end and return the assembled ContextMaterial.

    ContextMaterial = ReferenceText + scraped URL text + extracted file text,
    in intake order. Failed fetches are logged and skipped (the original only
    logged on success and moved on).
    """
    reference_text = load_master_reference(reference_path)
    log("new row read and masterreference downloaded")

    parts: List[str] = [
        "=== HILCA MASTER REFERENCE ===\n" + _clip(reference_text, MAX_REFERENCE_CHARS)
    ]
    for i, url in enumerate(evidence_urls, start=1):
        url = (url or "").strip()
        if not url:
            continue  # skip empty cells, as the original's if-not-empty guards did
        try:
            text = fetch_url_text(url)
            parts.append(f"=== EVIDENCE SOURCE {i}: {url} ===\n" + _clip(text, MAX_SOURCE_CHARS))
            log(f"evidence url {i} scraped/downloaded: {url}")
        except Exception as exc:
            parts.append(f"=== EVIDENCE SOURCE {i}: {url} ===\n[fetch failed: {exc}]")
            log(f"evidence url {i} failed: {url} ({exc})")
    log("supportive intake websites and files were scraped and downloaded")

    return _clip("\n\n".join(parts), MAX_CONTEXT_CHARS)
