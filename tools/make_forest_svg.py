"""Render EXHIBIT A's forest plot as a standalone SVG for README.md.

Why a committed file rather than a screenshot: an SVG is text. A reader — or
an automated one — gets the numbers out of it without OCR, and a diff shows
which figure moved. It is regenerated from the same manifest the dashboard
reads, so the two cannot disagree.

    python3 tools/make_forest_svg.py
"""
from __future__ import annotations

import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "out" / "development" / "evaluation.json"
OUT_LIGHT = REPO_ROOT / "docs" / "assets" / "forest-light.svg"
OUT_DARK = REPO_ROOT / "docs" / "assets" / "forest-dark.svg"

AHEAD, BEHIND = "#2a78d6", "#d0463f"      # light-mode diverging pair
AHEAD_D, BEHIND_D = "#3987e5", "#e66767"  # dark steps of the same two hues


def build(report: dict, *, dark: bool) -> str:
    rows = sorted(
        (
            {"arm": arm, **m["recovery_rate"]}
            for arm, m in report["paired_vs_vasool"].items()
            if m.get("recovery_rate")
        ),
        key=lambda r: r["point"],
    )
    pad_l, pad_r, pad_t, row_h = 190, 108, 58, 40
    width = 900
    height = pad_t + row_h * len(rows) + 54
    lo = min(min(r["low"] for r in rows), 0.0)
    hi = max(max(r["high"] for r in rows), 0.0)
    span = (hi - lo) or 1.0
    x0, x1 = lo - span * 0.1, hi + span * 0.1
    scale = lambda v: pad_l + (v - x0) / (x1 - x0) * (width - pad_l - pad_r)

    # Explicit per-theme values. The palette is the validated diverging pair;
    # only the surface and the ink change between the two files.
    surface = "#0d1117" if dark else "#ffffff"
    ink = "#e6edf3" if dark else "#1f2328"
    muted = "#8b949e" if dark else "#656d76"
    grid = "#21262d" if dark else "#e4e8ed"
    zero = "#8b949e" if dark else "#57606a"
    ahead = AHEAD_D if dark else AHEAD
    behind = BEHIND_D if dark else BEHIND

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="Paired difference in recovery rate against Vasool, 95 percent bootstrap intervals">',
        "<style>",
        f"  .lbl{{font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;fill:{ink}}}",
        f"  .sub{{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;fill:{muted}}}",
        f"  .tick{{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;fill:{muted}}}",
        "  .val{font:600 12.5px ui-monospace,SFMono-Regular,Menlo,monospace}",
        f"  .ttl{{font:600 14px -apple-system,Segoe UI,sans-serif;fill:{ink}}}",
        f"  .grid{{stroke:{grid};stroke-width:1}}",
        f"  .zero{{stroke:{zero};stroke-width:2;stroke-dasharray:5 4}}",
        f"  .ring{{stroke:{surface}}}",
        "</style>",
        f'<rect width="{width}" height="{height}" fill="{surface}"/>',
        f'<text x="{pad_l}" y="18" class="ttl">Paired difference vs Vasool — recovery rate, 1,000 seeds</text>',
    ]

    step = 5.0
    start = -((-x0 * 100) // step) * step
    v = start
    while v <= x1 * 100 + 1e-9:
        x = scale(v / 100)
        parts.append(f'<line x1="{x:.1f}" y1="{pad_t - 8}" x2="{x:.1f}" y2="{height - 42}" class="grid"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{height - 24}" text-anchor="middle" class="tick">'
            f'{"+" if v > 0 else ""}{v:.0f}pp</text>'
        )
        v += step

    zx = scale(0.0)
    parts.append(f'<line x1="{zx:.1f}" y1="{pad_t - 8}" x2="{zx:.1f}" y2="{height - 42}" class="zero"/>')
    parts.append(f'<text x="{zx - 10:.1f}" y="{pad_t - 18}" text-anchor="end" class="sub">&#8592; Vasool recovers less</text>')
    parts.append(f'<text x="{zx + 10:.1f}" y="{pad_t - 18}" text-anchor="start" class="sub">Vasool recovers more &#8594;</text>')

    for i, r in enumerate(rows):
        y = pad_t + i * row_h + row_h / 2
        colour = behind if r["point"] < 0 else ahead
        cls = f"c{i}"
        parts.append(f"<style>.{cls}{{stroke:{colour};fill:{colour}}}</style>")
        parts.append(f'<text x="{pad_l - 18}" y="{y + 1:.1f}" text-anchor="end" class="lbl">{r["arm"]}</text>')
        parts.append(
            f'<text x="{pad_l - 18}" y="{y + 14:.1f}" text-anchor="end" class="sub">'
            f'{"excludes zero" if r["excludes_zero"] else "includes zero"}</text>'
        )
        parts.append(
            f'<line x1="{scale(r["low"]):.2f}" y1="{y:.1f}" x2="{scale(r["high"]):.2f}" y2="{y:.1f}" '
            f'class="{cls}" stroke-width="2" stroke-linecap="round"/>'
        )
        parts.append(
            f'<circle cx="{scale(r["point"]):.2f}" cy="{y:.1f}" r="5.5" class="{cls} ring" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{width - pad_r + 16}" y="{y + 4:.1f}" class="val {cls}" stroke="none">'
            f'{"+" if r["point"] >= 0 else ""}{r["point"] * 100:.2f}pp</text>'
        )
        parts.append(f"<!-- {r['arm']}: {r['point'] * 100:+.3f}pp "
                     f"[{r['low'] * 100:+.3f}, {r['high'] * 100:+.3f}] -->")

    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    report = json.loads(MANIFEST.read_text())
    OUT_LIGHT.parent.mkdir(parents=True, exist_ok=True)
    OUT_LIGHT.write_text(build(report, dark=False), encoding="utf-8")
    OUT_DARK.write_text(build(report, dark=True), encoding="utf-8")
    print(f"wrote {OUT_LIGHT}\nwrote {OUT_DARK}")
