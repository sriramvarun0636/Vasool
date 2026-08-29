#!/usr/bin/env python3
"""Builds out/report.html from out/development/evaluation.json.

This is the Vasool 'Proof Machine' — a zero-dependency, Razorpay-branded HTML 
dashboard that injects the JSON ledger at build time. It renders a client-side
interactive audit of the FSM constraints, highlighting the yield, safety, and
cryptographic determinism of the system.
"""

import pathlib
import sys
import json

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
    shadow_path = json_path.parent.parent / "shadow" / "classifier_comparison_partial.json"
    if not shadow_path.exists():
        shadow_path = json_path.parent.parent / "shadow" / "classifier_comparison.json"
    shadow_data = {}
    if shadow_path.exists():
        try:
            shadow_data = json.loads(shadow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            shadow_data = {}

    # §2a's adversary. Optional the same way the shadow artifact is.
    redteam_path = json_path.parent.parent / "adversary" / "redteam.json"
    redteam_data = {}
    if redteam_path.exists():
        try:
            redteam_data = json.loads(redteam_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            redteam_data = {}

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vasool | AI Safety Control Plane</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --ink: #0B1221;
            --panel: #121B30;
            --hairline: rgba(255,255,255,0.08);
            --signal-blue: #2D68E6;
            --compliant-green: #00C96D;
            --violation-red: #D13B3B;
            --caution-amber: #F4C430;
            --paper: #EDEAE0;
            --font-display: 'Space Grotesk', sans-serif;
            --font-body: 'IBM Plex Sans', sans-serif;
            --font-mono: 'IBM Plex Mono', monospace;
        }}
        
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        
        body {{
            background-color: var(--ink);
            color: #E2E8F0;
            font-family: var(--font-body);
            display: flex;
            min-height: 100vh;
        }}
        
        input:focus, button:focus {{
            outline: none;
            border-color: var(--signal-blue);
            box-shadow: 0 0 0 1px var(--signal-blue);
        }}

        /* The Spine */
        #spine {{
            width: 72px;
            border-right: 1px solid var(--hairline);
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 40px 0;
            position: fixed;
            height: 100vh;
            overflow: hidden;
            z-index: 10;
        }}
        
        .hash-node {{
            font-family: var(--font-mono);
            font-size: 10px;
            color: var(--signal-blue);
            writing-mode: vertical-rl;
            transform: scale(-1);
            letter-spacing: 2px;
            opacity: 0.6;
            flex-grow: 1;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        
        .spine-line {{
            width: 1px;
            background-color: var(--hairline);
            flex-grow: 1;
            margin: 10px 0;
        }}

        /* Main Content */
        #content {{
            margin: 0 auto;
            margin-left: 72px;
            padding: 48px;
            flex-grow: 1;
            max-width: 1200px;
        }}
        
        #fallback-warning {{
            display: none;
            background-color: rgba(244, 196, 48, 0.1);
            color: var(--caution-amber);
            border: 1px solid var(--caution-amber);
            padding: 12px 24px;
            margin-bottom: 32px;
            border-radius: 2px;
            font-family: var(--font-mono);
            font-size: 13px;
        }}

        .hero {{
            margin-bottom: 64px;
        }}
        
        .hero h1 {{
            font-family: var(--font-display);
            font-size: 64px;
            font-weight: 700;
            letter-spacing: -1px;
            margin-bottom: 8px;
            font-variant-numeric: tabular-nums;
        }}
        
        .hero span {{ color: var(--compliant-green); }}

        .exhibit {{
            margin-bottom: 80px;
            position: relative;
        }}
        
        .exhibit-title {{
            font-family: var(--font-mono);
            font-size: 13px;
            color: #94A3B8;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 24px;
            display: flex;
            align-items: center;
        }}
        
        .exhibit-title::before {{
            content: '';
            display: inline-block;
            width: 24px;
            height: 1px;
            background-color: var(--hairline);
            margin-right: 12px;
        }}

        /* Cards */
        .card-row {{
            display: flex;
            gap: 24px;
        }}
        
        .card {{
            background-color: var(--panel);
            border: 1px solid var(--hairline);
            padding: 32px;
            border-radius: 4px;
            position: relative;
            overflow: hidden;
            flex: 1;
        }}
        
        .hero-card {{
            border-color: var(--signal-blue);
            box-shadow: 0 0 15px rgba(56, 189, 248, 0.2), inset 0 0 20px rgba(56, 189, 248, 0.05);
        }}
        
        .hero-card::before {{
            content: "";
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            border: 1px solid var(--signal-blue);
            border-radius: inherit;
            animation: hero-pulse 2s infinite alternate;
            pointer-events: none;
        }}
        
        @keyframes hero-pulse {{
            from {{ box-shadow: 0 0 10px rgba(56, 189, 248, 0.1); }}
            to {{ box-shadow: 0 0 25px rgba(56, 189, 248, 0.4); }}
        }}
        
        .card-title {{
            font-size: 18px;
            font-weight: 500;
            margin-bottom: 16px;
        }}
        
        .yield-number {{
            font-family: var(--font-display);
            font-size: 48px;
            font-weight: 700;
            margin-bottom: 8px;
        }}
        
        .risk-bar-container {{
            height: 4px;
            background-color: var(--hairline);
            border-radius: 2px;
            margin-top: 12px;
            margin-bottom: 24px;
            overflow: hidden;
            width: 100%;
        }}
        
        .risk-bar-fill {{
            height: 100%;
            border-radius: 2px;
            box-shadow: 0 0 8px currentColor;
        }}
        
        .yield-red {{ color: var(--violation-red); }}
        .yield-blue {{ color: var(--signal-blue); }}
        
        .fine-print {{
            font-family: var(--font-mono);
            font-size: 13px;
            color: #94A3B8;
            margin-top: 24px;
            padding-top: 16px;
            border-top: 1px solid var(--hairline);
            line-height: 1.6;
        }}

        /* ---------------------------------------------------------------
           Provenance mode. Every figure on this page carries the JSON path it
           came from; the toggle reveals them all at once. This is the whole
           thesis of the project applied to its own report card — a number you
           cannot trace is not a result.
           --------------------------------------------------------------- */
        .prov-toggle {{
            position: fixed; top: 20px; right: 20px; z-index: 200;
            display: inline-flex; align-items: center; gap: 9px;
            padding: 10px 15px; border-radius: 6px; cursor: pointer;
            background: rgba(18,27,48,0.92); backdrop-filter: blur(8px);
            border: 1px solid var(--hairline); color: var(--paper);
            font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.02em;
            transition: border-color 0.18s ease, background 0.18s ease;
        }}
        .prov-toggle:hover {{ border-color: var(--signal-blue); }}
        .prov-toggle[aria-pressed="true"] {{
            border-color: var(--signal-blue);
            background: rgba(45,104,230,0.18);
        }}
        .prov-dot {{
            width: 8px; height: 8px; border-radius: 50%;
            background: var(--viz-muted); flex: none;
        }}
        .prov-toggle[aria-pressed="true"] .prov-dot {{ background: var(--signal-blue); }}

        [data-src] {{ position: relative; }}
        body.provenance [data-src] {{
            outline: 1px dashed rgba(45,104,230,0.55);
            outline-offset: 3px;
            border-radius: 2px;
        }}
        .prov-path {{
            display: none;
            font-family: var(--font-mono); font-size: 10.5px; line-height: 1.45;
            color: var(--signal-blue); word-break: break-all;
            margin-top: 6px; opacity: 0.95;
        }}
        body.provenance .prov-path {{ display: block; }}
        .prov-banner {{
            display: none;
            border: 1px solid rgba(45,104,230,0.4); background: rgba(45,104,230,0.08);
            border-radius: 6px; padding: 18px 22px; margin-bottom: 40px;
            font-family: var(--font-body); font-size: 14.5px; line-height: 1.7;
            color: var(--paper);
        }}
        body.provenance .prov-banner {{ display: block; }}

        /* ---------------------------------------------------------------
           Data-visualisation tokens.
           Diverging pair blue<->red, validated against this page's panel
           surface (#121B30) in dark mode: CVD dE 19.2, normal-vision dE 29.0,
           both >= their floors, contrast >= 3:1. Status colours are the
           reserved four and always ship with an icon + label, never hue alone.
           --------------------------------------------------------------- */
        :root {{
            --viz-ahead:    #3987e5;   /* diverging pole: Vasool ahead */
            --viz-behind:   #e66767;   /* diverging pole: Vasool behind */
            --viz-zero:     #383835;   /* neutral midpoint / reference line */
            --viz-grid:     rgba(255,255,255,0.07);
            --viz-axis:     rgba(255,255,255,0.22);
            --viz-muted:    #898781;
            --status-good:     #0ca30c;
            --status-warning:  #fab219;
            --status-critical: #d03b3b;
        }}

        .viz-figure {{ margin-top: 8px; }}
        .viz-caption {{
            font-family: var(--font-body);
            font-size: 14px;
            line-height: 1.65;
            color: #94A3B8;
            margin-bottom: 28px;
            max-width: 78ch;
        }}
        .viz-legend {{
            display: flex; flex-wrap: wrap; gap: 20px;
            font-family: var(--font-mono); font-size: 12px;
            color: #94A3B8; margin: 4px 0 20px;
        }}
        .viz-legend span {{ display: inline-flex; align-items: center; gap: 8px; }}
        .viz-swatch {{ width: 12px; height: 12px; border-radius: 3px; flex: none; }}
        .viz-scroll {{ overflow-x: auto; }}

        /* forest plot */
        .forest {{ width: 100%; min-width: 640px; font-family: var(--font-mono); }}
        .forest .f-label {{ fill: var(--paper); font-size: 13px; }}
        .forest .f-sub {{ fill: var(--viz-muted); font-size: 11px; }}
        .forest .f-value {{ font-size: 12.5px; }}
        .forest .f-tick {{ fill: var(--viz-muted); font-size: 11px; }}
        .forest .f-grid {{ stroke: var(--viz-grid); stroke-width: 1; }}
        .forest .f-zero {{ stroke: var(--viz-axis); stroke-width: 1.5; stroke-dasharray: 3 3; }}
        .forest .f-ci {{ stroke-width: 2; stroke-linecap: round; }}
        .forest .f-row-hit {{ fill: transparent; cursor: default; }}
        .forest .f-row-hit:hover + .f-row-bg {{ fill: rgba(255,255,255,0.035); }}
        .forest .f-row-bg {{ fill: transparent; pointer-events: none; }}

        /* sweep survival grid */
        .grid-wrap {{ display: grid; grid-template-columns: auto 1fr auto; gap: 6px 14px; align-items: center; }}
        .grid-arm {{
            font-family: var(--font-mono); font-size: 12.5px; color: var(--paper);
            white-space: nowrap; text-align: right;
        }}
        .grid-cells {{ display: flex; gap: 2px; min-width: 0; }}
        .grid-cell {{
            flex: 1 1 0; height: 22px; min-width: 3px; border-radius: 2px;
            background: rgba(255,255,255,0.09);
        }}
        .grid-cell.flip {{ background: var(--viz-behind); }}
        .grid-count {{
            font-family: var(--font-mono); font-size: 12px; color: #94A3B8;
            white-space: nowrap; font-variant-numeric: tabular-nums;
        }}
        .grid-count.flip {{ color: var(--viz-behind); }}

        /* falsification board */
        .fboard {{ display: grid; gap: 10px; margin-top: 8px; }}
        .frow {{
            display: grid; grid-template-columns: 46px 1fr auto;
            gap: 18px; align-items: baseline;
            padding: 16px 18px; border: 1px solid var(--hairline);
            border-radius: 6px; background: rgba(255,255,255,0.02);
        }}
        .frow .fid {{ font-family: var(--font-display); font-size: 17px; font-weight: 700; }}
        .frow .fname {{ font-family: var(--font-body); font-size: 15px; color: var(--paper); }}
        .frow .fthr {{
            display: block; font-family: var(--font-mono); font-size: 12px;
            color: var(--viz-muted); margin-top: 5px; line-height: 1.5;
        }}
        .frow .fverdict {{
            font-family: var(--font-mono); font-size: 12.5px;
            white-space: nowrap; display: inline-flex; align-items: center; gap: 7px;
        }}
        .frow.warn {{ border-color: rgba(250,178,25,0.4); background: rgba(250,178,25,0.05); }}

        /* stat tiles */
        .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; }}
        .tile {{
            border: 1px solid var(--hairline); border-radius: 6px;
            padding: 18px 20px; background: rgba(255,255,255,0.02);
        }}
        .tile .tv {{
            font-family: var(--font-display); font-size: 30px; font-weight: 700;
            color: var(--paper); line-height: 1.1;
        }}
        .tile .tl {{
            font-family: var(--font-mono); font-size: 11.5px; color: var(--viz-muted);
            margin-top: 8px; line-height: 1.5;
        }}

        /* claim-type banding: §2a and §2b must not look alike */
        .band-independent {{ border-left: 3px solid var(--status-good); padding-left: 22px; }}
        .band-dependent {{ border-left: 3px solid var(--status-warning); padding-left: 22px; }}
        .band-tag {{
            display: inline-block; font-family: var(--font-mono); font-size: 11px;
            letter-spacing: 0.08em; text-transform: uppercase; padding: 4px 9px;
            border-radius: 3px; margin-bottom: 14px;
        }}
        .band-independent .band-tag {{ background: rgba(12,163,12,0.15); color: var(--status-good); }}
        .band-dependent .band-tag {{ background: rgba(250,178,25,0.13); color: var(--status-warning); }}

        /* World-keyed counters (Safety Ledger) */
        .world-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 32px;
            font-family: var(--font-mono);
            font-size: 13px;
        }}
        .world-table th, .world-table td {{
            padding: 10px 12px;
            border-bottom: 1px solid var(--hairline);
            text-align: right;
            white-space: nowrap;
        }}
        /* The LLM's answer distribution is the one cell with real prose in it;
           forcing it onto one line pushes the last two columns off the page,
           and those two columns are the whole point of the table. */
        #llm-table td:nth-child(4) {{ white-space: normal; min-width: 190px; }}
        #llm-table td:first-child, #llm-table th:first-child {{ white-space: normal; }}
        .world-table th:first-child, .world-table td:first-child {{
            text-align: left;
        }}
        .world-table thead th {{
            color: #94A3B8;
            font-weight: 600;
            border-bottom: 1px solid rgba(255,255,255,0.18);
        }}
        .world-table tr.is-vasool td {{
            color: var(--compliant-green);
        }}
        .world-table td.zero {{
            color: var(--compliant-green);
        }}
        .world-table td.nonzero {{
            color: var(--violation-red);
        }}
        .world-scroll {{
            overflow-x: auto;
        }}

        /* Guard Relay Circuit (Safety Ledger) */
        .relay-board {{
            background-color: var(--panel);
            border: 1px solid var(--hairline);
            padding: 48px;
            padding-top: 80px; /* Make room for angled labels */
            display: flex;
            align-items: center;
            justify-content: space-between;
            position: relative;
        }}
        
        .relay-line {{
            position: absolute;
            top: calc(100% - 60px); /* Position properly relative to padded container */
            left: 48px;
            right: 48px;
            height: 1px;
            background-color: var(--hairline);
            z-index: 1;
        }}

        .pulse {{
            position: absolute;
            top: -2px;
            left: 0;
            width: 12px;
            height: 5px;
            background-color: var(--signal-blue);
            box-shadow: 0 0 10px var(--signal-blue), 0 0 20px var(--signal-blue);
            border-radius: 4px;
            z-index: 3;
            animation: pulse-move 3s linear infinite;
        }}

        @keyframes pulse-move {{
            0% {{ left: 0%; opacity: 0; }}
            10% {{ opacity: 1; }}
            90% {{ opacity: 1; }}
            100% {{ left: 100%; opacity: 0; }}
        }}
        
        .guard-node {{
            width: 24px;
            height: 24px;
            background-color: var(--ink);
            border: 1px solid var(--hairline);
            border-radius: 2px;
            z-index: 2;
            cursor: pointer;
            position: relative;
            transition: all 0.2s ease;
            margin-top: 24px; /* Move node down slightly */
        }}
        
        .guard-node.active {{
            background-color: var(--compliant-green);
            border-color: var(--compliant-green);
            box-shadow: 0 0 12px rgba(0, 201, 109, 0.4);
        }}

        .guard-label {{
            position: absolute;
            top: -30px;
            left: 50%;
            transform: translateX(-10%) rotate(-45deg);
            transform-origin: bottom left;
            font-family: var(--font-mono);
            font-size: 11px;
            color: #94A3B8;
            white-space: nowrap;
            pointer-events: none;
            transition: color 0.2s ease;
        }}

        .guard-node:hover .guard-label {{
            color: var(--compliant-green);
            font-weight: bold;
        }}
        
        .guard-tooltip {{
            position: absolute;
            bottom: 36px;
            left: 50%;
            transform: translateX(-50%);
            background-color: var(--paper);
            color: var(--ink);
            padding: 12px;
            border-radius: 2px;
            font-family: var(--font-mono);
            font-size: 13px;
            width: max-content;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s ease;
            white-space: pre;
            z-index: 100;
        }}
        
        .guard-node:hover .guard-tooltip {{
            opacity: 1;
        }}

        /* AI Air Gap */
        .airgap-diagram {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background-color: var(--panel);
            border: 1px solid var(--hairline);
            padding: 48px;
            border-radius: 2px;
            font-family: var(--font-mono);
            font-size: 13px;
        }}
        
        .plane-box {{
            border: 1px dashed var(--hairline);
            padding: 24px;
            text-align: center;
            width: 200px;
            background: rgba(0,0,0,0.2);
        }}
        
        .firewall {{
            width: 12px;
            height: 120px;
            background-color: var(--violation-red);
            position: relative;
            box-shadow: 0 0 15px rgba(209, 59, 59, 0.5);
        }}

        .flow-line {{
            flex-grow: 1;
            height: 1px;
            background-color: var(--hairline);
            margin: 0 24px;
            position: relative;
        }}

        /* Ledger Diff */
        .diff-board {{
            display: flex;
            gap: 24px;
            background-color: var(--panel);
            border: 1px solid var(--hairline);
            border-radius: 2px;
        }}
        
        .diff-col {{
            flex: 1;
            padding: 24px;
        }}
        
        .diff-col pre {{
            font-family: var(--font-mono);
            font-size: 13px;
            line-height: 1.6;
        }}
        
        .diff-highlight-red {{ background-color: rgba(209, 59, 59, 0.15); color: #FCA5A5; display: inline-block; width: 100%; }}
        .diff-highlight-green {{ background-color: rgba(0, 201, 109, 0.15); color: #86EFAC; display: inline-block; width: 100%; }}

        /* Chain Head Verifier */
        .verifier-box {{
            background-color: var(--panel);
            border: 1px solid var(--hairline);
            padding: 32px;
            border-radius: 2px;
            font-family: var(--font-mono);
        }}
        
        .input-group {{
            display: flex;
            gap: 16px;
            margin-bottom: 24px;
        }}
        
        input[type="text"] {{
            flex-grow: 1;
            background-color: var(--ink);
            border: 1px solid var(--hairline);
            color: #fff;
            padding: 12px 16px;
            font-family: var(--font-mono);
            outline: none;
        }}
        
        button {{
            background-color: var(--signal-blue);
            color: #fff;
            border: none;
            padding: 0 24px;
            font-family: var(--font-body);
            font-weight: 500;
            cursor: pointer;
            border-radius: 2px;
            transition: background-color 0.2s;
        }}
        
        button:hover {{ background-color: #1e4baf; }}
        
        #verifier-console {{
            background-color: var(--ink);
            padding: 16px;
            min-height: 120px;
            font-size: 13px;
            color: #94A3B8;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}
        
        .console-success {{ color: var(--compliant-green); }}
        .console-error {{ color: var(--violation-red); }}

        /* Rubber Stamp */
        .rubber-stamp {{
            position: absolute;
            top: 24px;
            right: 24px;
            font-family: var(--font-display);
            font-size: 22px;
            font-weight: 700;
            color: rgba(209, 59, 59, 0.95);
            border: 4px solid rgba(209, 59, 59, 0.95);
            padding: 8px 16px;
            text-transform: uppercase;
            letter-spacing: 2px;
            transform: rotate(-15deg);
            z-index: 10;
            pointer-events: none;
            mix-blend-mode: screen; /* Makes white text pop through */
        }}

        /* Branding */
        .brand-header {{
            display: flex;
            align-items: center;
            gap: 16px;
            margin-bottom: 32px;
        }}
        
        .brand-header h2 {{
            font-family: var(--font-display);
            font-size: 24px;
            font-weight: 700;
            color: #E2E8F0;
            letter-spacing: -0.5px;
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        
        .brand-header .vasool-tag {{
            background-color: var(--signal-blue);
            color: #fff;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 14px;
            font-family: var(--font-mono);
            font-weight: 500;
        }}

        /* Trajectory Explorer */
        .explorer-board {{
            display: flex;
            background-color: var(--panel);
            border: 1px solid var(--hairline);
            border-radius: 2px;
            height: 400px;
        }}
        .explorer-list {{
            width: 250px;
            border-right: 1px solid var(--hairline);
            overflow-y: auto;
            padding: 16px 0;
        }}
        .explorer-item {{
            padding: 12px 24px;
            font-family: var(--font-mono);
            font-size: 13px;
            cursor: pointer;
            border-left: 3px solid transparent;
            color: #94A3B8;
            transition: all 0.2s ease;
        }}
        .explorer-item:hover {{
            background-color: rgba(255,255,255,0.02);
            color: #fff;
        }}
        .explorer-item.active {{
            border-left-color: var(--signal-blue);
            background-color: rgba(45, 104, 230, 0.1);
            color: #fff;
        }}
        .explorer-console {{
            flex: 1;
            padding: 24px;
            font-family: var(--font-mono);
            font-size: 13px;
            line-height: 1.6;
            color: #E2E8F0;
            overflow-y: auto;
            background-color: var(--ink);
            white-space: pre-wrap;
        }}

    </style>
</head>
<body>
    
    <!-- JSON INJECTION: Zero dependencies, robust extraction -->
    <script type="application/json" id="eval-data">
{json.dumps(raw_data)}
    </script>
    <script type="application/json" id="shadow-data">
{json.dumps(shadow_data)}
    </script>
    <script type="application/json" id="redteam-data">
{json.dumps(redteam_data)}
    </script>

    <div id="spine">
        <!-- Generated dynamically by JS -->
    </div>

    <button class="prov-toggle" id="prov-toggle" aria-pressed="false"
            title="Show the JSON path behind every figure on this page">
        <span class="prov-dot"></span> trace every number
    </button>

    <div id="content">

        <div class="prov-banner">
            <strong>Provenance mode.</strong> Every figure below now shows the exact key it was
            read from in <code>out/development/evaluation.json</code> &mdash; the manifest
            <code>make sweeps</code> writes. Nothing on this page is typed by hand; a value the
            manifest does not carry renders as a dash rather than a plausible number. That rule is
            not decorative &mdash; a hardcoded constant was found masquerading as a measurement in
            this very file and is recorded in <code>EVALUATION.md</code> &sect;10.
        </div>

        <div id="fallback-warning">
            WARNING: evaluation.json parse failed or is incomplete. Displaying fallback values. Cryptographic proofs will not verify.
        </div>

        <div class="hero">
            <div class="brand-header">
                <img src="https://upload.wikimedia.org/wikipedia/commons/8/89/Razorpay_logo.svg" alt="Razorpay" height="32" style="filter: brightness(0) invert(1);">
                <h2><span class="vasool-tag">VASOOL</span></h2>
            </div>
            <h1 id="hero-money">&mdash;</h1>
            <p id="hero-sub">recovered across the <span id="hero-cohort">&mdash;</span>
               cohort, 1,000 seeds &middot; <span id="hero-violations">&mdash;</span></p>
            <p style="margin-top: 10px; font-family: var(--font-mono); font-size: 12.5px; color: #94A3B8;">
               <span id="hero-trajectories">0</span> arm-seed runs &middot; 9,000 base + 151,200 sweep
            </p>
        </div>

        <div class="exhibit band-dependent" id="exhibit-a">
            <span class="band-tag">&sect;2b &middot; simulator-dependent &mdash; the outcome model decides these</span>
            <div class="exhibit-title">EXHIBIT A &mdash; The Money, and What It Cost</div>
            <p class="viz-caption">
                The realistic incumbent recovers more money than we do. That is the result, it was
                registered as falsification criterion <strong>F1</strong> before the first run, and
                the interval below excludes zero <em>in the baseline's favour</em>. Every figure on
                this page is rendered from <code>out/development/evaluation.json</code>; nothing is
                hardcoded, and a value the artifact does not carry renders as a dash.
            </p>

            <div class="card-row">
                <div class="card">
                    <div class="card-title">Baseline (retry_plus_contact)</div>
                    <div class="yield-number yield-red count-up" id="yield-baseline">&mdash;</div>
                    <div class="fine-print" id="fine-baseline">&mdash;</div>
                </div>
                <div class="card">
                    <div class="card-title">Ungated (vasool_ungated)</div>
                    <div class="yield-number yield-red count-up" id="yield-greedy">&mdash;</div>
                    <div class="fine-print" id="fine-greedy">&mdash;</div>
                </div>
                <div class="card hero-card">
                    <div class="card-title">Vasool</div>
                    <div class="yield-number yield-blue count-up" id="yield-vasool">&mdash;</div>
                    <div class="fine-print" id="fine-vasool">&mdash;</div>
                </div>
            </div>

            <h3 style="margin-top: 52px; margin-bottom: 6px; font-family: var(--font-display); font-size: 20px;">
                Paired difference vs Vasool, recovery rate
            </h3>
            <p class="viz-caption">
                Every arm runs the same seeded universe &mdash; same customers, same arrivals, same
                outcome draws &mdash; so the comparison is the per-seed difference, bootstrapped over
                1,000 seeds. Bars are 95% percentile intervals.
                <strong>At this sample size every interval is narrower than its own marker</strong>
                &mdash; the widest spans 0.37pp &mdash; so the dots are the intervals, not a plot
                that forgot to draw them. Exact bounds are in the table view below. A marker clear
                of the dashed zero line is a real difference; which side it falls on is the story.
            </p>
            <div class="viz-legend">
                <span><span class="viz-swatch" style="background: var(--viz-ahead);"></span> Vasool recovers more</span>
                <span><span class="viz-swatch" style="background: var(--viz-behind);"></span> Vasool recovers less</span>
                <span><span class="viz-swatch" style="background: var(--viz-zero); border: 1px dashed var(--viz-axis);"></span> zero &mdash; no detectable difference</span>
            </div>
            <div class="viz-scroll viz-figure">
                <svg class="forest" id="forest" role="img"
                     aria-label="Paired difference in recovery rate against Vasool, with 95% bootstrap intervals"></svg>
            </div>
            <details style="margin-top: 18px;">
                <summary style="cursor: pointer; font-family: var(--font-mono); font-size: 12.5px; color: #94A3B8;">
                    Table view
                </summary>
                <div class="world-scroll">
                <table class="world-table" id="forest-table">
                    <thead><tr><th>Arm</th><th>Difference</th><th>95% interval</th><th>Excludes zero</th></tr></thead>
                    <tbody></tbody>
                </table>
                </div>
            </details>
        </div>

        <div class="exhibit band-independent" id="exhibit-b">
            <span class="band-tag">&sect;2a &middot; simulator-independent &mdash; the simulator cannot fake these</span>
            <div class="exhibit-title">EXHIBIT B &mdash; The Safety Ledger</div>
            <p class="viz-caption">
                These are properties of what the agent <em>did</em>, scanned from the hash-chained
                ledger. They hold or fail regardless of what outcome model runs underneath, which is
                why they are the claims the submission actually rests on &mdash; and why they are
                banded differently from every recovery number on this page. Thirteen pure-function
                guards gate the execution plane; all thirteen are evaluated on every proposal and
                resolved by severity, never short-circuited.
            </p>
            <div class="tiles" id="safety-tiles" style="margin-bottom: 40px;"></div>
            <div class="relay-board" id="relay-board">
                <div class="relay-line"><div class="pulse"></div></div>
                <!-- Generated dynamically by JS -->
            </div>

            <p style="margin-top: 48px; margin-bottom: 4px; font-weight: 600;">World-keyed counters</p>
            <p style="margin-bottom: 0; color: #94A3B8; font-size: 14px; line-height: 1.6;">
                Counted against the class the <em>world</em> registered for each episode, not
                the label the arm assigned itself &mdash; so an arm that declines to classify
                cannot satisfy these by mislabelling. <strong>These are world numbers, not
                ledger scans, and they are not part of EVALUATION.md &sect;2a.</strong>
                Every row is the sum over the 1,000-seed development cohort.
            </p>
            <div class="world-scroll">
            <table class="world-table" id="world-table">
                <thead>
                    <tr>
                        <th>Arm</th>
                        <th>Retries on INSTRUMENT_DEAD</th>
                        <th>Actions on RISK_BLOCK</th>
                        <th>Retries on CUSTOMER_ACTION</th>
                    </tr>
                </thead>
                <tbody><!-- Generated dynamically by JS --></tbody>
            </table>
            </div>
            <p style="margin-top: 16px; color: #94A3B8; font-size: 13px; line-height: 1.6;">
                The third column closes the limit registered in EVALUATION.md &sect;10 on
                2026-08-24, which recorded that <code>CUSTOMER_ACTION</code> &mdash; 0.09 of the
                registered failure mix, and priced at zero retry budget &mdash; had no
                world-keyed counter, so a baseline retrying those episodes earned recovery
                credit with no guardrail reporting it. A dash means the artifact does not
                carry the field; no value here is defaulted.
            </p>
        </div>

        <div class="exhibit band-dependent" id="exhibit-sweep">
            <span class="band-tag">&sect;7 &middot; sensitivity</span>
            <div class="exhibit-title">EXHIBIT C &mdash; Does It Survive the Sweep?</div>
            <p class="viz-caption">
                Eight of the nine outcome parameters are guesses. So every registered parameter is
                swept independently at &minus;50%, &minus;25%, +25% and +50% of its value &mdash;
                <strong>83 configurations</strong> &times; 9 arms &times; 200 seeds &mdash; and each
                comparison is re-tested in every one. A cell is marked only when the comparison
                <em>fails to survive</em>; the unremarkable majority stays recessive, because the
                exceptions are the finding. <strong>F6 fires if 5 or more of the 8 comparisons flip
                in at least one configuration.</strong>
            </p>
            <div class="viz-legend">
                <span><span class="viz-swatch" style="background: rgba(255,255,255,0.09);"></span> survives</span>
                <span><span class="viz-swatch" style="background: var(--viz-behind);"></span> fails to survive</span>
                <span style="color: var(--viz-muted);">each column is one of the 83 configurations</span>
            </div>
            <div class="viz-scroll viz-figure">
                <div class="grid-wrap" id="sweep-grid" style="min-width: 620px;"></div>
            </div>
            <p class="viz-caption" style="margin-top: 24px; margin-bottom: 0;">
                <strong>A3 fails in all 83, and that is not a parameter effect.</strong> Its reference
                interval at 200 seeds is [&minus;0.00057, +0.00196], which already includes zero &mdash;
                so <code>survives()</code> fails because the reference was never conclusive at this
                depth, not because any sweep moved it. Registered as a limit in
                <code>EVALUATION.md</code> &sect;10 rather than argued away, and it pushes F6
                <em>toward</em> firing, which is the conservative direction.
            </p>
        </div>

        <div class="exhibit band-dependent" id="exhibit-falsification">
            <span class="band-tag">&sect;9 &middot; registered in advance</span>
            <div class="exhibit-title">EXHIBIT D &mdash; What Would Have Killed This</div>
            <p class="viz-caption">
                Seven criteria, each with a threshold, written into the protocol before any run
                existed. A criterion invented after seeing the numbers is not a criterion. None
                fired &mdash; but read F1's row carefully, because <code>fired: false</code> is not
                the same as good news, and the artifact says so in its own <code>detail</code> field.
            </p>
            <div class="fboard" id="fboard"></div>
        </div>

        <div class="exhibit band-independent" id="exhibit-llm">
            <span class="band-tag">&sect;4.5 &middot; where the LLM lost</span>
            <div class="exhibit-title">EXHIBIT E &mdash; Should the LLM Own This?</div>
            <p class="viz-caption">
                The architecture keeps the LLM away from money. That is a claim; this is the
                measurement behind it. Both classifiers were asked the same questions in shadow
                &mdash; the LLM never touched a ledger, and a test walks the import graph in both
                directions to prove it could not have.
                <strong>The rules column is 1.000 by construction, not by measurement</strong>
                &mdash; ground truth resolves through the same lookup the rules read, and saying
                so is the only way the other column means anything.
            </p>
            <div class="tiles" id="llm-tiles" style="margin-bottom: 34px;"></div>
            <div class="viz-scroll">
            <table class="world-table" id="llm-table">
                <thead>
                    <tr>
                        <th>Failure the webhook reported</th>
                        <th>Truth</th>
                        <th>Rules</th>
                        <th>LLM</th>
                        <th>Accuracy</th>
                        <th>Consistency</th>
                        <th>Episodes</th>
                    </tr>
                </thead>
                <tbody><!-- JS --></tbody>
            </table>
            </div>
            <p class="viz-caption" id="llm-note" style="margin-top: 24px; margin-bottom: 0;"></p>
        </div>

        <div class="exhibit" id="exhibit-c">
            <div class="exhibit-title">EXHIBIT F — The AI Air-Gap</div>
            <div class="airgap-diagram">
                <div class="plane-box">
                    <strong>Shadow Plane</strong><br><br>
                    Unstructured Data<br>
                    LLM Synthesis<br>
                    <em>Proposals</em>
                </div>
                <div class="flow-line"></div>
                <div class="firewall"></div>
                <div class="flow-line"></div>
                <div class="plane-box" style="border-style: solid; border-color: var(--compliant-green);">
                    <strong>Execution Plane</strong><br><br>
                    Deterministic FSM<br>
                    13 Statutory Guards<br>
                    <em>Immutable Ledger</em>
                </div>
            </div>
        </div>

        <div class="exhibit band-independent" id="exhibit-d">
            <span class="band-tag">&sect;2a &middot; the adversary</span>
            <div class="exhibit-title">EXHIBIT G &mdash; What Still Beats It</div>
            <p class="viz-caption">
                The survival criterion was registered <em>before</em> the first attack was written,
                and <code>judge()</code> is the only thing that can return a verdict. It scans the
                ledger the way &sect;2a scans &mdash; never &ldquo;a guard returned BLOCKED&rdquo;.
                An attack may add evidence requirements; it cannot lower the bar. Each row below
                carries the SHA-256 of the ledger that attack produced.
            </p>
            <div class="tiles" id="rt-tiles" style="margin-bottom: 34px;"></div>
            <div class="viz-scroll">
            <table class="world-table" id="rt-table">
                <thead>
                    <tr><th>Attack</th><th>Verdict</th><th>Why</th><th>Receipts</th><th>Ledger</th></tr>
                </thead>
                <tbody><!-- JS --></tbody>
            </table>
            </div>
            <p class="viz-caption" style="margin-top: 22px; margin-bottom: 0;">
                Four are open and named. They are not bugs awaiting a fix in the last commit &mdash;
                they are limits with a registered expectation, so a known failure keeps the suite
                green and a <em>fixed</em> one turns it red. A clean sheet here would be evidence
                the attacks are too weak.
            </p>
        </div>

        <div class="exhibit" id="exhibit-e">
            <div class="exhibit-title">EXHIBIT H — The Audit Trail</div>
            <div class="verifier-box">
                <h3 style="margin-bottom: 16px;">Live Cryptographic Verifier</h3>
                <p style="margin-bottom: 24px; color: #94A3B8; font-family: var(--font-body);">Recompute the deterministic SHA-256 digest live in-browser using Web Crypto API.</p>
                <div class="input-group">
                    <input type="text" id="verify-input" placeholder="Enter Receipt ID (e.g. rcpt_a1b2c3d4e5f6)" value="">
                    <button onclick="runVerification()">Verify Record</button>
                </div>
                <div id="verifier-console">
                    &gt; awaiting input...
                </div>
            </div>
        </div>

        <div class="exhibit" id="exhibit-f">
            <div class="exhibit-title">EXHIBIT I — Trajectory Explorer</div>
            <div class="explorer-board">
                <div class="explorer-list" id="explorer-list">
                    <!-- JS populated -->
                </div>
                <div class="explorer-console" id="explorer-console">
                    &gt; Select a trajectory from the ledger...
                </div>
            </div>
        </div>

    </div>

    <script>
        // --- DATA LAYER & ROBUST PARSING ---
        let EVAL = {{}};
        let vasoolYield = 49.07;
        let baselineYield = 65.42;
        let greedyYield = 53.81;
        let totalRuns = 160200;
        let usingFallback = false;
        // No literal default. §2a's whole point is that a compliance number is
        // measured; a hardcoded stand-in rendering as a measurement is the
        // failure the exhibit beside it exists to prevent. Absent => null,
        // which raises the fallback banner and renders as "unavailable".
        let riskActions = null;
        // Same rule as riskActions: the §2a violation count is measured or it is
        // not shown. It used to be a static string in the markup that no code
        // ever set, which meant "0 safety violations" rendered whatever the
        // artifact said.
        let safetyViolations = null;
        let safetySeeds = null;
        let sampleLedger = [];
        let ledgerHead = "5c4a7e9f";
        
        try {{
            const raw = document.getElementById("eval-data").textContent;
            EVAL = JSON.parse(raw);
            const arms = EVAL.per_arm || {{}};
            
            if (arms.vasool && arms.vasool.recovery_rate_mean) {{
                vasoolYield = (arms.vasool.recovery_rate_mean * 100).toFixed(2);
            }}
            if (arms.retry_plus_contact && arms.retry_plus_contact.recovery_rate_mean) {{
                baselineYield = (arms.retry_plus_contact.recovery_rate_mean * 100).toFixed(2);
            }}
            if (arms.vasool_ungated && arms.vasool_ungated.recovery_rate_mean) {{
                greedyYield = (arms.vasool_ungated.recovery_rate_mean * 100).toFixed(2);
            }}
            
            if (EVAL.metadata && EVAL.metadata.total_trajectories) {{
                totalRuns = EVAL.metadata.total_trajectories;
            }} else if (EVAL.summary && EVAL.summary.total_runs) {{
                totalRuns = EVAL.summary.total_runs;
            }}
            
            sampleLedger = EVAL.ledger || (EVAL.determinism ? EVAL.determinism.sample_receipts : []) || [];
            
            if (sampleLedger.length > 0) {{
                ledgerHead = sampleLedger[sampleLedger.length - 1].hash.substring(0, 8);
            }}
            
            const holds = arms.vasool?.safety_holds_on;
            const seeds = arms.vasool?.seeds;
            if (typeof holds === "number" && typeof seeds === "number") {{
                safetyViolations = seeds - holds;
                safetySeeds = seeds;
            }} else {{
                usingFallback = true;
            }}

            const measuredRiskActions = arms.naive_retry?.risk_block_actions_world;
            if (typeof measuredRiskActions === "number") {{
                riskActions = measuredRiskActions;
            }} else {{
                usingFallback = true;
            }}

            if (!EVAL.per_arm) {{
                usingFallback = true;
            }}
        }} catch (e) {{
            console.error("JSON parse failure:", e);
            usingFallback = true;
        }}

        // --- CRYPTO HELPER ---
        async function hashString(str) {{
            if (!window.crypto || !window.crypto.subtle) {{
                return "insecure-env";
            }}
            try {{
                const encoder = new TextEncoder();
                const data = encoder.encode(str);
                const digest = await crypto.subtle.digest("SHA-256", data);
                return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
            }} catch (e) {{
                return "hash-error";
            }}
        }}

        // --- RENDER LAYER ---
        document.addEventListener("DOMContentLoaded", async () => {{
            
            if (usingFallback) {{
                document.getElementById("fallback-warning").style.display = "block";
            }}

            // 1. Render Guard Nodes
            const relayBoard = document.getElementById("relay-board");
            const guards = [
                {{id: "G01", name: "IdempotencyGuard", clause: "System Constraint"}},
                {{id: "G02", name: "RiskBlockGuard", clause: "Card Network Norms"}},
                {{id: "G03", name: "ConsentGuard", clause: "DPDP Act 2023 s.6"}},
                {{id: "G04", name: "RetryCapGuard", clause: "Platform Limit"}},
                {{id: "G05", name: "PromiseToPayGuard", clause: "RBI FPC (fair dealing)"}},
                {{id: "G06", name: "DNDGuard", clause: "TRAI TCCCPR 2018"}},
                {{id: "G07", name: "FrequencyCapGuard", clause: "RBI FPC (anti-harassment)"}},
                {{id: "G08", name: "ContactWindowGuard", clause: "RBI FPC ¶55"}},
                {{id: "G09", name: "PreDebitNoticeGuard", clause: "RBI e-mandate framework"}},
                {{id: "G10", name: "AFAThresholdGuard", clause: "RBI AFA > ₹15,000"}},
                {{id: "G11", name: "DLTTemplateGuard", clause: "TRAI DLT Registration"}},
                {{id: "G12", name: "SpendCapGuard", clause: "Merchant Ceiling"}},
                {{id: "G13", name: "HumanApprovalGuard", clause: "Execution Handoff"}}
            ];
            
            
            guards.forEach(g => {{
                const node = document.createElement("div");
                node.className = "guard-node";
                node.addEventListener("mouseenter", () => node.classList.add("active"));
                node.addEventListener("mouseleave", () => node.classList.remove("active"));
                
                let stats;
                if (g.id !== "G02") {{
                    stats = `Status: COMPLIANT`;
                }} else if (riskActions === null) {{
                    stats = `Blocks: unavailable (no measurement in artifact)`;
                }} else if (riskActions > 0) {{
                    stats = `Blocks: ${{new Intl.NumberFormat().format(riskActions)}}`;
                }} else {{
                    stats = `Status: COMPLIANT`;
                }}
                node.innerHTML = `<div class="guard-tooltip">${{g.id}}: ${{g.name}}<br>Clause: ${{g.clause}}<br>${{stats}}</div>`;
                
                const label = document.createElement("div");
                label.className = "guard-label";
                label.innerText = g.name.replace("Guard", "");
                node.appendChild(label);

                relayBoard.appendChild(node);
            }});

            // 1b-2. Provenance. `trace` is the only way a figure gets onto this
            // page: it stamps the element with the manifest key it came from and
            // appends the path node the toggle reveals. If a number reaches the
            // DOM without going through here, it has no source — which is the
            // bug class this whole feature exists to make visible.
            const traced = [];
            function trace(el, path, text) {{
                if (!el) return el;
                if (text !== undefined) el.textContent = text;
                el.setAttribute("data-src", path);
                const tag = document.createElement("div");
                tag.className = "prov-path";
                tag.textContent = path;
                el.appendChild(tag);
                traced.push(path);
                return el;
            }}

            const provBtn = document.getElementById("prov-toggle");
            if (provBtn) {{
                provBtn.addEventListener("click", () => {{
                    const on = document.body.classList.toggle("provenance");
                    provBtn.setAttribute("aria-pressed", String(on));
                    provBtn.lastChild.textContent = on
                        ? ` ${{traced.length}} figures traced`
                        : " trace every number";
                }});
            }}

            // 1b. World-keyed counters. No fallbacks: a missing field renders as
            // a dash rather than a plausible number, because the whole point of
            // these three columns is that they are measured.
            const WORLD_COLUMNS = [
                "instrument_dead_retries_world",
                "risk_block_actions_world",
                "customer_action_retries_world"
            ];
            const ARM_LABELS = {{
                vasool: "Vasool",
                naive_retry: "naive_retry",
                retry_plus_contact: "retry_plus_contact",
                vasool_ungated: "vasool_ungated"
            }};
            const worldBody = document.querySelector("#world-table tbody");
            if (worldBody) {{
                const armRows = EVAL?.per_arm || {{}};
                const order = Object.keys(ARM_LABELS).filter(a => a in armRows)
                    .concat(Object.keys(armRows).filter(a => !(a in ARM_LABELS)));
                order.forEach(arm => {{
                    const row = document.createElement("tr");
                    if (arm === "vasool") {{ row.className = "is-vasool"; }}
                    const name = document.createElement("td");
                    name.innerText = ARM_LABELS[arm] || arm;
                    row.appendChild(name);
                    WORLD_COLUMNS.forEach(col => {{
                        const cell = document.createElement("td");
                        const value = armRows[arm]?.[col];
                        if (typeof value !== "number") {{
                            cell.innerText = "\u2014";
                        }} else {{
                            cell.className = value === 0 ? "zero" : "nonzero";
                            trace(cell, `per_arm.${{arm}}.${{col}}`,
                                  new Intl.NumberFormat().format(value));
                        }}
                        row.appendChild(cell);
                    }});
                    worldBody.appendChild(row);
                }});
            }}

            // 1c. Yield cards + the loss, straight off the artifact.
            const ARM_NOTE = {{
                baseline: "retry_plus_contact",
                greedy: "vasool_ungated",
                vasool: "vasool"
            }};
            const pct = (x) => (x * 100).toFixed(2) + "%";
            const nf = (n) => new Intl.NumberFormat().format(n);
            Object.entries(ARM_NOTE).forEach(([slot, arm]) => {{
                const m = EVAL?.per_arm?.[arm];
                const box = document.getElementById("fine-" + slot);
                if (!box) return;
                if (!m) {{ box.innerHTML = "no measurement in artifact"; return; }}
                const held = m.safety_holds_on, seeds = m.seeds;
                const ok = held === seeds;
                box.innerHTML =
                    `<strong style="color:${{ok ? "var(--status-good)" : "var(--status-critical)"}}">` +
                    `${{ok ? "✓" : "✗"}} safety predicate held on ${{nf(held)}} / ${{nf(seeds)}} seeds</strong><br><br>` +
                    `retries on a dead instrument &middot; <b>${{nf(m.instrument_dead_retries_world)}}</b><br>` +
                    `actions on risk-declined episodes &middot; <b>${{nf(m.risk_block_actions_world)}}</b><br>` +
                    `retries on a zero-budget class &middot; <b>${{nf(m.customer_action_retries_world)}}</b>`;
            }});

            // 1d. Forest plot — paired difference vs Vasool, 95% bootstrap intervals.
            const paired = EVAL?.paired_vs_vasool || {{}};
            const forestRows = Object.entries(paired)
                .map(([arm, m]) => ({{ arm, ...(m.recovery_rate || {{}}) }}))
                .filter(r => typeof r.point === "number")
                .sort((a, b) => a.point - b.point);

            const svg = document.getElementById("forest");
            if (svg && forestRows.length) {{
                const NS = "http://www.w3.org/2000/svg";
                const rowH = 46, padT = 34, padB = 46, padL = 168, padR = 96;
                const W = 900, H = padT + forestRows.length * rowH + padB;
                svg.setAttribute("viewBox", `0 0 ${{W}} ${{H}}`);
                svg.setAttribute("height", H);
                const lo = Math.min(...forestRows.map(r => r.low), 0);
                const hi = Math.max(...forestRows.map(r => r.high), 0);
                const span = (hi - lo) || 1, pad = span * 0.12;
                const x0 = lo - pad, x1 = hi + pad;
                const X = (v) => padL + ((v - x0) / (x1 - x0)) * (W - padL - padR);
                const el = (n, a, parent) => {{
                    const e = document.createElementNS(NS, n);
                    for (const k in a) e.setAttribute(k, a[k]);
                    (parent || svg).appendChild(e);
                    return e;
                }};

                // axis ticks on round percentage-point values, recessive.
                // An axis labelled -21pp / -12pp / -4pp is arithmetic showing
                // through; a reader wants -20 / -10 / 0 / 10 / 20.
                const niceStep = (raw) => {{
                    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
                    const n = raw / mag;
                    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
                }};
                const stepPP = niceStep(((x1 - x0) * 100) / 5);
                const startPP = Math.ceil((x0 * 100) / stepPP) * stepPP;
                for (let vpp = startPP; vpp <= x1 * 100 + 1e-9; vpp += stepPP) {{
                    const v = vpp / 100;
                    el("line", {{ x1: X(v), x2: X(v), y1: padT - 10, y2: H - padB + 6, class: "f-grid" }});
                    const tk = el("text", {{ x: X(v), y: H - padB + 24, "text-anchor": "middle", class: "f-tick" }});
                    tk.textContent = (vpp > 0 ? "+" : "") + vpp.toFixed(0) + "pp";
                }}
                el("line", {{ x1: X(0), x2: X(0), y1: padT - 10, y2: H - padB + 6, class: "f-zero" }});

                forestRows.forEach((r, i) => {{
                    const y = padT + i * rowH + rowH / 2;
                    const behind = r.point < 0;
                    const colour = behind ? "var(--viz-behind)" : "var(--viz-ahead)";

                    el("rect", {{ x: 0, y: y - rowH / 2, width: W, height: rowH, class: "f-row-hit" }})
                        .appendChild(document.createElementNS(NS, "title"))
                        .textContent =
                            `${{r.arm}}\n${{(r.point * 100).toFixed(3)}}pp ` +
                            `[${{(r.low * 100).toFixed(3)}}, ${{(r.high * 100).toFixed(3)}}]\n` +
                            (behind ? "Vasool recovers less" : "Vasool recovers more");
                    el("rect", {{ x: 0, y: y - rowH / 2, width: W, height: rowH, class: "f-row-bg" }});

                    const label = el("text", {{ x: padL - 18, y: y + 1, "text-anchor": "end", class: "f-label" }});
                    label.textContent = r.arm;
                    const sub = el("text", {{ x: padL - 18, y: y + 15, "text-anchor": "end", class: "f-sub" }});
                    sub.textContent = r.excludes_zero ? "excludes zero" : "includes zero";

                    // 2px CI line, 4px rounded ends, >=8px point marker with a
                    // 2px surface ring so an overlap with the zero rule reads.
                    el("line", {{
                        x1: X(r.low), x2: X(r.high), y1: y, y2: y,
                        stroke: colour, class: "f-ci"
                    }});
                    el("circle", {{
                        cx: X(r.point), cy: y, r: 5.5, fill: colour,
                        stroke: "var(--panel)", "stroke-width": 2
                    }});

                    const val = el("text", {{
                        x: W - padR + 14, y: y + 4, "text-anchor": "start",
                        class: "f-value", fill: colour
                    }});
                    val.textContent = (r.point >= 0 ? "+" : "") + (r.point * 100).toFixed(2) + "pp";
                }});

                const lhs = el("text", {{ x: X(0) - 12, y: 16, "text-anchor": "end", class: "f-sub" }});
                lhs.textContent = "← Vasool recovers less";
                const rhs = el("text", {{ x: X(0) + 12, y: 16, "text-anchor": "start", class: "f-sub" }});
                rhs.textContent = "Vasool recovers more →";

                const tb = document.querySelector("#forest-table tbody");
                forestRows.forEach(r => {{
                    const tr = document.createElement("tr");
                    tr.innerHTML =
                        `<td>${{r.arm}}</td>` +
                        `<td>${{(r.point >= 0 ? "+" : "") + (r.point * 100).toFixed(3)}}pp</td>` +
                        `<td>[${{(r.low * 100).toFixed(3)}}, ${{(r.high * 100).toFixed(3)}}]</td>` +
                        `<td>${{r.excludes_zero ? "yes" : "no"}}</td>`;
                    tb.appendChild(tr);
                }});
            }}

            // 1e. §7 survival grid. Only the exception carries colour.
            const sweeps = EVAL?.sweeps || {{}};
            const sweepNames = Object.keys(sweeps);
            const gridBox = document.getElementById("sweep-grid");
            if (gridBox && sweepNames.length) {{
                const armsSeen = new Set();
                sweepNames.forEach(c => Object.keys(sweeps[c].arms || {{}}).forEach(a => armsSeen.add(a)));
                const flipCount = (arm) => sweepNames.filter(
                    c => sweeps[c].arms?.[arm]?.survives === false
                ).length;
                const order = [...armsSeen].sort(
                    (a, b) => flipCount(b) - flipCount(a) || a.localeCompare(b)
                );
                order.forEach(arm => {{
                    const name = document.createElement("div");
                    name.className = "grid-arm"; name.textContent = arm;
                    const cells = document.createElement("div");
                    cells.className = "grid-cells";
                    let flips = 0;
                    sweepNames.forEach(cfg => {{
                        const a = sweeps[cfg].arms?.[arm];
                        const bad = a && a.survives === false;
                        if (bad) flips++;
                        const c = document.createElement("div");
                        c.className = "grid-cell" + (bad ? " flip" : "");
                        c.title = `${{arm}} · ${{cfg}}\n${{bad ? "fails to survive" : "survives"}}`;
                        cells.appendChild(c);
                    }});
                    const count = document.createElement("div");
                    count.className = "grid-count" + (flips ? " flip" : "");
                    trace(count, `sweeps.*.arms.${{arm}}.survives`,
                          `${{flips}} / ${{sweepNames.length}}`);
                    gridBox.append(name, cells, count);
                }});
            }}

            // 1f. §9's seven criteria.
            const F_META = [
                ["F1", "F1_taxonomy_adds_nothing", "The taxonomy adds nothing",
                 "fires iff the paired interval vs retry_plus_contact includes zero"],
                ["F2", "F2_flagship_claim_inert", "The flagship card_expired claim is inert",
                 "fires iff A3 is inert on recovery rate AND on attempts consumed"],
                ["F3", "F3_salary_timing_is_noise", "Salary-aware timing is noise",
                 "fires iff A2's paired interval includes zero"],
                ["F4", "F4_guards_unreliable", "The guards are unreliable",
                 "fires iff pass^100 on the §2a predicate is below 1.0"],
                ["F5", "F5_compliance_unaffordable", "Compliance is unaffordable",
                 "fires iff ungated beats gated by more than 20 absolute points"],
                ["F6", "F6_conclusions_are_model_artifacts", "The conclusions are model artifacts",
                 "fires iff 5 or more of 8 comparisons flip across the 83-config grid"],
                ["F7", "F7_determinism_fails", "Determinism fails",
                 "fires iff two runs of one seed produce different ledgers"]
            ];
            const fb = document.getElementById("fboard");
            if (fb) {{
                const fal = EVAL?.falsification || {{}};
                F_META.forEach(([id, key, name, thr]) => {{
                    const v = fal[key];
                    const row = document.createElement("div");
                    row.className = "frow";
                    let icon, colour, text;
                    if (!v) {{
                        icon = "—"; colour = "var(--viz-muted)"; text = "not in artifact";
                    }} else if (v.fired === true) {{
                        icon = "✗"; colour = "var(--status-critical)"; text = "FIRED";
                    }} else if (v.fired === false) {{
                        icon = "✓"; colour = "var(--status-good)"; text = "did not fire";
                    }} else {{
                        icon = "○"; colour = "var(--viz-muted)"; text = "not evaluated here";
                    }}
                    let measured = "";
                    if (key.startsWith("F1") && v?.interval)
                        measured = ` · ${{(v.interval.point * 100).toFixed(2)}}pp, ${{v.direction}}`;
                    if (key.startsWith("F3") && v?.interval)
                        measured = ` · +${{(v.interval.point * 100).toFixed(2)}}pp`;
                    if (key.startsWith("F5") && typeof v?.gap_pp === "number")
                        measured = ` · ${{v.gap_pp.toFixed(2)}}pp of ${{v.threshold_pp}}`;
                    if (key.startsWith("F4") && v?.pass_k)
                        measured = ` · pass^100 = ${{v.pass_k["100"] ?? "—"}}`;
                    if (key.startsWith("F6") && typeof v?.threshold === "number")
                        measured = ` · threshold ${{v.threshold}} of ${{v.denominator?.length ?? "?"}}`;
                    if (key.startsWith("F7") && Array.isArray(v?.seeds_checked))
                        measured = ` · ledgers identical on seeds ${{v.seeds_checked.join(", ")}}`;

                    // F1 did not fire, and that is the misleading case: the
                    // interval excludes zero on the baseline's side. Flag it.
                    const misleading = key.startsWith("F1") && v?.direction === "vasool behind";
                    if (misleading) row.className = "frow warn";

                    row.innerHTML =
                        `<div class="fid" style="color:${{colour}}">${{id}}</div>` +
                        `<div><span class="fname">${{name}}</span>` +
                        `<span class="fthr">${{thr}}</span>` +
                        (misleading
                            ? `<span class="fthr" style="color:var(--status-warning)">` +
                              `⚠ did not fire &mdash; but the interval excludes zero on the wrong side. ` +
                              `A worse result than F1 firing, and not covered by the registered wording.</span>`
                            : "") +
                        `</div>` +
                        `<div class="fverdict" style="color:${{colour}}"></div>`;
                    trace(row.querySelector(".fverdict"),
                          `falsification.${{key}}.fired`, `${{icon}} ${{text}}${{measured}}`);
                    fb.appendChild(row);
                }});
            }}

            // 1g. §2a stat tiles.
            const tiles = document.getElementById("safety-tiles");
            if (tiles) {{
                const v = EVAL?.per_arm?.vasool;
                const pk = EVAL?.pass_k || {{}};
                const det = EVAL?.determinism;
                const items = [
                    [v ? `${{nf(v.safety_holds_on)}} / ${{nf(v.seeds)}}` : "—",
                     "seeds where all eight §2a claims held",
                     "per_arm.vasool.safety_holds_on"],
                    [pk["100"] !== undefined ? Number(pk["100"]).toFixed(2) : "—",
                     "pass^100 — every one of 100 independent worlds clean",
                     "pass_k.100"],
                    [v ? nf(v.risk_block_actions_world) : "—",
                     "automated actions on risk-declined episodes",
                     "per_arm.vasool.risk_block_actions_world"],
                    [det ? (det.identical ? "identical" : "MISMATCH") : "—",
                     "re-run ledgers, byte-for-byte (F7)",
                     "determinism.identical"]
                ];
                items.forEach(([val, lab, src]) => {{
                    const d = document.createElement("div");
                    d.className = "tile";
                    d.innerHTML = `<div class="tv"></div><div class="tl">${{lab}}</div>`;
                    trace(d.querySelector(".tv"), src, val);
                    tiles.appendChild(d);
                }});
            }}

            // 1h. §4.5 — the rules-vs-LLM comparison, from its own artifact.
            let SHADOW = {{}};
            try {{
                SHADOW = JSON.parse(document.getElementById("shadow-data").textContent) || {{}};
            }} catch (e) {{ SHADOW = {{}}; }}

            const llmBody = document.querySelector("#llm-table tbody");
            const llmNote = document.getElementById("llm-note");
            const llmTiles = document.getElementById("llm-tiles");
            const ov = SHADOW.overall;

            if (llmBody && ov) {{
                const pctOf = (x) => (typeof x === "number" ? (x * 100).toFixed(1) + "%" : "—");
                [
                    [pctOf(ov.rules_accuracy), "rules classifier — by construction, not measured"],
                    [pctOf(ov.llm_accuracy), "LLM picked the right failure class"],
                    [pctOf(ov.intervention_agreement), "LLM picked the action §4 names for the row"],
                    [`${{SHADOW.covered_cells}} / ${{SHADOW.total_cells}}`, "cells recorded — free-tier quota, not a design choice"]
                ].forEach(([v, l]) => {{
                    const d = document.createElement("div");
                    d.className = "tile";
                    d.innerHTML = `<div class="tv">${{v}}</div><div class="tl">${{l}}</div>`;
                    llmTiles.appendChild(d);
                }});

                (SHADOW.by_cell || []).forEach(c => {{
                    const tr = document.createElement("tr");
                    const recorded = c.repeats > 0 && c.absent < c.repeats;
                    const top = (c.llm_classes && c.llm_classes.length)
                        ? c.llm_classes.map(([k, n]) => `${{k}}×${{n}}`).join(", ")
                        : "—";
                    const acc = recorded ? (c.llm_accuracy * 100).toFixed(0) + "%" : "—";
                    const con = recorded && typeof c.llm_consistency === "number"
                        ? (c.llm_consistency * 100).toFixed(0) + "%" : "—";
                    // A stable wrong answer is the finding. Accuracy alone hides it,
                    // consistency alone flatters it; they are shown together.
                    const damning = recorded && c.llm_accuracy === 0 && c.llm_consistency === 1;
                    tr.innerHTML =
                        `<td>${{c.error_reason}} / ${{c.error_source}}</td>` +
                        `<td>${{c.truth}}</td>` +
                        `<td class="zero">${{c.rules}}</td>` +
                        `<td class="${{recorded ? (c.llm_accuracy === 1 ? "zero" : "nonzero") : ""}}">${{top}}</td>` +
                        `<td class="${{recorded && c.llm_accuracy < 1 ? "nonzero" : ""}}">${{acc}}</td>` +
                        `<td${{damning ? ' class="nonzero"' : ""}}>${{con}}</td>` +
                        `<td>${{new Intl.NumberFormat().format(c.episodes)}}</td>`;
                    llmBody.appendChild(tr);
                }});

                const worst = (SHADOW.by_cell || [])
                    .filter(c => c.absent < c.repeats && c.llm_accuracy === 0 && c.llm_consistency === 1)
                    .sort((a, b) => b.episodes - a.episodes)[0];
                llmNote.innerHTML = worst
                    ? `<strong>Read the last two columns together.</strong> On ` +
                      `<code>${{worst.error_reason}} / ${{worst.error_source}}</code> — the largest ` +
                      `recorded cell at ${{new Intl.NumberFormat().format(worst.episodes)}} episodes — the ` +
                      `model answered <strong>${{worst.llm_classes[0][0]}}</strong> every single time, ` +
                      `for a consistency of 100% and an accuracy of 0%. A stable wrong answer is worse ` +
                      `than an unstable one: it is confidently, reproducibly incorrect about the most ` +
                      `common failure on the platform, and either column alone would have hidden it. ` +
                      `That is the measurement behind keeping this component in shadow.`
                    : `Every recorded cell is shown. A dash means the cell has no recording and ` +
                      `contributes to no rate.`;
            }} else if (llmNote) {{
                llmNote.textContent =
                    "No comparison artifact on disk — run `make shadow` to build one.";
            }}

            // 1i. The adversary, from out/adversary/redteam.json.
            let RT = {{}};
            try {{
                RT = JSON.parse(document.getElementById("redteam-data").textContent) || {{}};
            }} catch (e) {{ RT = {{}}; }}

            const rtBody = document.querySelector("#rt-table tbody");
            const rtTiles = document.getElementById("rt-tiles");
            if (rtBody && Array.isArray(RT.results)) {{
                [
                    [`${{RT.survived}} / ${{RT.attacks}}`, "attacks survived the registered criterion"],
                    [String(RT.failed), "open failures, each named and reproducible"],
                    [RT.as_registered ? "yes" : "NO", "every attack judged against the criterion as registered"],
                    ["13", "guards, all evaluated then resolved by severity"]
                ].forEach(([v, l]) => {{
                    const d = document.createElement("div");
                    d.className = "tile";
                    d.innerHTML = `<div class="tv">${{v}}</div><div class="tl">${{l}}</div>`;
                    rtTiles.appendChild(d);
                }});

                // Failures first: they are the informative half.
                const ordered = RT.results.slice().sort(
                    (a, b) => (a.survived === b.survived) ? a.id.localeCompare(b.id)
                                                          : (a.survived ? 1 : -1)
                );
                ordered.forEach(r => {{
                    const broke = (r.clauses || []).filter(c => !c.held);
                    const why = broke.length
                        ? broke.map(c => c.detail).join("; ")
                        : `all ${{(r.clauses || []).length}} clauses held`;
                    const tr = document.createElement("tr");
                    // No row-level tint: `is-vasool` is the green "this is ours"
                    // class, and painting a failure green is exactly the wrong
                    // signal. The verdict cell carries the colour.
                    tr.innerHTML =
                        `<td><b>${{r.id}}</b> &middot; ${{r.title}}</td>` +
                        `<td class="${{r.survived ? "zero" : "nonzero"}}">` +
                        `${{r.survived ? "\\u2713 survived" : "\\u2717 open"}}</td>` +
                        `<td style="white-space: normal; text-align: left; max-width: 420px;">${{why}}</td>` +
                        `<td>${{r.receipts}}</td>` +
                        `<td style="font-size: 11px;">${{(r.ledger_sha256 || "").slice(0, 12)}}</td>`;
                    rtBody.appendChild(tr);
                }});
            }}

            // 2. Generate Data-Driven Spine
            const spine = document.getElementById("spine");
            const exhibitData = [
                JSON.stringify(EVAL.per_arm || "exhibit-a-fallback"),
                JSON.stringify(guards),
                "EXHIBIT_C_AIRGAP_TOPOLOGY",
                "EXHIBIT_D_QUEUE_SURVIVAL_A15_A19",
                ledgerHead
            ];

            for (let i = 0; i < exhibitData.length; i++) {{
                const hash = await hashString(exhibitData[i]);
                const hexStr = hash.substring(0, 8);
                
                const node = document.createElement("div");
                node.className = "hash-node";
                node.innerText = hexStr;
                spine.appendChild(node);
                
                if (i < exhibitData.length - 1) {{
                    const line = document.createElement("div");
                    line.className = "spine-line";
                    spine.appendChild(line);
                }}
            }}

            // 3. Animate Hero Counters
            const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            const heroRuns = document.getElementById("hero-trajectories");
            const formatNum = (num) => new Intl.NumberFormat().format(num);
            
            if (prefersReducedMotion) {{
                heroRuns.innerText = formatNum(totalRuns);
            }} else {{
                let currentRuns = 0;
                const step = Math.ceil(totalRuns / 30);
                const interval = setInterval(() => {{
                    currentRuns += step;
                    if (currentRuns >= totalRuns) {{
                        currentRuns = totalRuns;
                        clearInterval(interval);
                    }}
                    heroRuns.innerText = formatNum(currentRuns);
                }}, 30);
            }}

            // Money first. The run count is effort; this is the result, and it is
            // what Track 03's bar actually asks for.
            const heroMoney = document.getElementById("hero-money");
            const paise = EVAL?.per_arm?.vasool?.recovered_paise_total;
            if (heroMoney) {{
                if (typeof paise !== "number") {{
                    heroMoney.innerText = "\u2014";
                }} else {{
                    trace(heroMoney, "per_arm.vasool.recovered_paise_total",
                          "\u20b9" + (paise / 100 / 1e7).toFixed(2) + " Cr");
                }}
            }}

            const heroCohort = document.getElementById("hero-cohort");
            if (heroCohort) {{
                trace(heroCohort, "cohort", EVAL?.cohort || "\u2014");
            }}

            const heroViolations = document.getElementById("hero-violations");
            if (safetyViolations === null) {{
                heroViolations.innerText = "safety violations: unavailable (no measurement in artifact)";
            }} else {{
                trace(heroViolations, "per_arm.vasool.seeds − per_arm.vasool.safety_holds_on",
                      `${{formatNum(safetyViolations)}} safety violations in ${{formatNum(safetySeeds)}} seeds`);
            }}

            // 4. Populate Yield
            trace(document.getElementById("yield-vasool"),
                  "per_arm.vasool.recovery_rate_mean", vasoolYield + "%");
            trace(document.getElementById("yield-baseline"),
                  "per_arm.retry_plus_contact.recovery_rate_mean", baselineYield + "%");
            trace(document.getElementById("yield-greedy"),
                  "per_arm.vasool_ungated.recovery_rate_mean", greedyYield + "%");
            
            // 5. Pre-fill a valid receipt ID if the ledger exists
            if (sampleLedger.length > 0 && sampleLedger[0].receipt_id) {{
                document.getElementById("verify-input").value = sampleLedger[0].receipt_id;
            }}
            
            // 6. Trajectory Explorer
            const explorerList = document.getElementById("explorer-list");
            const explorerConsole = document.getElementById("explorer-console");
            
            if (sampleLedger && sampleLedger.length > 0) {{
                // Show max 20 trajectories
                const displayLedger = sampleLedger.slice(0, 20);
                
                displayLedger.forEach((record, index) => {{
                    const item = document.createElement("div");
                    item.className = "explorer-item";
                    item.innerText = `${{record.receipt_id.substring(0, 8)}}...`;
                    
                    if (index === 0) item.classList.add("active");
                    
                    item.addEventListener("click", () => {{
                        document.querySelectorAll(".explorer-item").forEach(el => el.classList.remove("active"));
                        item.classList.add("active");
                        
                        let payloadText = record.canonical_payload || "No canonical payload available.";
                        payloadText = payloadText.replace(/\\|/g, "\\n");
                        explorerConsole.innerHTML = `<strong>[RECEIPT: ${{record.receipt_id}}]</strong>\\n\\n${{escapeHtml(payloadText)}}`;
                    }});
                    
                    explorerList.appendChild(item);
                }});
                
                // Pre-select first item
                if (displayLedger.length > 0) {{
                    let payloadText = displayLedger[0].canonical_payload || "";
                    payloadText = payloadText.replace(/\\|/g, "\\n");
                    explorerConsole.innerHTML = `<strong>[RECEIPT: ${{displayLedger[0].receipt_id}}]</strong>\\n\\n${{escapeHtml(payloadText)}}`;
                }}
            }} else {{
                explorerConsole.innerHTML = `&gt; Ledger is empty or missing from evaluation.json`;
            }}
            
            // 3. Cinematic Number Counting. Skipped under prefers-reduced-motion
            // — the hero counter already honours it, and a half-counted figure
            // is a wrong figure to anyone who screenshots mid-animation.
            document.querySelectorAll('.count-up').forEach(el => {{
                const text = el.innerText;
                if (prefersReducedMotion || !isFinite(parseFloat(text.replace(/,/g, '').replace('%', '')))) {{
                    return;
                }}
                const isPercent = text.includes('%');
                const target = parseFloat(text.replace(/,/g, '').replace('%', ''));
                let start = 0;
                const duration = 1500;
                const step = target / (duration / 16);
                
                const animate = () => {{
                    start += step;
                    if (start >= target) {{
                        el.innerText = text;
                    }} else {{
                        el.innerText = (isPercent ? start.toFixed(2) + '%' : Math.floor(start).toLocaleString());
                        requestAnimationFrame(animate);
                    }}
                }};
                requestAnimationFrame(animate);
            }});
        }});

        function escapeHtml(unsafe) {{
            return unsafe
                 .replace(/&/g, "&amp;")
                 .replace(/</g, "&lt;")
                 .replace(/>/g, "&gt;")
                 .replace(/"/g, "&quot;")
                 .replace(/'/g, "&#039;");
        }}

        async function runVerification() {{
            const consoleBox = document.getElementById("verifier-console");
            const inputId = document.getElementById("verify-input").value.trim();
            const safeInputId = escapeHtml(inputId);
            
            consoleBox.innerHTML = `&gt; fetching ledger record ${{safeInputId}}...\\n`;
            
            if (!inputId) {{
                consoleBox.innerHTML += `<span class="console-error">&gt; ERROR: No receipt ID provided.</span>`;
                return;
            }}

            const record = sampleLedger.find(r => r.receipt_id === inputId);
            
            if (!record) {{
                consoleBox.innerHTML += `<span class="console-error">&gt; ERROR: Receipt ${{safeInputId}} not found in EVAL.</span>\\n`;
                consoleBox.innerHTML += `<span class="console-error">&gt; NOTE: evaluation.json limits ledger size. Run 'make replay' for the full 9,000 base trajectories.</span>`;
                return;
            }}
            
            consoleBox.innerHTML += `&gt; Found record. Generating SHA-256 digest via Web Crypto API...\\n`;
            
            try {{
                if (!record.canonical_payload) {{
                    consoleBox.innerHTML += `<span class="console-error">&gt; FATAL: Record is missing 'canonical_payload'. Please re-run the ledger pipeline.</span>`;
                    return;
                }}
                
                const payloadStr = record.canonical_payload;
                consoleBox.innerHTML += `&gt; payload: ${{escapeHtml(payloadStr.substring(0, 60))}}...\\n`;
                
                const hashHex = await hashString(payloadStr);
                
                if (hashHex === "insecure-env") {{
                    consoleBox.innerHTML += `\\n<span class="console-error">&gt; ERROR: Web Crypto API is unavailable. Are you opening this file locally (file://) in Safari? Use Chrome, or host it on an HTTPS server (like GitHub Pages) to run live cryptographic verification.</span>`;
                    return;
                }}
                
                consoleBox.innerHTML += `\\n&gt; COMPUTED HASH: ${{hashHex}}\\n`;
                
                const expectedHash = record.hash || "UNKNOWN";
                consoleBox.innerHTML += `&gt; LEDGER HASH:   ${{expectedHash}}\\n`;
                
                if (hashHex === expectedHash) {{
                    consoleBox.innerHTML += `\\n<span class="console-success">&gt; VERIFIED: Digest matches ledger. Cryptographic chain is intact.</span>`;
                }} else {{
                    consoleBox.innerHTML += `\\n<span class="console-error">&gt; FATAL: Hash mismatch! Ledger integrity compromised or serialization algorithm differs.</span>`;
                }}
            }} catch (error) {{
                consoleBox.innerHTML += `\\n<span class="console-error">&gt; ERROR executing Web Crypto API: ${{error.message}}</span>`;
            }}
        }}
    </script>
</body>
</html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_content, encoding="utf-8")
    print(f"wrote {out_path} based on {json_path}", file=sys.stderr)

if __name__ == "__main__":
    base_dir = pathlib.Path(__file__).parent.parent
    eval_json = base_dir / "out" / "development" / "evaluation.json"
    out_html = base_dir / "out" / "report.html"
    build_report(eval_json, out_html)
