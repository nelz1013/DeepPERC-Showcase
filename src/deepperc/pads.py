"""Pad and rail identification for GF180.

Sources, in order: a user ``--pads`` file (TOML ``[pads]`` table, net name -> ``io`` | ``supply``);
else the pad pins of GF180 I/O-library signal cells (``gf180mcu_{fd,ocd,ws}_io__{bi,in,asig}_*``)
plus bare bond-pad cells. Top-level ports are not pads: a port is not proof of a bond pad.

Rails come from the supply ports of every I/O-cell instance (``DVDD``/``VDD`` positive,
``DVSS``/``VSS`` ground), and from a ``[rails]`` table in the pads file (net -> ``pos`` | ``gnd``).
A pad from an I/O cell uses its own cell's rails; any other pad uses every identified rail.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from deepperc.spice import FlatNetlist, _primitive_kind

IO_RE = re.compile(r"^(gf180mcu_(fd|ws|ocd)_io__[A-Za-z0-9_]+?)(\$\d+)*$", re.I)
SIGNAL_CELL = re.compile(r"__(bi_|in_|asig_)", re.I)
BONDPAD = re.compile(r"bondpad", re.I)
PAD_PORTS = ("PAD", "ASIG5V")
POS_PORTS = ("DVDD", "VDD")
GND_PORTS = ("DVSS", "VSS")

#: I/O-library masters whose internal ESD network the library characterizes. Devices inside an
#: instance of one of these are the library's responsibility and are not subjects.
CHARACTERIZED = frozenset(
    f"gf180mcu_{lib}_io__{c}"
    for lib, cells in (
        ("fd", ("asig_5p0", "asig_5p0_70x100", "asig_5p0_fixed", "bi_24t", "bi_t", "in_c", "in_s")),
        ("ocd", ("asig_5p0", "bi_24t", "bi_a", "in_c", "in_s")),
    )
    for c in cells
)


class PadsError(ValueError):
    """Malformed ``--pads`` file, or a name in it that matches no net."""


@dataclass
class Pad:
    net: int
    source: str  # io_cell | bondpad | user
    cell: str  # I/O cell short name, bond-pad cell name, or "user"
    instances: list[str] = field(default_factory=list)
    supply: bool = False
    own_pos: set[int] = field(default_factory=set)
    own_gnd: set[int] = field(default_factory=set)


@dataclass
class PadSet:
    pads: dict[int, Pad]
    rails_pos: dict[int, str]  # net -> how it was identified
    rails_gnd: dict[int, str]
    black_box_nets: set[int]
    excluded_prefixes: list[str]  # instance prefixes of characterized library I/O cells

    def rails_of(self, pad: Pad) -> tuple[set[int], set[int]]:
        if pad.own_pos and pad.own_gnd:
            return pad.own_pos, pad.own_gnd
        return set(self.rails_pos), set(self.rails_gnd)


def walk(cells, top):
    """(prefix, cell, instance nets, parent prefix) for every instance, preorder; black boxes as
    (prefix, None, nets, parent prefix)."""
    out = []

    def rec(cell, prefix, stack):
        for head, nets, target in cell.instances:
            child = cells.get(target)
            p = prefix + head + "/"
            if child is None or target in stack:
                if child is None and _primitive_kind(target) is None:
                    out.append((p, None, nets, prefix))
                continue
            out.append((p, target, nets, prefix))
            rec(child, p, stack | {target})

    rec(cells[top], "", frozenset({top}))
    return out


def _merged(a2n: dict[str, int]) -> dict[tuple[str, str], set[int]]:
    """KLayout joins pin names with ``|`` (``DVSS|VSS``): index each part separately."""
    idx: dict[tuple[str, str], set[int]] = {}
    for alias, nid in a2n.items():
        cut = alias.rfind("/") + 1
        for tok in alias[cut:].split("|"):
            idx.setdefault((alias[:cut], tok), set()).add(nid)
    return idx


def port_net(a2n, merged, prefix: str, port: str) -> int | None:
    nid = a2n.get(prefix + port)
    if nid is not None:
        return nid
    hits = merged.get((prefix, port), set())
    return next(iter(hits)) if len(hits) == 1 else None


def load_pads(path: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise PadsError(f"{path}: {exc}") from exc
    pads = data.get("pads")
    if not isinstance(pads, dict) or not pads:
        raise PadsError(f"{path}: [pads] table missing or empty")
    for k, v in pads.items():
        if v not in ("io", "supply"):
            raise PadsError(f"{path}: pads.{k}: expected 'io' or 'supply', got {v!r}")
    rails = data.get("rails", {})
    for k, v in rails.items():
        if v not in ("pos", "gnd"):
            raise PadsError(f"{path}: rails.{k}: expected 'pos' or 'gnd', got {v!r}")
    return pads, rails


def identify(cells, top: str, nl: FlatNetlist, user=None) -> PadSet:
    """``user``: ``(pads, rails)`` from ``load_pads``, or None."""
    a2n = {a: i for i, al in enumerate(nl.aliases) for a in al}
    merged = _merged(a2n)
    pads: dict[int, Pad] = {}
    rails_pos: dict[int, str] = {}
    rails_gnd: dict[int, str] = {}
    black: set[int] = set()
    io_prefixes: list[str] = []
    excluded: list[str] = []
    for prefix, target, nets, parent in walk(cells, top):
        if target is None:
            for n in nets:
                nid = port_net(a2n, merged, parent, n)
                if nid is not None:
                    black.add(nid)
            continue
        m = IO_RE.match(target)
        if m:
            io_prefixes.append(prefix)
            base = m.group(1)
            if base.lower() in CHARACTERIZED:
                excluded.append(prefix)
            short = base.split("__", 1)[1]
            ports = {
                p: n for p in POS_PORTS + GND_PORTS if (n := port_net(a2n, merged, prefix, p)) is not None
            }
            pos = {ports[p] for p in POS_PORTS if p in ports}
            gnd = {ports[p] for p in GND_PORTS if p in ports}
            for n in pos:
                rails_pos.setdefault(n, f"io_cell:{short}")
            for n in gnd:
                rails_gnd.setdefault(n, f"io_cell:{short}")
            if SIGNAL_CELL.search(base) and user is None:
                for p in PAD_PORTS:
                    nid = port_net(a2n, merged, prefix, p)
                    if nid is None:
                        continue
                    pad = pads.get(nid)
                    if pad is None or pad.source != "io_cell":
                        pad = pads[nid] = Pad(nid, "io_cell", short)
                    pad.instances.append(prefix.rstrip("/"))
                    pad.own_pos |= pos
                    pad.own_gnd |= gnd
            continue
        if user is None and BONDPAD.search(target):
            if any(prefix.startswith(q) for q in io_prefixes):
                continue
            for port in cells[target].ports:
                nid = port_net(a2n, merged, prefix, port)
                if nid is None or (nid in pads and pads[nid].source == "io_cell"):
                    continue
                pad = pads.setdefault(nid, Pad(nid, "bondpad", target.split("$", 1)[0]))
                if prefix.rstrip("/") not in pad.instances:
                    pad.instances.append(prefix.rstrip("/"))
    if user is not None:
        upads, urails = user
        missing = [k for k in [*upads, *urails] if k not in a2n]
        if missing:
            raise PadsError(f"name(s) match no net: {', '.join(sorted(missing))}")
        for k, cls in upads.items():
            nid = a2n[k]
            pads[nid] = Pad(nid, "user", "user", [k], supply=cls == "supply")
        for k, pol in urails.items():
            (rails_pos if pol == "pos" else rails_gnd).setdefault(a2n[k], "user")
    taps = _well_taps(nl)
    for p in pads.values():
        # A library or bond-pad pad whose net is a well tap is a supply pad, whatever cell it's in.
        if p.source != "user" and p.net in taps:
            p.supply = True
    return PadSet(pads, rails_pos, rails_gnd, black, excluded)


def _well_taps(nl: FlatNetlist) -> set[int]:
    """Nets that are a MOS bulk (an N-well or substrate tap)."""
    return {b for d in nl.devices if d.kind == "M" and (b := d.net("B")) is not None}
