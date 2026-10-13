"""Device-limit rows and their provenance.

A limit table is a CSV with one row per (devices, quantity) limit. Every row carries a ``source``
tag saying where its value comes from:

* ``pdk-cited``: stated in the PDK's own documentation; ``citation`` names the file and line.
* ``user-asserted``: entered by the user, with a justification in ``citation``.
* ``sim-derived``: simulated from the netlist with the PDK's own models. Not PDK-documented, so a
  verdict that rests on it is capped at WARN in both directions (never a FAIL, never a clean PASS).
* ``deck-cited``: taken from a verification deck rather than the PDK documents. A FAIL that rests
  on it is capped at WARN.
* ``none``: the PDK states no value; the row exists so the gap is reported, not silently skipped.

Only ``pdk-cited`` and ``user-asserted`` values can back a FAIL.

Precedence, for one device and one parameter: user-asserted rows replace pdk-cited rows, which
replace sim-derived rows, which replace deck-cited rows; within the winning source, the lowest
value (the conservative one).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path

from deepperc.names import fold_globs, glob_any


class Source(StrEnum):
    PDK_CITED = "pdk-cited"
    USER_ASSERTED = "user-asserted"
    SIM_DERIVED = "sim-derived"
    DECK_CITED = "deck-cited"
    NONE = "none"


#: Sources whose values may back a FAIL.
FAIL_SOURCES = (Source.PDK_CITED, Source.USER_ASSERTED)
_RANK = {Source.USER_ASSERTED: 0, Source.PDK_CITED: 1, Source.SIM_DERIVED: 2, Source.DECK_CITED: 3}

HEADER = (
    "pdk", "devices", "quantity", "limit_kind", "min", "max", "unit", "conditions", "source",
    "citation", "device_mapping", "note",
)  # fmt: skip
PARAMS = ("Vt1", "Vt2", "V_clamp")
BV_QUANTITY = "|V_gate-any|"
_LOWER_BOUND = re.compile(r"(?:^|;)\s*lower_bound\s*(?:;|$)")
_TOKEN = {p: re.compile(rf"\b{p}\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*V\b") for p in PARAMS}
_GLOB_TOKEN = re.compile(r"^[A-Za-z0-9_*?\[\]]+$")

DEFAULT_TABLE = "gf180_esd_limits.csv"


class LimitsError(ValueError):
    """A limit table that can't be used."""


@dataclass(frozen=True)
class LimitRow:
    table: str
    line: int
    pdk: str
    devices: str
    quantity: str
    limit_kind: str
    max: float | None
    unit: str
    conditions: str
    source: Source
    citation: str
    note: str
    globs: tuple[str, ...] = field(default=(), compare=False)

    @property
    def rid(self) -> str:
        return f"{self.table}:{self.line}"

    @property
    def lower_bound(self) -> bool:
        """The row states a value the real limit is at least (e.g. a parametric BVox)."""
        return bool(_LOWER_BOUND.search(self.conditions))

    def matches(self, name: str) -> bool:
        return glob_any(name, self.globs)

    def param(self, p: str) -> float | None:
        """Clamp parameter ``p``: ``max`` when ``quantity`` is ``p`` (the user form), else a
        ``p=<n> V`` token in ``conditions`` (the PDK form). A quoted or ambiguous value
        (``Vt2='3.5V/6V'``) gives None."""
        if self.quantity == p:
            return self.max
        m = _TOKEN[p].search(self.conditions)
        return float(m.group(1)) if m else None


def _num(text: str, where: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise LimitsError(f"{where}: not a number: {text!r}") from exc


def load(path: str | Path) -> list[LimitRow]:
    """Read and validate one limit table."""
    path = Path(path)
    rows: list[LimitRow] = []
    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        missing = [h for h in HEADER if h not in (rd.fieldnames or [])]
        if missing:
            raise LimitsError(f"{path}: missing column(s): {', '.join(missing)}")
        for line, rec in enumerate(rd, start=2):
            where = f"{path.name}:{line}"
            try:
                src = Source(rec["source"].strip())
            except ValueError as exc:
                raise LimitsError(f"{where}: unknown source {rec['source']!r}") from exc
            if src is Source.USER_ASSERTED and not rec["citation"].strip():
                raise LimitsError(f"{where}: a user-asserted row needs a justification in citation")
            toks = [t.strip() for t in rec["devices"].split(",")]
            if not all(_GLOB_TOKEN.match(t) for t in toks):
                raise LimitsError(f"{where}: devices must be comma-separated globs")
            rows.append(
                LimitRow(
                    table=path.stem,
                    line=line,
                    pdk=rec["pdk"].strip(),
                    devices=rec["devices"],
                    quantity=rec["quantity"].strip(),
                    limit_kind=rec["limit_kind"].strip(),
                    max=_num(rec["max"], where),
                    unit=rec["unit"].strip(),
                    conditions=rec["conditions"],
                    source=src,
                    citation=rec["citation"],
                    note=rec["note"],
                    globs=fold_globs(toks),
                )
            )
    return rows


def default_table() -> Path:
    return Path(str(resources.files("deepperc.data").joinpath(DEFAULT_TABLE)))


def load_all(extra: list[str | Path] | None = None) -> list[LimitRow]:
    """The bundled GF180 table first, then each user table in order."""
    rows = load(default_table())
    for p in extra or []:
        rows.extend(load(p))
    return rows


@dataclass
class Pick:
    """The value that binds for one (device, parameter), and every row considered."""

    value: float | None
    row: LimitRow | None
    considered: list[dict]


def pick(cands: list[tuple[LimitRow, float | None]], role: str, param: str | None) -> Pick:
    """Apply precedence: the best-ranked source with a value wins; within it, the lowest value."""
    valued = [(r, v) for r, v in cands if v is not None and r.source in _RANK]
    best_rank = min((_RANK[r.source] for r, _ in valued), default=None)
    win = [(r, v) for r, v in valued if _RANK[r.source] == best_rank]
    row, value = min(win, key=lambda rv: rv[1]) if win else (None, None)
    considered = [
        {
            "row": r.rid,
            "role": role,
            "param": param,
            "value": v,
            "source": str(r.source),
            "citation": r.citation,
            "overridden": v is not None and best_rank is not None and _RANK.get(r.source, 9) > best_rank,
            **({"why": "unparsed"} if v is None and r.source is not Source.NONE else {}),
        }
        for r, v in cands
    ]
    return Pick(value, row, considered)


class Limits:
    """Lookups the ESD-window check makes against the loaded rows."""

    def __init__(self, rows: list[LimitRow], pdk: str = "gf180"):
        self.rows = [r for r in rows if r.pdk == pdk]
        self._tlp = [r for r in self.rows if r.limit_kind in ("esd_tlp", "none")]
        self._bv = [
            r for r in self.rows if r.limit_kind == "esd_breakdown" and r.quantity == BV_QUANTITY
        ]
        self._clamp = [r for r in self._tlp if r.quantity == "V_clamp" or r.param("V_clamp")]
        self._cache: dict[tuple, Pick] = {}

    def clamp_param(self, model: str, p: str) -> Pick:
        """``Vt1``/``Vt2`` of an ESD device model (snapback MOS or diode)."""
        key = ("tlp", model.lower(), p)
        if key not in self._cache:
            cands = [
                (r, r.param(p))
                for r in self._tlp
                if r.matches(model) and r.quantity != "V_clamp"
                and (r.param(p) is not None or r.limit_kind == "none" or p in r.conditions)
            ]
            self._cache[key] = pick(cands, "stress", p)
        return self._cache[key]

    def v_clamp(self, cell: str) -> Pick:
        """``V_clamp`` of a power-clamp cell (matched by cell name, not device model)."""
        key = ("vc", cell.lower())
        if key not in self._cache:
            cands = [(r, r.param("V_clamp")) for r in self._clamp if r.matches(cell)]
            self._cache[key] = pick(cands, "stress", "V_clamp")
        return self._cache[key]

    def is_power_clamp(self, cell: str) -> bool:
        return any(r.matches(cell) for r in self._clamp)

    def bv(self, model: str) -> Pick:
        """ESD-timescale gate-oxide breakdown of a MOS or MOS-cap model."""
        key = ("bv", model.lower())
        if key not in self._cache:
            cands = [(r, r.max) for r in self._bv if r.matches(model)]
            self._cache[key] = pick(cands, "breakdown", None)
        return self._cache[key]
