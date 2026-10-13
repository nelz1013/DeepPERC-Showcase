"""Command line: ``deepperc esd-window NETLIST``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deepperc import esd_window, limits, pads, report, spice


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deepperc", description="ESD sign-off checks for open PDKs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ew = sub.add_parser("esd-window", help="ESD path vs. gate-oxide breakdown (GF180MCU)")
    ew.add_argument("netlist", help="extracted or schematic SPICE netlist")
    ew.add_argument("--top", help="top cell (default: the largest uninstantiated cell)")
    ew.add_argument("--pads", help="TOML file: [pads] net = 'io'|'supply', optional [rails] net = 'pos'|'gnd'")
    ew.add_argument("--limits", action="append", default=[], help="extra limit CSV (repeatable), loaded after the bundled GF180 table")
    ew.add_argument("--margin", type=float, default=0.10, help="WARN margin below BV (default 0.10)")
    ew.add_argument("--format", choices=("md", "json"), default="md")
    ew.add_argument("-o", "--output", help="write the report here instead of stdout")
    args = ap.parse_args(argv)

    try:
        text = Path(args.netlist).read_text(encoding="utf-8", errors="replace")
        cells = spice.parse(text)
        nl = spice.flatten(cells, args.top)
        lim = limits.Limits(limits.load_all(args.limits))
        user = pads.load_pads(args.pads) if args.pads else None
        res = esd_window.run_check(cells, nl.top, nl, lim, margin=args.margin, pads_user=user)
    except (OSError, spice.NetlistError, limits.LimitsError, pads.PadsError, ValueError) as exc:
        print(f"deepperc: {exc}", file=sys.stderr)
        return 2
    out = json.dumps(res, indent=1) if args.format == "json" else report.markdown(res)
    if args.output:
        Path(args.output).write_text(out + "\n", encoding="utf-8")
    else:
        print(out)
    s = res["summary"]["verdicts"]
    print(
        f"{res['top']}: {res['summary']['subjects']} gate(s) checked: "
        f"FAIL {s['FAIL']}, WARN {s['WARN']}, PASS {s['PASS']}, UNRESOLVABLE {s['UNRESOLVABLE']}",
        file=sys.stderr,
    )
    return 1 if s["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
