"""Transcribe NPCI's UPI response codes into the CITED tier, and render the table.

    python tools/cite_npci.py extract <path-to-pdf>   # rewrites data/cited_payloads/
    python tools/cite_npci.py table                   # rewrites docs/taxonomy.md §11's table

**What is cited.** Three sections of NPCI's *Unified Payments Interface — Error
and Response Codes*, version 2.9 (17 January 2024), a document NPCI marks
"Public" on every page: §3.1, the codes a remitter or beneficiary bank returns
on a debit or credit, mandate debits included; §4.1, the codes UPI itself
returns on a timeout; and §4.4, errors from the UPI service layer. Registered in
docs/EVALUATION.md §10, 2026-09-15, which says why these three and not the rest.

**Bytes, not a location.** No copy hosted by NPCI was found, so `extract`
refuses any file whose SHA-256 is not `SHA256` below — any copy that matches is
the document cited, wherever it came from, and nothing else is.

**Verbatim, with the one repair recorded.** Codes, descriptions, remarks and the
TD/BD column are copied as printed — capitalisation, typing errors ("ACQURIER")
and all. The extraction needed one layout repair, and it is written down in
`REPAIRS` rather than made silently: a repair that no longer matches the text
raises, so a different layout fails loudly instead of being half-transcribed.

`extract` needs `pypdf`, which is deliberately not a project dependency — the
committed transcription is what everything else reads, and the PDF is not in
this repository. `pip install pypdf` to re-derive it.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
CITED_DIR = ROOT / "data" / "cited_payloads"
TAXONOMY = ROOT / "docs" / "taxonomy.md"

SHA256 = "93584968a0089d37c8cc7d730e1f06820841ea958a8a5bc0ee98bb14c6a231a4"
RETRIEVED = "2026-09-15"
RETRIEVED_FROM = (
    "https://dth95m2xtyv8v.cloudfront.net/tesseract/assets/upi-tpap-sdk/"
    "UPI_Error_and_Response_Codes_2_9-HHLrJ.pdf"
)

DOCUMENT = {
    "tier": "CITED",
    "publisher": "National Payments Corporation of India (NPCI)",
    "document": "Unified Payments Interface — Error and Response Codes",
    "version": "2.9",
    "version_date": "2024-01-17",
    "marking": "Public - UPI V 2.9",
    "retrieved": RETRIEVED,
    "retrieved_from": RETRIEVED_FROM,
    "sha256": SHA256,
    "transcribed_by": "tools/cite_npci.py",
}

SECTIONS = {
    "3.1": {
        "start": "3.1 Response codes in RespPay Debit",
        "end": "3.2 Response codes in RespPayReversal",
        "clause": "§3.1 Response codes in RespPay Debit, RespPay Credit, RespPay Debit "
                  "(Mandate) & RespChkTxn API",
        "pages": "6–11",
        "codes": r"[0-9]{2}|[A-Z][A-Z0-9]",
        "count": 110,
    },
    "4.1": {
        "start": "4.1 Response codes populated by UPI",
        "end": "4.3 UPI API message level Validations",
        "clause": "§4.1 Response codes populated by UPI",
        "pages": "23",
        "codes": r"[0-9]{2}|[A-Z]{2}",
        "count": 7,
    },
    "4.4": {
        "start": "4.4 Errors from UPI Service Layer",
        "end": "4.5 Errors from UPI 2.0 Service Layer",
        "clause": "§4.4 Errors from UPI Service Layer",
        "pages": "59–64",
        "codes": r"[A-Z]{1,2}[0-9]{1,2}",
        "count": 108,
    },
}

REPAIRS = {
    ("3.1", "FL"): (
        "limited to BD Rs5,000", "limited to Rs5,000", "BD",
        "FL's TD/BD cell is printed beside the first half of its remarks, which "
        "then continue on the next page, so extraction reads the flag as a word "
        "of the remark. The flag moves back to its column; no text changes.",
    ),
}
"""(section, code) -> (found, replacement, flag, why)."""

_FURNITURE = re.compile(
    r"^(=== page \d+ ===|UPI Error and Response Codes\s*|\s*Page \d+ of 72 Public - UPI V 2\.9\s*|\s*)$"
)
_HEADERS = re.compile(r"^(Response code Description|API Error Code|Response\s*$|code Description)")
_FLAGS = {"TD", "BD", "NA", "-"}
_TRAILING_FLAG = re.compile(r"\s(TD|BD|NA|-)\s*$")
_REMARKS_BEGIN = ("SCENARIO:", "Members should not")


def _page_text(pdf: pathlib.Path) -> list[str]:
    try:
        import pypdf
    except ImportError:
        raise SystemExit("error: `extract` needs pypdf -- pip install pypdf") from None
    reader = pypdf.PdfReader(pdf)
    text = "\n".join(f"=== page {i + 1} ===\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
    return text.splitlines()


def _section_lines(lines: list[str], start: str, end: str) -> list[str]:
    first = next(i for i, l in enumerate(lines) if l.strip().startswith(start) and "...." not in l)
    last = next(i for i, l in enumerate(lines)
                if i > first and l.strip().startswith(end) and "...." not in l)
    return [l for l in lines[first + 1:last] if not _FURNITURE.match(l)]


def _records(section: str, lines: list[str], code_re: str) -> list[dict]:
    """One record per code. A line holding only a TD/BD flag closes the record
    it wrapped from — the flag column wraps whenever a description runs long."""
    starts = re.compile(rf"^\s*({code_re})(\s+|$)(.*)$")
    raw, current = [], None
    for line in lines:
        stripped = line.strip()
        if _HEADERS.match(stripped):
            continue
        if stripped in _FLAGS and current is not None:
            current["text"] += " " + stripped
            continue
        match = starts.match(line)
        if match:
            if current:
                raw.append(current)
            current = {"code": match.group(1), "text": match.group(3)}
        elif current is not None:
            current["text"] += " " + stripped
    if current:
        raw.append(current)

    out = []
    for record in raw:
        text = re.sub(r"\s+", " ", record["text"]).strip()
        flag = None
        repair = REPAIRS.get((section, record["code"]))
        if repair:
            found, replacement, flag, _ = repair
            if found not in text:
                raise SystemExit(f"error: the recorded repair for §{section} {record['code']} "
                                 "no longer matches — is this the cited document?")
            text = text.replace(found, replacement)
        else:
            trailing = _TRAILING_FLAG.search(" " + text)
            if trailing:
                flag = trailing.group(1)
                text = text[: trailing.start() - 1].strip()
        description, remarks = text, None
        for marker in _REMARKS_BEGIN:
            if marker in text:
                at = text.index(marker)
                description, remarks = text[:at].strip(), text[at:].strip()
                break
        out.append({"code": record["code"], "description": description,
                    "remarks": remarks, "td_bd": flag})
    return out


def cited_path(section: str) -> pathlib.Path:
    return CITED_DIR / f"npci_upi_v2.9__s{section.replace('.', '_')}.json"


def extract(pdf: pathlib.Path) -> None:
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"error: {pdf} has sha256 {digest}, not the cited {SHA256}")
    lines = _page_text(pdf)
    CITED_DIR.mkdir(parents=True, exist_ok=True)
    for section, spec in SECTIONS.items():
        records = _records(section, _section_lines(lines, spec["start"], spec["end"]), spec["codes"])
        if len(records) != spec["count"]:
            raise SystemExit(f"error: §{section} yielded {len(records)} codes, "
                             f"expected {spec['count']}")
        document = {
            "_PROVENANCE": {**DOCUMENT, "clause": spec["clause"], "pages": spec["pages"]},
            "codes": records,
        }
        cited_path(section).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {cited_path(section).relative_to(ROOT)}: {len(records)} codes")


# ---------------------------------------------------------------------------
# docs/taxonomy.md §11's table — generated, never hand-edited
# ---------------------------------------------------------------------------
TABLE_START = "<!-- npci-table:start — generated by `python tools/cite_npci.py table`; do not edit -->"
TABLE_END = "<!-- npci-table:end -->"


def load_cited() -> dict[tuple[str, str], dict]:
    out = {}
    for section in SECTIONS:
        for record in json.loads(cited_path(section).read_text())["codes"]:
            out[(section, record["code"])] = record
    return out


def render_table() -> str:
    from vasool.diagnosis.npci import MAPPINGS, Unmapped
    from vasool.diagnosis.taxonomy import FailureClass

    cited = load_cited()
    order = [*FailureClass, *Unmapped]
    lines = [TABLE_START]
    for outcome in order:
        rows = [m for m in MAPPINGS if m.outcome is outcome]
        if not rows:
            continue
        label = f"`{outcome.value}`" if isinstance(outcome, FailureClass) else f"Unmapped — `{outcome.value}`"
        lines += ["", f"#### {label} ({len(rows)})", "",
                  "| § | Code | NPCI's description | TD/BD | Why |", "|---|---|---|---|---|"]
        for m in rows:
            record = cited[m.key]
            description = record["description"].replace("|", "\\|")
            lines.append(f"| {m.section} | `{m.code}` | {description} | {record['td_bd'] or '—'} | {m.why} |")
    lines += ["", TABLE_END]
    return "\n".join(lines)


def write_table() -> None:
    text = TAXONOMY.read_text()
    if TABLE_START not in text or TABLE_END not in text:
        raise SystemExit("error: docs/taxonomy.md has no npci-table markers")
    head, rest = text.split(TABLE_START, 1)
    _, tail = rest.split(TABLE_END, 1)
    TAXONOMY.write_text(head + render_table() + tail)
    print("rewrote docs/taxonomy.md §11's table")


def main(argv: list[str]) -> int:
    if argv[:1] == ["extract"] and len(argv) == 2:
        extract(pathlib.Path(argv[1]))
    elif argv == ["table"]:
        write_table()
    else:
        print(__doc__.strip().splitlines()[2:4], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
