# deepPERC — showcase

**ESD and latch-up sign-off checks for open-source silicon.**

Open PDKs (GF180MCU, sky130, IHP SG13G2) have made tapeouts accessible, but they ship without the reliability sign-off layer that commercial flows rely on. Designs routinely reach the shuttle with no check that an ESD event has a safe discharge path, or that a pad-to-rail path doesn't exceed device breakdown. deepPERC is a schematic-level checker that fills that gap.

This repo is a **cut-down, runnable slice** of deepPERC. The full engine is in active development in a private repo; this showcase contains one complete check end to end, plus a real report on a public design.

---

## What deepPERC checks

| Check | What it catches |
|---|---|
| **ESD path vs. breakdown** *(included here)* | Discharge paths through multiple devices where the stacked voltage exceeds a device's breakdown limit |
| Static overvoltage | Devices biased beyond PDK limits (lookup table + user-defined limits) |
| Rail clamp per domain | Power domains with no rail clamp |
| Clamp count / spacing per rail | Too few clamps, or clamps too far apart for the pad-ring resistance |
| Clamp sizing & power-clamp simulation | Undersized clamps; estimated clamp voltage from simulating the power clamp |
| Cross-domain crossings | Signals crossing power domains without protection |
| CDM estimate | Rough die-size-based CDM risk |
| Layout-obligations manifest | Requirements a schematic check can't verify, handed off to layout review |

Checks that can't be resolved at schematic level (aging, EM, IR drop) are reported as an explicit **UNRESOLVABLE** class rather than silently skipped.

## How it works

```
 PDK netlist extraction          PDK documentation
 (KLayout for GF180,              (device limits, ESD rules)
  Magic for sky130)                       │
        │                                 ▼
        ▼                        LLM-assisted rule extraction
   Netlist graph  ◄────────────  (every rule tagged: PDK-documented
        │                          vs. tribal knowledge)
        ▼
   Topology checks  ──►  Report: violations · warnings · UNRESOLVABLE
```

- **PDK docs as datasheets.** Rules are extracted from PDK documentation with LLM assistance, and every rule records its provenance: documented in the PDK, or derived from industry practice. Derived values (e.g. clamp spacing from pad-ring metal resistance) are labeled as guidance and can be overridden by the user.
- **Agent-authored rules are measured, not just merged.** Each rule is validated against a corpus of real designs (Tiny Tapeout, Caravel, OpenROAD) plus a mutation corpus of deliberately broken variants, and compared with CVC, the existing open-source checker.
- **Dual-PDK.** GF180MCU is the primary rules PDK; sky130 serves as a control.

## Quickstart

```bash
git clone https://github.com/nelz1013/DeepPERC-Showcase
cd DeepPERC-Showcase
pip install -e .
deepperc esd-window examples/esd_fixture.cir --pads examples/esd_fixture_pads.toml --limits examples/esd_fixture_limits.csv
```

Expected output: a Markdown report on stdout, and on stderr `esd_fixture: 4 gate(s) checked: FAIL 2, WARN 0, PASS 2, UNRESOLVABLE 0` (exit code 1 because of the FAIL). The 6 V receiver on `IN_OK` passes at 11.9 V. The 3.3 V receiver on `IN_BAD` fails, because its 5/6 V clamps let the pad reach 11.7 V against a 9 V oxide breakdown.

Python 3.11+, stdlib only. Run the tests with `pip install -e '.[dev]' && pytest`. Use `--format json` for the full per-device output.

**No PDK install is needed to run the check.** The GF180MCU ESD limits it reads ship in `src/deepperc/data/gf180_esd_limits.csv`, and every row cites its source file in the [GF180MCU PDK](https://github.com/google/gf180mcu-pdk) docs. You do need the PDK to produce a netlist from a GDS: use a standard [open_pdks](https://github.com/RTimothyEdwards/open_pdks) install (`$PDK_ROOT/gf180mcuD`) and its `gf180mcu_fd_pv` KLayout LVS deck in `net_only` mode. No PDK files are vendored here.

## Sample report

[`examples/naxos_report.md`](examples/naxos_report.md) — deepPERC run on **Naxos** (GF180MCU) by ZeduloTech, a public wafer.space design ([github.com/ZeduloTech/Naxos](https://github.com/ZeduloTech/Naxos), Apache-2.0). It flags 36 thin-oxide (3.3 V) gates wired straight to analog pads whose clamps hold them near 13.3 V. That is above the PDK's stated 7 V minimum oxide breakdown. The result is a WARN, not a FAIL, because that 7 V is a lower bound and the clamp voltage is simulated.

## Results so far

| Metric | Value |
|---|---|
| Open designs checked | 36 GF180MCU chips, plus 179 GF180MCU Tiny Tapeout tiles (overvoltage) and 38 sky130 designs |
| PDKs | GF180MCU, sky130 |
| Mutation-corpus detection rate (ESD path vs. breakdown) | 22/22 seeded violations detected, 0 false positives (57/57 labelled cases) |

## Status

The MVP is in its testing phase: the checks are built, and they are now being run against as many open designs as possible across PDKs. IHP SG13G2 support is planned.

## Background

deepPERC is a sibling of deepERC, a board-level electrical rule checker: the methodology carries over from board to die. Built by Nelson, a hardware engineer with a background in reliability and radiation effects (Intel, Applied Materials, NASA).

[LinkedIn](https://www.linkedin.com/in/nelson-gaspard-4645291a/) · [nelson.j.gaspard@gmail.com](mailto:nelson.j.gaspard@gmail.com)

## License

<LICENSE>
