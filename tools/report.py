#!/usr/bin/env python3
"""Builds out/report.html from out/development/evaluation.json.

This is the Vasool 'Proof Machine' — a zero-dependency HTML dashboard that
injects the JSON ledger at build time. It renders a client-side interactive
audit of the FSM constraints, highlighting the yield, safety, and cryptographic
determinism of the system.

**The page is a Jinja2 template**, `tools/templates/report.html.j2`; this module
computes what the page is given and nothing about how it looks. Until
2026-09-16 the page was 2,341 lines of HTML, CSS and JavaScript inside one
f-string, with 503 escaped brace pairs and no tool that could read it as what it
was — the structural debt ARCHITECTURE.md named, and the shape two real bugs
came out of (POSTMORTEM.md INC-006). The template was generated from that
f-string's own parse and renders the page byte for byte as it rendered before.
Autoescaping is off because every value is HTML or JSON built here on purpose,
and an undefined name raises rather than rendering as nothing.
"""

import json
import pathlib
import re
import sys
import urllib.parse

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    # Run as `python tools/report.py`, the script's own directory is on the
    # path and the repository is not; the guard chain is read from the code.
    sys.path.insert(0, str(REPO))

_TEMPLATES = Environment(
    loader=FileSystemLoader(pathlib.Path(__file__).resolve().parent / "templates"),
    autoescape=False,
    undefined=StrictUndefined,
)

def guard_chain() -> dict:
    """The chain the page draws, read from the registry rather than copied.

    The dashboard used to carry its own list of the fifteen guards, with a
    clause beside each written by hand -- a copy of `GUARD_CHAIN` that a test
    had to compare name by name, and whose clauses no test compared at all.
    It is serialised here instead: each guard's own `name` and `statute`, in
    evaluation order, with COMPLIANCE.md's G number beside it. The counts the
    page prints are this list's.
    """
    from vasool.policy.registry import GUARD_CHAIN

    ids = dict(
        (name, gid)
        for gid, name in re.findall(r"^\| (G\d\d) \| `(\w+)` \|", (REPO / "COMPLIANCE.md").read_text(), re.M)
    )
    guards = [
        {"id": ids.get(g.name), "name": g.name, "statute": getattr(g, "statute", None)}
        for g in GUARD_CHAIN
    ]
    return {
        "guards": guards,
        "count": len(guards),
        "statutes": sum(g["statute"] is not None for g in guards),
    }


def build_report(json_path: pathlib.Path, out_path: pathlib.Path) -> None:
    if not json_path.exists():
        print(f"error: {json_path} not found. run 'make sweeps' first.", file=sys.stderr)
        sys.exit(1)
        
    with open(json_path, "r", encoding="utf-8") as f:
        # Load and reserialize to ensure clean syntax without manual string replacements
        try:
            raw_data = json.load(f)
        except json.JSONDecodeError:
            print(f"error: {json_path} is corrupted. run 'make sweeps' first.", file=sys.stderr)
            sys.exit(1)

    # §4.5's rules-vs-LLM comparison. Optional: absent on a clone that has not
    # run `make shadow`, and the exhibit renders "not run" rather than nothing.
    # Complete beats partial, always. The preference used to run the other way,
    # from when partial was the only artifact that existed -- which meant that
    # the moment a full run landed, a stale `_partial` left on disk silently
    # kept rendering. A superseded partial is not evidence about anything.
    shadow_dir = json_path.parent.parent / "shadow"
    shadow_path = shadow_dir / "classifier_comparison.json"
    if not shadow_path.exists():
        shadow_path = shadow_dir / "classifier_comparison_partial.json"
    shadow_data = {}
    if shadow_path.exists():
        try:
            shadow_data = json.loads(shadow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            shadow_data = {}

    # §3c's holdout, if it has been evaluated. Money recovered is a fact about
    # the whole population, and the hero quotes it — reporting only the
    # development cohort's share there while README.md quotes the total is how
    # two correct numbers turn into one apparent contradiction.
    # The fresh range first (§10, 2026-09-19): out/holdout/evaluation.json is
    # the 2026-08-29 run, which describes an agent three re-runs old, while
    # out/holdout/fresh/ is the cohort this agent was evaluated on. Both are
    # kept — the spent one is the only record of an execution §3c forbids
    # repeating — and the newer one is what the page reports.
    holdout_root = json_path.parent.parent / "holdout"
    holdout_data = {}
    for candidate in (holdout_root / "fresh" / "evaluation.json", holdout_root / "evaluation.json"):
        if not candidate.exists():
            continue
        try:
            holdout_data = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            holdout_data = {}
        if holdout_data:
            break

    # §2a's adversary. Optional the same way the shadow artifact is.
    redteam_path = json_path.parent.parent / "adversary" / "redteam.json"
    redteam_data = {}
    if redteam_path.exists():
        try:
            redteam_data = json.loads(redteam_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            redteam_data = {}

    # --- Exhibit F's data ------------------------------------------------
    # Invariant 1 is a property of the import graph, and tests/test_shadow_
    # boundary.py already proves it by walking that graph with `ast`. Until now
    # the exhibit *illustrated* the claim with two boxes and a bar; this walks
    # the same graph the test walks and renders what is actually there, so the
    # picture is a measurement rather than a drawing of one.
    def _import_graph():
        import ast as _ast

        roots = ("vasool", "windtunnel", "tools")
        repo = pathlib.Path(__file__).resolve().parent.parent

        def name_of(path):
            parts = list(path.relative_to(repo).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            return ".".join(parts)

        def imports_of(path):
            found = set()
            for node in _ast.walk(_ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, _ast.Import):
                    for alias in node.names:
                        found.add(alias.name)
                elif isinstance(node, _ast.ImportFrom):
                    if node.level or node.module is None:
                        continue
                    found.add(node.module)
                    for alias in node.names:
                        found.add(f"{node.module}.{alias.name}")
            return {n for n in found if n.split(".")[0] in roots}

        graph = {}
        for root in roots:
            base = repo / root
            if not base.is_dir():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                try:
                    graph[name_of(path)] = imports_of(path)
                except SyntaxError:
                    continue

        modules = set(graph)

        def reach(start):
            seen, frontier = set(), [start]
            while frontier:
                for nxt in graph.get(frontier.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
            return seen

        llm = "vasool.diagnosis.llm"
        acting = (
            "vasool.actions.executor", "vasool.actions.razorpay_client",
            "vasool.actions.comms", "vasool.ledger.receipts",
            "vasool.policy.machine", "vasool.events.receiver",
            "windtunnel.runner", "windtunnel.adversary.arena",
            "windtunnel.adversary.harness", "vasool.demo",
        )
        llm_side = ((reach(llm) | {llm}) & modules) if llm in graph else set()
        act_side = set()
        for m in acting:
            if m in graph:
                act_side |= (reach(m) | {m}) & modules
        shared = sorted(llm_side & act_side)
        return {
            "total": len(modules),
            "llm_only": sorted(llm_side - act_side),
            "shared": shared,
            "acting_only": sorted(act_side - llm_side),
            "acting_roots": [m for m in acting if m in graph],
            "llm_importers": sorted(m for m, i in graph.items() if llm in i),
            "llm_reaches_actor": sorted(llm_side & set(acting)),
            "actor_reaches_llm": llm in act_side,
        }

    try:
        airgap = _import_graph()
    except Exception:
        airgap = None

    def _airgap_svg(g):
        """Two cones converging on shared data, and a gap with nothing in it.

        The arrows point from importer to imported, so both planes point *into*
        the shared vocabulary: they agree on the type definitions and share no
        path. The dashed rule is drawn because the finding is an absence, and an
        absence is otherwise invisible.
        """
        if not g:
            return "<p class='viz-caption'>Import graph unavailable at build time.</p>"

        short = lambda m: m.replace("vasool.", "").replace("windtunnel.", "wt.")
        mid = g["shared"][:3]
        acts = g["acting_roots"][:4]
        extra = len(g["acting_only"]) - len(acts)

        rows = []
        for i, m in enumerate(mid):
            y = 128 + i * 46
            rows.append(
                f'<rect x="352" y="{y - 17}" width="236" height="34" rx="2" class="ag-node ag-shared"/>'
                f'<text x="470" y="{y + 5}" class="ag-t" text-anchor="middle">{short(m)}</text>'
                f'<line x1="252" y1="174" x2="346" y2="{y}" class="ag-edge"/>'
                f'<line x1="688" y1="{174 if i == 1 else 128 + i * 46}" x2="594" y2="{y}" class="ag-edge"/>'
            )

        right = []
        for i, m in enumerate(acts):
            y = 105 + i * 46
            right.append(
                f'<rect x="694" y="{y - 16}" width="212" height="32" rx="2" class="ag-node ag-act"/>'
                f'<text x="800" y="{y + 4}" class="ag-t" text-anchor="middle">{short(m)}</text>'
            )
        if extra > 0:
            right.append(f'<text x="800" y="{105 + len(acts) * 46 + 6}" class="ag-t ag-dim" '
                         f'text-anchor="middle">+ {extra} more modules</text>')

        imp = " &middot; ".join(short(m) for m in g["llm_importers"])
        return f"""<svg viewBox="0 0 940 318" class="airgap-svg" role="img"
     aria-label="Import graph: the LLM module and the execution plane both import three shared
     data modules, and no edge connects them in either direction.">
  <text x="150" y="34" class="ag-h" text-anchor="middle">SHADOW PLANE</text>
  <text x="470" y="34" class="ag-h" text-anchor="middle">SHARED VOCABULARY</text>
  <text x="800" y="34" class="ag-h" text-anchor="middle">EXECUTION PLANE</text>
  <text x="150" y="52" class="ag-c" text-anchor="middle">{len(g['llm_only'])} module</text>
  <text x="470" y="52" class="ag-c" text-anchor="middle">{len(g['shared'])} modules &middot; pure data</text>
  <text x="800" y="52" class="ag-c" text-anchor="middle">{len(g['acting_only'])} modules</text>

  <line x1="310" y1="72" x2="310" y2="300" class="ag-gap"/>
  <line x1="630" y1="72" x2="630" y2="300" class="ag-gap"/>

  {''.join(rows)}
  <rect x="44" y="157" width="212" height="34" rx="2" class="ag-node ag-llm"/>
  <text x="150" y="179" class="ag-t" text-anchor="middle">diagnosis.llm</text>
  <text x="150" y="228" class="ag-c ag-dim" text-anchor="middle">imported by {len(g['llm_importers'])}</text>
  <text x="150" y="246" class="ag-t ag-dim" text-anchor="middle">{imp}</text>
  <text x="150" y="266" class="ag-c ag-dim" text-anchor="middle">neither is an actor</text>
  {''.join(right)}
</svg>
<p class="ag-legend">No edge crosses either dashed rule, in either direction &mdash;
{len(g['llm_reaches_actor'])} paths from <code>diagnosis.llm</code> to anything that acts,
and it is unreachable from all {len(g['acting_roots'])} execution roots.
{g['total']} modules parsed.</p>"""

    airgap_svg = _airgap_svg(airgap)

    # A no-JavaScript rendering of the headline figures, generated here rather
    # than written by hand. Every figure on this page is drawn client-side from
    # the embedded JSON, which means a reader -- or an evaluating agent -- that
    # fetches the raw HTML without executing scripts sees a dash where every
    # number should be. The data is right there in the document; only the
    # rendering needed JavaScript. These rows come off the same manifests the
    # scripts read, at build time, so they cannot drift from what the page
    # shows once it runs.
    def _fig(value, fmt="{:.2%}"):
        return fmt.format(value) if isinstance(value, (int, float)) else "&mdash;"

    _arms = raw_data.get("per_arm", {})
    _v, _b, _u = (_arms.get(k, {}) for k in ("vasool", "retry_plus_contact", "vasool_ungated"))
    _hold = holdout_data.get("per_arm", {}).get("vasool", {})
    # Cohorts are added only when one agent produced both — the same rule the
    # hero's script applies. The holdout of 2026-08-29 carries no fingerprint,
    # so from the first re-measurement of the development cohort under a
    # changed agent it is reported beside the result, never summed into it.
    _same_agent = bool(holdout_data) and (
        holdout_data.get("agent_fingerprint") == raw_data.get("agent_fingerprint")
    )
    _paise = (_v.get("recovered_paise_total") or 0) + (
        (_hold.get("recovered_paise_total") or 0) if _same_agent else 0
    )
    # "Across both cohorts" would say one population; the two are disjoint seed
    # ranges of one agent, and §10's 2026-09-19 row fixes the wording the sum
    # may use.
    hero_scope = (
        "recovered across 2,000 seeded universes"
        if _same_agent
        else "recovered in the development cohort"
    )
    _closure = _v.get("closure", {})
    _rt = redteam_data or {}
    _sh = (shadow_data or {}).get("overall", {})

    noscript_rows = "\n".join(
        f"<tr><th scope='row'>{label}</th><td>{value}</td></tr>"
        for label, value in [
            ("Money recovered, " + ("both cohorts" if _same_agent else "development cohort"),
             f"&#8377;{_paise / 100 / 1e7:,.2f} Cr" if _paise else "&mdash;"),
            ("Vasool recovery rate", _fig(_v.get("recovery_rate_mean"))),
            ("Incumbent (retry_plus_contact)", _fig(_b.get("recovery_rate_mean"))),
            ("Ungated (no guard chain)", _fig(_u.get("recovery_rate_mean"))),
            ("&sect;2a safety predicate, Vasool",
             f"{_v.get('safety_holds_on')} / {_v.get('seeds')} seeds"
             if _v.get("seeds") else "&mdash;"),
            ("&sect;2a safety predicate, incumbent",
             f"{_b.get('safety_holds_on')} / {_b.get('seeds')} seeds"
             if _b.get("seeds") else "&mdash;"),
            ("Episodes the guards declined", f"{_closure.get('blocked'):,}" if _closure.get("blocked") else "&mdash;"),
            ("Automated actions on risk-declined payments",
             f"{_v.get('risk_block_actions_world'):,}" if _v.get("risk_block_actions_world") is not None else "&mdash;"),
            ("Adversarial attacks survived",
             f"{_rt.get('survived')} of {_rt.get('attacks')}" if _rt.get("attacks") else "&mdash;"),
            ("LLM classification accuracy (shadow)", _fig(_sh.get("llm_accuracy"))),
            ("Unsafe actions the LLM proposed on RISK_BLOCK",
             str(_sh.get("unsafe_risk_block_actions")) if _sh.get("unsafe_risk_block_actions") is not None else "&mdash;"),
        ]
    )

    # The mark, from the one committed copy. Inlined rather than linked so that
    # out/report.html, which has no assets/ beside it, shows it too.
    _logo = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "assets" / "vasool-logo.svg").read_text()
    logo_svg = _logo.replace('role="img" aria-label="Vasool"', 'class="vasool-mark" aria-hidden="true"')
    favicon_href = "data:image/svg+xml," + urllib.parse.quote(" ".join(_logo.split()))

    html_content = _TEMPLATES.get_template("report.html.j2").render(
        favicon_href=favicon_href,
        noscript_rows=noscript_rows,
        evaluation_json=json.dumps(raw_data),
        shadow_json=json.dumps(shadow_data),
        redteam_json=json.dumps(redteam_data),
        holdout_json=json.dumps(holdout_data),
        chain_json=json.dumps(guard_chain()),
        logo_svg=logo_svg,
        hero_scope=hero_scope,
        airgap_svg=airgap_svg,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_content, encoding="utf-8")
    print(f"wrote {out_path} based on {json_path}", file=sys.stderr)

if __name__ == "__main__":
    base_dir = pathlib.Path(__file__).parent.parent
    eval_json = base_dir / "out" / "development" / "evaluation.json"
    out_html = base_dir / "out" / "report.html"
    build_report(eval_json, out_html)
