"""Render a check result as Markdown."""

from __future__ import annotations

from collections import Counter

ORDER = ("FAIL", "WARN", "UNRESOLVABLE", "PASS")
MAX_ROWS = 40


def _v(x) -> str:
    return "—" if x is None else f"{x:.3f}".rstrip("0").rstrip(".")


def markdown(res: dict, title: str | None = None, header: str = "") -> str:
    s = res["summary"]
    out = [f"# {title or 'deepPERC ESD design window: ' + res['top']}", ""]
    if header:
        out += [header.rstrip(), ""]
    out += [
        f"Rule `{res['rule']}`: ESD stress on each exposed gate oxide vs. its breakdown. "
        f"Margin {res['margin']:.0%}.",
        "",
        "| Verdict | Count |",
        "|---|---|",
        *(f"| {v} | {s['verdicts'][v]} |" for v in ORDER),
        "",
        f"Subjects (gate oxides on I/O pad paths): {s['subjects']}. I/O pads: {s['io_pads']}. "
        f"Supply pads: {s['supply_pads']}. Power-clamp instances with a V_clamp row: {s['power_clamps']}.",
        "",
        "By verdict and reason: " + ", ".join(f"`{k}` {n}" for k, n in s["by_reason"].items()),
        "",
        "## Pads",
        "",
        "| Pad | Group | PS stress (V) | ND stress (V) | Gates |",
        "|---|---|---|---|---|",
    ]
    def stress(p):
        return tuple(
            _v(md["v"]) if md["v"] is not None else f"unresolved ({md['reason']})"
            for md in (p["modes"]["PS"], p["modes"]["ND"])
        )

    shown = [p for p in res["pads"] if p["n_gates"]]
    rest = [p for p in res["pads"] if not p["n_gates"]]
    for p in shown:
        ps, nd = stress(p)
        out.append(f"| `{p['pad']}` | {p['group']} | {ps} | {nd} | {p['n_gates']} |")
    rest_groups = Counter((p["group"], *stress(p)) for p in rest)
    for (group, ps, nd), n in sorted(rest_groups.items()):
        out.append(f"| {n} pad(s) with no exposed gate | {group} | {ps} | {nd} | 0 |")
    out += ["", "How each pad's stress was set (pads with gates):", ""]
    for p in shown:
        for m in ("PS", "ND"):
            out.append(f"- `{p['pad']}` {m}: {p['modes'][m]['how']}")
    findings = res["findings"]
    out += ["", "## Findings", ""]
    groups = Counter(
        (f["verdict"], f.get("reason") or "", f.get("pad", ""), f.get("model", ""), _v(f.get("measured_v")), _v(f.get("bv")))
        for f in findings
    )
    out += [
        "Grouped by (verdict, reason, worst pad, model, stress, BV); the JSON output lists every device.",
        "",
        "| Verdict | Reason | Pad | Model | V stress | BV | Devices |",
        "|---|---|---|---|---|---|---|",
    ]
    keyed = sorted(groups.items(), key=lambda kv: (ORDER.index(kv[0][0]), kv[0][2], kv[0][3]))
    for (verdict, reason, pad, model, v, bv), n in keyed[:MAX_ROWS]:
        out.append(f"| {verdict} | {reason or '—'} | `{pad}` | {model} | {v} | {bv} | {n} |")
    if len(keyed) > MAX_ROWS:
        out.append(f"| … | | | | | | {len(keyed) - MAX_ROWS} more groups |")
    worst = next((f for v in ORDER for f in findings if f["verdict"] == v and f.get("model")), None)
    if worst:
        out += ["", f"### Example: {worst['id']} ({worst['verdict']})", "", f"`{worst['subject']}` ({worst['model']})", ""]
        out += [f"- {w}" for w in worst["witness"]]
        if worst.get("caps_applicable"):
            out.append(f"- caps applied: {', '.join(worst['caps_applicable'])} (uncapped: {worst.get('verdict_uncapped')})")
    rows = {}
    for f in findings:
        for r in [*(f.get("stress_rows") or []), f.get("bv_row")]:
            if r:
                rows[(r["row"], r.get("param"))] = r
    for c in res["power_clamps"]:
        if c.get("row"):
            rows[(c["row"]["row"], "V_clamp")] = c["row"]
    if rows:
        out += [
            "",
            "## Limit rows used (provenance)",
            "",
            "| Row | Devices | Parameter | Value | Provenance | Lower bound | Citation |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in sorted(rows.values(), key=lambda r: r["row"]):
            out.append(
                f"| {r['row']} | {r['devices']} | {r.get('param') or '—'} | {_v(r['value'])} | {r['provenance']} | "
                f"{'yes' if r['lower_bound'] else 'no'} | {r['citation']} |"
            )
    out += ["", "## Notes", "", *(f"- {n}" for n in res["notes"]), ""]
    return "\n".join(out)
