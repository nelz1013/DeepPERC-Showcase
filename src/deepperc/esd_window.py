"""GF180 ESD design window: ESD path vs. gate-oxide breakdown (rule ``ew/gate_oxide``).

A pad is zapped against one of its own rails. Its clamps limit the pad to a peak voltage *V*, the
stress. Every MOS gate oxide hung on the pad net, directly or through one series resistor, sees
*V*. The check compares *V* with the oxide's ESD-timescale breakdown *BV*, with a margin *m*:

* PASS if V <= BV * (1 - m); WARN if BV * (1 - m) < V <= BV; FAIL if V > BV.
* UNRESOLVABLE (with a reason) when *V* or *BV* can't be set from cited rows. Never a silent pass.

**Modes.** Two reverse modes per pad: PS (pad positive, a ground rail grounded) and ND (pad
negative, a positive rail grounded). The stress at a node is the higher of the two. The forward
modes (PD, NS) put one diode drop on the pad and are not evaluated.

**Stress of one mode at one node.** Every clamp path below is an upper bound on the node voltage,
so the mode's stress is the lowest one that resolves:

1. *Snapback* (ggNMOS ``nfet_*_dss`` to a ground rail in PS; ggPMOS ``pfet_*_dss`` to a positive
   rail in ND): the node is held at max(Vt1, Vt2) from the device's TLP row.
2. *Diode, then power clamp* (``diode_pd2nw`` up to a positive rail in PS; ``diode_nd2ps`` down to
   a ground rail in ND): the diode's Vt2 plus the V_clamp of a power-clamp cell spanning that rail
   and a rail of the other polarity.
3. No clamp, or none with values: UNRESOLVABLE ``no_limit`` (``black_box`` if the node meets an
   unextracted instance). No rail of the needed polarity: UNRESOLVABLE ``unknown_node``.

**Subjects.** The gate of every non-``_dss`` MOS, and either plate of a MOS capacitor, whose net is
the pad net (depth 0) or a node joined to it by exactly one resistor (depth 1). At depth 1 the node
is bounded by the lower of the pad's stress and its own secondary clamp's. Devices inside a
characterized library I/O cell are the library's responsibility and are not subjects.

**Provenance caps.** A FAIL needs every row behind *V* and *BV* to be ``pdk-cited`` or
``user-asserted``. A breakdown row marked ``lower_bound`` (the PDK's parametric BVox stands in for
the unpublished ESD-timescale value) caps a FAIL at WARN ``bv_lower_bound``. A ``sim-derived``
row caps a FAIL at WARN and withholds a PASS as WARN ``sim_derived``. A ``deck-cited`` row caps a
FAIL at WARN ``deck_only``. Every cap that applied is listed.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from deepperc import pads as padmod
from deepperc.limits import FAIL_SOURCES, LimitRow, Limits, Source
from deepperc.names import fold
from deepperc.spice import Device, FlatNetlist

RULE = "ew/gate_oxide"
MODES = ("PS", "ND")
_CELL_SUFFIX = re.compile(r"(\$\d+)+$")

NOTES = (
    "Forward modes (PD, NS) put one diode drop on the pad and are not evaluated.",
    "Pad-ring bus resistance is ignored (no I*R added to the stress).",
    "Only one pad against its own rails is judged; pad-to-pad and cross-domain stacking are not.",
)


# ----------------------------------------------------------------------------------------------- #
# Device classes
# ----------------------------------------------------------------------------------------------- #


def is_dss(model: str) -> bool:
    return fold(model).endswith("_dss")


def mos_polarity(model: str) -> str | None:
    m = fold(model)
    return "P" if m.startswith("pfet") else "N" if m.startswith("nfet") else None


def esd_direction(d: Device, node: int) -> tuple[str, str, int] | None:
    """If ``d`` is an ESD device with a terminal on ``node``: (``snapback`` | ``diode``,
    ``up`` | ``down``, the net on its other side)."""
    m = fold(d.model)
    if d.kind == "D":
        a, c = d.net("A"), d.net("C")
        if m.startswith("diode_pd2nw") and a == node and c is not None and c != node:
            return "diode", "up", c
        if m.startswith("diode_nd2ps") and c == node and a is not None and a != node:
            return "diode", "down", a
        return None
    if d.kind == "M" and is_dss(d.model):
        s, dr = d.net("S"), d.net("D")
        if node not in (s, dr) or s == dr:
            return None
        other = dr if s == node else s
        pol = mos_polarity(d.model)
        if pol:
            return "snapback", "up" if pol == "P" else "down", other
    return None


def gate_terminals(d: Device) -> list[tuple[str, int]]:
    """Exposed oxide terminals: a MOS gate (``_dss`` clamps excluded), either plate of a MOS cap."""
    if d.kind == "M" and mos_polarity(d.model) and not is_dss(d.model):
        g = d.net("G")
        return [("G", g)] if g is not None else []
    if d.kind == "C" and re.match(r"^cap_[np]mos_", fold(d.model)):
        return [(r, n) for r, n in zip(d.roles, d.nets, strict=False) if r in ("P", "N")]
    return []


# ----------------------------------------------------------------------------------------------- #
# Stress
# ----------------------------------------------------------------------------------------------- #


@dataclass
class Mode:
    """Stress of one mode at one node. ``value`` None means unresolved, with ``reason``."""

    value: float | None
    reason: str | None = None
    how: str = ""
    rows: list[tuple[LimitRow, str, float]] = field(default_factory=list)  # (row, param, value)
    clamps: list[str] = field(default_factory=list)


@dataclass
class PowerClamp:
    instance: str
    cell: str
    ports: frozenset[int]


class Stress:
    def __init__(self, nl: FlatNetlist, lim: Limits, ps: padmod.PadSet, clamps: list[PowerClamp]):
        self.nl, self.lim, self.ps, self.clamps = nl, lim, ps, clamps
        self.on: dict[int, list[int]] = {}
        for i, d in enumerate(nl.devices):
            for n in set(d.nets):
                self.on.setdefault(n, []).append(i)

    def _v_clamp(self, p: int, g: int) -> tuple[float | None, LimitRow | None, str]:
        best: tuple[float | None, LimitRow | None, str] = (None, None, "")
        for c in self.clamps:
            if p == g or p not in c.ports or g not in c.ports:
                continue
            pk = self.lim.v_clamp(c.cell)
            if pk.value is not None and (best[0] is None or pk.value < best[0]):
                best = (pk.value, pk.row, c.instance)
        return best

    def mode(self, node: int, mode: str, pos: set[int], gnd: set[int]) -> Mode:
        need = gnd if mode == "PS" else pos
        if not need:
            return Mode(None, "unknown_node", f"no {'ground' if mode == 'PS' else 'positive'} rail")
        cands: list[Mode] = []
        seen: list[str] = []
        snap_dir, diode_dir = ("down", "up") if mode == "PS" else ("up", "down")
        for i in self.on.get(node, []):
            d = self.nl.devices[i]
            e = esd_direction(d, node)
            if e is None:
                continue
            kind, direction, other = e
            if kind == "snapback" and direction == snap_dir and other in need:
                vt1, vt2 = self.lim.clamp_param(d.model, "Vt1"), self.lim.clamp_param(d.model, "Vt2")
                seen.append(f"{d.name} {d.model}")
                if vt1.value is None:
                    continue
                if vt2.value is None:
                    cands.append(Mode(vt1.value, how=f"{d.model} Vt1 {vt1.value:g} V (Vt2 missing)", rows=[(vt1.row, "Vt1", vt1.value)], clamps=[d.name]))
                else:
                    v = max(vt1.value, vt2.value)
                    cands.append(Mode(v, how=f"{d.model} snapback max(Vt1, Vt2) = {v:g} V", rows=[(vt1.row, "Vt1", vt1.value), (vt2.row, "Vt2", vt2.value)], clamps=[d.name]))
            elif kind == "diode" and direction == diode_dir and other in (pos if mode == "PS" else gnd):
                seen.append(f"{d.name} {d.model}")
                vt2 = self.lim.clamp_param(d.model, "Vt2")
                if vt2.value is None:
                    continue
                spans = [(other, g) for g in gnd] if mode == "PS" else [(p, other) for p in pos]
                for p, g in spans:
                    vc, row, inst = self._v_clamp(p, g)
                    if vc is None:
                        continue
                    v = round(vt2.value + vc, 6)
                    cands.append(
                        Mode(
                            v,
                            how=f"{d.model} Vt2 {vt2.value:g} V + power clamp {inst} V_clamp {vc:g} V = {v:g} V",
                            rows=[(vt2.row, "Vt2", vt2.value), (row, "V_clamp", vc)],
                            clamps=[d.name, inst],
                        )
                    )
        if cands:
            return min(cands, key=lambda m: m.value)
        if not seen:
            reason = "black_box" if node in self.ps.black_box_nets else "no_limit"
            return Mode(None, reason, f"no {mode} clamp on the node")
        return Mode(None, "no_limit", f"{len(seen)} clamp(s) without a usable Vt1/Vt2/V_clamp row, e.g. {seen[0]}")


def power_clamps(cells, top: str, nl: FlatNetlist, lim: Limits) -> list[PowerClamp]:
    a2n = {a: i for i, al in enumerate(nl.aliases) for a in al}
    merged = padmod._merged(a2n)
    out = []
    for prefix, target, _nets, _parent in padmod.walk(cells, top):
        if target is None:
            continue
        base = _CELL_SUFFIX.sub("", target)
        if not lim.is_power_clamp(base):
            continue
        ports = {n for p in cells[target].ports if (n := padmod.port_net(a2n, merged, prefix, p)) is not None}
        out.append(PowerClamp(prefix.rstrip("/"), base, frozenset(ports)))
    return out


# ----------------------------------------------------------------------------------------------- #
# Check
# ----------------------------------------------------------------------------------------------- #


@dataclass
class Path:
    pad: str
    group: str
    depth: int
    resistor: str | None
    node: str
    modes: dict[str, Mode]


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}".rstrip("0").rstrip(".")


def _row_ref(r: LimitRow, param: str | None = None, value: float | None = None) -> dict:
    return {
        "row": r.rid,
        "devices": r.devices,
        "param": param,
        "value": r.max if value is None else value,
        "source": str(r.source),
        "provenance": provenance(r.source),
        "lower_bound": r.lower_bound,
        "citation": r.citation,
    }


def provenance(src: Source) -> str:
    return {
        Source.PDK_CITED: "PDK-documented",
        Source.USER_ASSERTED: "user-asserted",
        Source.SIM_DERIVED: "simulated (not PDK-documented)",
        Source.DECK_CITED: "deck only (not PDK-documented)",
    }.get(src, "none")


def run_check(cells, top: str, nl: FlatNetlist, lim: Limits, *, margin: float = 0.10, pads_user=None) -> dict:
    if not 0 <= margin < 1:
        raise ValueError("margin must be in [0, 1)")
    ps = padmod.identify(cells, top, nl, pads_user)
    clamps = power_clamps(cells, top, nl, lim)
    st = Stress(nl, lim, ps, clamps)
    name = nl.names
    findings: list[dict] = []
    if not ps.pads:
        findings.append(
            {
                "id": "EW-00000",
                "rule": RULE,
                "subject": "design",
                "verdict": "UNRESOLVABLE",
                "reason": "pad_unidentified",
                "detail": "no GF180 I/O cell or bond pad found; give a --pads file",
            }
        )
    io_pads = [p for p in ps.pads.values() if not p.supply]
    all_rails = set(ps.rails_pos) | set(ps.rails_gnd)
    taps = padmod._well_taps(nl)
    excluded = tuple(ps.excluded_prefixes)
    subjects: dict[int, list[Path]] = {}
    pad_rows = []
    for pad in sorted(io_pads, key=lambda p: name[p.net]):
        pos, gnd = ps.rails_of(pad)
        pad_modes = {m: st.mode(pad.net, m, pos, gnd) for m in MODES}
        nodes: list[tuple[int, int, str | None, dict[str, Mode]]] = [(pad.net, 0, None, pad_modes)]
        for i in st.on.get(pad.net, []):
            d = nl.devices[i]
            if d.kind != "R":
                continue
            p_, n_ = d.net("P"), d.net("N")
            n1 = n_ if p_ == pad.net else p_ if n_ == pad.net else None
            if n1 is None or n1 == pad.net or n1 in all_rails or n1 in ps.pads or n1 in taps:
                continue
            own = {m: st.mode(n1, m, pos, gnd) for m in MODES}
            comb = {}
            for m in MODES:
                ok = [x for x in (pad_modes[m], own[m]) if x.value is not None]
                comb[m] = min(ok, key=lambda x: x.value) if ok else pad_modes[m]
            nodes.append((n1, 1, f"{d.name} {d.model} {d.value or ''}".strip(), comb))
        n_gates = 0
        for node, depth, res, modes in nodes:
            for i in st.on.get(node, []):
                d = nl.devices[i]
                if d.name.startswith(excluded):
                    continue
                if any(n == node for _, n in gate_terminals(d)):
                    n_gates += 1
                    subjects.setdefault(i, []).append(
                        Path(name[pad.net], pad.cell, depth, res, name[node], modes)
                    )
        pad_rows.append(
            {
                "pad": name[pad.net],
                "group": pad.cell,
                "rails_pos": sorted(name[n] for n in pos),
                "rails_gnd": sorted(name[n] for n in gnd),
                "modes": {m: {"v": pad_modes[m].value, "reason": pad_modes[m].reason, "how": pad_modes[m].how} for m in MODES},
                "n_gates": n_gates,
            }
        )
    for k, (i, paths) in enumerate(sorted(subjects.items(), key=lambda kv: nl.devices[kv[0]].name), start=1):
        findings.append(_finding(f"EW-{k:05d}", nl.devices[i], paths, lim, margin))
    counts = Counter(f["verdict"] for f in findings)
    by_reason = Counter(f"{f['verdict']}:{f.get('reason') or '-'}" for f in findings)
    return {
        "schema": "deepperc.showcase.report/1",
        "check": "esd_window",
        "rule": RULE,
        "top": top,
        "margin": margin,
        "summary": {
            "subjects": len(subjects),
            "io_pads": len(io_pads),
            "supply_pads": sum(p.supply for p in ps.pads.values()),
            "power_clamps": len(clamps),
            "verdicts": {v: counts.get(v, 0) for v in ("PASS", "WARN", "FAIL", "UNRESOLVABLE")},
            "by_reason": dict(sorted(by_reason.items())),
        },
        "pads": pad_rows,
        "power_clamps": [
            {"instance": c.instance, "cell": c.cell, "v_clamp": lim.v_clamp(c.cell).value, "row": (r := lim.v_clamp(c.cell).row) and _row_ref(r, "V_clamp", lim.v_clamp(c.cell).value)}
            for c in clamps
        ],
        "findings": findings,
        "notes": list(NOTES) + [f"netlist: {w}" for w in nl.warnings[:5]],
    }


def _finding(fid: str, d: Device, paths: list[Path], lim: Limits, margin: float) -> dict:
    resolved = [(p, m, md) for p in paths for m, md in p.modes.items() if md.value is not None]
    open_ = [(p, m, md) for p in paths for m, md in p.modes.items() if md.value is None]
    worst = max(resolved, key=lambda x: x[2].value) if resolved else None
    v = worst[2].value if worst and not open_ else None
    v_lb = worst[2].value if worst and open_ else None
    bv = lim.bv(d.model)
    caps: list[str] = []
    sim_any = any(r.source is Source.SIM_DERIVED for _, _, md in resolved for r, _, _ in md.rows)
    f = {
        "id": fid,
        "rule": RULE,
        "subject": d.name,
        "model": d.model,
        "verdict": None,
        "reason": None,
        "measured_v": v if v is not None else v_lb,
        "measured_is_lower_bound": v is None and v_lb is not None,
        "bv": bv.value,
        "bv_required": None if v is None else round(v / (1 - margin), 4),
        "mode": worst and worst[1],
        "pad": (worst or (paths[0], None, None))[0].pad,
        "group": (worst or (paths[0], None, None))[0].group,
        "depth": (worst or (paths[0], None, None))[0].depth,
        "stress_rows": [_row_ref(r, p, x) for r, p, x in (worst[2].rows if worst else [])],
        "bv_row": bv.row and _row_ref(bv.row, "BV"),
        "witness": [
            f"{p.pad} d{p.depth}{' via ' + p.resistor if p.resistor else ''} {m}: "
            + (f"{_fmt(md.value)} V ({md.how})" if md.value is not None else f"unresolved ({md.reason}: {md.how})")
            for p in paths[:10]
            for m, md in p.modes.items()
        ],
        "n_paths": len(paths),
    }
    # Unresolved stress: a FAIL is still provable when the resolved lower bound exceeds BV.
    if v is None:
        if v_lb is not None and bv.value is not None and v_lb > bv.value:
            verdict = "FAIL"
        else:
            f["verdict"] = "UNRESOLVABLE"
            f["reason"] = open_[0][2].reason if open_ else "no_limit"
            return f
    else:
        if bv.value is None:
            f["verdict"], f["reason"] = "UNRESOLVABLE", "no_limit"
            return f
        verdict = "PASS" if v <= bv.value * (1 - margin) else "WARN" if v <= bv.value else "FAIL"
    behind = [r for r, _, _ in (worst[2].rows if worst else [])]
    if verdict == "FAIL":
        if bv.row and bv.row.lower_bound:
            caps.append("bv_lower_bound")
        if any(r.source is Source.SIM_DERIVED for r in behind):
            caps.append("sim_derived")
        if any(r.source is Source.DECK_CITED for r in [*behind, bv.row] if r):
            caps.append("deck_only")
        assert caps or all(r.source in FAIL_SOURCES for r in [*behind, bv.row] if r)
        if caps:
            f["verdict_uncapped"] = "FAIL"
            verdict, f["reason"] = "WARN", caps[0]
    elif sim_any:
        caps.append("sim_derived")
        if verdict == "PASS":
            f["verdict_uncapped"] = "PASS"
            verdict, f["reason"] = "WARN", "sim_derived"
    f["verdict"] = verdict
    if caps:
        f["caps_applicable"] = caps
    return f
