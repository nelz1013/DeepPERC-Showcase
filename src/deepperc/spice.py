"""SPICE reader: parse an extracted netlist and flatten its ``.SUBCKT`` hierarchy.

Written for the KLayout gf180mcu_fd_pv LVS deck's ``net_only`` output, whose shape is:

* ``.SUBCKT name port ...`` / ``.ENDS``; continuation lines start with ``+``; comments with ``*``.
* Net names escape ``$`` as ``\\$`` (``\\$I9496``); pin names can be joined with ``|``
  (``DVSS|VSS``). Backslash escapes are removed on read.
* Devices (terminal order is SPICE order, bulk included):

  - ``M<n> d g s b model K=V ...``
  - ``D<n> anode cathode model K=V ...``
  - ``C<n> t0 t1 [value] model K=V ...`` (MOS caps: t0 is the gate)
  - ``R<n> t0 t1 [body] [value] model K=V ...``
  - ``Q<n> c b e [s] model K=V ...``
  - ``X<n> net ... subckt [K=V ...]`` (cell instance)

Every ``K=V`` token is kept verbatim in ``Device.params`` (W/L/AS/AD/PS/PD/A/P/``m``/...).

An ``X`` line whose target is not a ``.SUBCKT`` in the file (Magic-style primitive) becomes a
device when its target name says which kind it is (``nfet``/``pfet``/``nmos``/``pmos`` -> M,
``diode`` -> D, ``cap`` -> C, ``res`` -> R, ``npn``/``pnp`` -> Q), with the line's ``K=V`` tokens
as its ``params`` (Magic writes ``X… nfet_06v0 w= l=``); otherwise it is recorded as
a black box. Other element letters (L, V, I, ...) are counted as skipped, never dropped silently.

Flattening: an instance path is joined with ``/`` (``X$2/X$1/M$3``); a net local to an instance is
named ``<path>/<local>``. A net reached through ports keeps the top-most name as its canonical
name, and every name it has along the hierarchy is kept in ``FlatNetlist.aliases``.

The top cell is the one named by ``top=``; otherwise the uninstantiated ``.SUBCKT`` with the most
flattened devices (a choice between several is recorded in ``warnings``). Lines outside any
``.SUBCKT`` form an implicit top cell named ``<toplevel>``.

## Additions for xschem exports and Magic extractions

* **Quoted/braced parameter values.** A ``K='...'`` or ``K={...}`` token (xschem writes
  ``ps='2*int((nf+2)/2) * (W/nf + 0.18u)'``) tokenizes as one token even though the value holds
  spaces; see ``_tokenize``. Kept verbatim, quotes included, in ``Device.params``.
- **Inline ``$`` comments** (ngspice): a ``$`` token preceded by whitespace/start-of-line and
  followed by whitespace/end-of-line starts a comment to end of line. KLayout's escaped ``\\$I9496``
  and bare tokens like ``X$1``/``$205`` (no space after ``$``) are untouched.
- **Magic primitive wrapper recursion.** An ``X`` instance whose target is a ``.SUBCKT`` already on
  the current expansion stack (an ancestor cell of the same name) and whose name ``_primitive_kind``
  recognises is read as a primitive device, not expanded again; recorded once per target name in
  ``warnings``. A cycle through a name ``_primitive_kind`` doesn't recognise still raises
  ``NetlistError``. The same stack check guards ``_device_counts``/``choose_top``.
- **``.control`` / ``.endc`` blocks** (ngspice test benches) are skipped whole, case-insensitively;
  each skipped block is counted once in ``FlatNetlist.skipped[".control"]``.
- **``merge_split_nets`` (opt-in, default ``False``).** Magic parasitic extractions split a node
  into ``.tN``/``.nN`` segments (``OUT.t81``, ``OUT.t3``, ...). When true, ``flatten()``/
  ``read_netlist()`` map a net matching ``^(.+)\\.(t|n)\\d+$`` to its base name before assigning net
  ids, so all segments of a node become one net. Any ``R``/``C`` element whose two main terminals
  land on the same net after merging is a parasitic split-segment resistor/cap and is dropped,
  counted in ``skipped`` as ``R_split``/``C_split``; a real ``R``/``C`` between different base nets
  is kept. One warning line summarises the merge. Default behaviour is unchanged.
- **``include_root`` (opt-in, default ``None``).** ``read_netlist(path, ..., include_root=...)``
  (and ``parse(text, ..., include_root=...)`` with ``source_dir`` given) follows an ``.include``
  whose resolved path lies inside ``include_root``, parsing it in place (recursive, cycle-safe); one
  not followed (outside the root, unresolvable, or missing) gets a warning
  ``"include not followed: <path> (<reason>)"``. ``.lib`` lines are never followed. Default
  (``include_root=None``) keeps today's behaviour: every ``.include``/``.lib`` line is ignored, no
  warning.

## Name case

* **Cells.** An ``X`` target resolves to the ``.SUBCKT`` of exactly that name, else to the
  one ``.SUBCKT`` whose name equals it case-insensitively (``deepperc.names.fold``).
  ``parse`` rewrites each instance's target to the resolved cell's name once, after the whole file
  (includes too) is read, so every reader of the parsed cells agrees. With two or more case-only
  candidates and no exact one the target stays as written, a black box, and ``flatten`` adds a
  warning (``"<target>: X target matches <n> cells only by case (<names>); read as a black box"``,
  once per target). ``top=`` follows the same rule (``resolve_cell``); an ambiguous or missing top
  is a ``NetlistError``.
* **Models.** ``Device.model`` is kept as written. Callers compare it through
  ``deepperc.names``.
* **Subckt-instance ``m``.** ``flatten`` reads a subckt instance's devices once, whatever its
  ``m``. For each subckt instance whose ``m`` (key in any case) isn't 1, it adds a warning, one line
  per (cell, value as written): ``"<first instance path>: m=<value> on a subckt instance of <cell>;
  its devices are read once (not expanded)"``, plus ``"; <n> instances"`` when there are more. A
  value that isn't a number (``m={nf}``) warns as written. Nested instances each count. ``m`` on a
  device line or an ``X`` primitive is device ``m`` (kept in ``params``), not a subckt instance.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from deepperc.names import fold

#: Terminal roles per element letter, in SPICE order. R and Q carry an optional last terminal.
ROLES: dict[str, tuple[str, ...]] = {
    "M": ("D", "G", "S", "B"),
    "D": ("A", "C"),
    "C": ("P", "N"),
    "R": ("P", "N", "B"),
    "Q": ("C", "B", "E", "S"),
}
_TOPLEVEL = "<toplevel>"
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[a-zA-Z]*$")
#: A ``$`` that starts an inline ngspice comment: preceded by whitespace/start-of-line, followed
#: by whitespace/end-of-line. Escaped ``\$`` and bare tokens like ``$205`` (no following space)
#: don't match.
_DOLLAR_COMMENT = re.compile(r"(?:(?<=\s)|^)\$(?=\s|$)")
#: A Magic parasitic-extraction split-net suffix: ``OUT.t81``, ``a_123.n4`` -> base ``OUT``/
#: ``a_123``.
_SPLIT_NET = re.compile(r"^(.+)\.(?:t|n)\d+$")
_VALUE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)(meg|[fpnumkgt])?[a-z]*$", re.I)
_SCALE = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "meg": 1e6,
    "g": 1e9,
    "t": 1e12,
}


class NetlistError(ValueError):
    """The file is not a netlist this reader can flatten."""


@dataclass(frozen=True, slots=True)
class Device:
    name: str  # hierarchical instance name, e.g. "X$2/X$1/M$3"
    kind: str  # M, D, C, R or Q
    model: str
    roles: tuple[str, ...]  # terminal roles, parallel to ``nets``
    nets: tuple[int, ...]  # flattened net ids
    params: dict[str, str]
    value: str | None = None  # R/C positional value, if any

    def net(self, role: str) -> int | None:
        try:
            return self.nets[self.roles.index(role)]
        except ValueError:
            return None


@dataclass
class FlatNetlist:
    top: str
    names: list[str]  # canonical name per net id
    aliases: list[list[str]]  # every hierarchical name per net id (canonical first)
    devices: list[Device]
    warnings: list[str] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # element letter -> count
    black_boxes: Counter = field(default_factory=Counter)  # undefined X target -> count

    @property
    def n_nets(self) -> int:
        return len(self.names)


@dataclass
class _Cell:
    name: str
    ports: list[str]
    elements: list[tuple[str, list[str], dict[str, str]]] = field(default_factory=list)
    instances: list[tuple[str, list[str], str]] = field(default_factory=list)
    # ``K=V`` tokens of an ``X`` line, by instance head (kept for Magic-style primitives)
    xparams: dict[str, dict[str, str]] = field(default_factory=dict)


def _unescape(tok: str) -> str:
    return re.sub(r"\\(.)", r"\1", tok) if "\\" in tok else tok


def _strip_dollar_comment(line: str) -> str:
    m = _DOLLAR_COMMENT.search(line)
    return line[: m.start()].rstrip() if m else line


def _tokenize(line: str) -> list[str]:
    """Split on whitespace, keeping a ``'...'`` or balanced ``{...}`` span as one token.

    xschem writes parameter expressions like ``ps='2*int((nf+2)/2) * (W/nf + 0.18u)'``: the
    whitespace inside the quotes (or braces) must not split the ``K=V`` token apart.
    """
    toks: list[str] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not line[i].isspace():
            if line[i] == "'":
                i += 1
                while i < n and line[i] != "'":
                    i += 1
                if i < n:
                    i += 1
                continue
            if line[i] == "{":
                depth = 1
                i += 1
                while i < n and depth > 0:
                    if line[i] == "{":
                        depth += 1
                    elif line[i] == "}":
                        depth -= 1
                    i += 1
                continue
            i += 1
        toks.append(line[start:i])
    return toks


def _logical_lines(text: str, skipped: Counter | None = None):
    """Yield logical (continuation-joined) lines, dropping comments and ``.control``/``.endc``.

    ``skipped`` (if given) gets ``[".control"]`` incremented once per skipped control block.
    """
    buf: str | None = None
    in_control = False
    for raw in text.splitlines():
        line = _strip_dollar_comment(raw.rstrip())
        stripped = line.lstrip()
        if not stripped:
            continue
        head_tok = stripped.split(None, 1)[0].lower()
        if in_control:
            if head_tok == ".endc":
                in_control = False
                if skipped is not None:
                    skipped[".control"] += 1
            continue
        if head_tok == ".control":
            in_control = True
            continue
        if stripped.startswith("*"):
            continue
        if stripped.startswith("+"):
            if buf is not None:
                buf += " " + stripped[1:]
            continue
        if buf is not None:
            yield buf
        buf = stripped
    if buf is not None:
        yield buf


def _split(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    pos, params = [], {}
    for t in tokens:
        if "=" in t:
            k, _, v = t.partition("=")
            params[k] = v
        else:
            pos.append(_unescape(t))
    return pos, params


def _is_number(tok: str) -> bool:
    return bool(_NUM.match(tok))


def _resolve_include(
    raw: str, source_dir: Path | None, include_root: Path | None
) -> tuple[Path | None, str | None]:
    """Resolve an ``.include`` argument. Return ``(path, None)`` or ``(None, reason)``."""
    p = raw.strip("\"'")
    if source_dir is None or include_root is None:
        return None, "include_root not set"
    if "$" in p or "<" in p or ">" in p:
        return None, "unresolvable path"
    try:
        resolved = (source_dir / p).resolve()
    except OSError:
        return None, "unresolvable path"
    try:
        resolved.relative_to(include_root)
    except ValueError:
        return None, "outside include_root"
    if not resolved.is_file():
        return None, "missing"
    return resolved, None


def _follow_include(
    toks: list[str],
    source_dir: Path | None,
    include_root: Path | None,
    cells: dict[str, _Cell],
    warnings: list[str],
    skipped: Counter,
    visited: set[Path],
) -> None:
    if len(toks) < 2:
        warnings.append("include not followed: <no path> (no path given)")
        return
    raw = toks[1]
    resolved, reason = _resolve_include(raw, source_dir, include_root)
    if reason is not None:
        warnings.append(f"include not followed: {raw} ({reason})")
        return
    assert resolved is not None
    if resolved in visited:
        return  # already included; cycle-safe
    visited.add(resolved)
    inc_text = resolved.read_text(encoding="utf-8", errors="replace")
    inc_cells = parse(
        inc_text,
        skipped,
        source_dir=resolved.parent,
        include_root=include_root,
        warnings=warnings,
        _visited=visited,
        _resolve=False,
    )
    cells.update(inc_cells)


def parse(
    text: str,
    skipped: Counter | None = None,
    *,
    source_dir: Path | None = None,
    include_root: Path | None = None,
    warnings: list[str] | None = None,
    _visited: set[Path] | None = None,
    _resolve: bool = True,
) -> dict[str, _Cell]:
    """Parse netlist text into cells (``.SUBCKT`` blocks plus an implicit top-level cell).

    ``skipped`` (optional) collects a ``".control"`` count for skipped control blocks. When
    ``include_root`` is given (with ``source_dir``, the including file's directory), a ``.include``
    resolving inside it is read and parsed in place; see the module docstring.
    """
    cells: dict[str, _Cell] = {}
    top = _Cell(_TOPLEVEL, [])
    cur: _Cell | None = None
    for line in _logical_lines(text, skipped):
        toks = _tokenize(line)
        head = toks[0]
        low = head.lower()
        if low == ".subckt":
            if len(toks) < 2:
                raise NetlistError(f".SUBCKT without a name: {line[:80]!r}")
            pos, _ = _split(toks[2:])
            cur = _Cell(toks[1], pos)
            cells[toks[1]] = cur
            continue
        if low == ".ends":
            cur = None
            continue
        if low == ".include":
            if include_root is not None:
                _follow_include(
                    toks,
                    source_dir,
                    include_root,
                    cells,
                    warnings if warnings is not None else [],
                    skipped if skipped is not None else Counter(),
                    _visited if _visited is not None else set(),
                )
            continue
        if low == ".lib":
            continue  # never followed
        if head.startswith("."):
            continue  # .END, .GLOBAL, .PARAM, ... carry no devices in this format
        cell = cur if cur is not None else top
        letter = head[0].upper()
        pos, params = _split(toks[1:])
        if letter == "X":
            if not pos:
                raise NetlistError(f"instance without target: {line[:80]!r}")
            cell.instances.append((head, pos[:-1], pos[-1]))
            if params:
                cell.xparams[head] = params
        else:
            cell.elements.append((head, pos, params))
    if top.elements or top.instances:
        cells[_TOPLEVEL] = top
    if _resolve:
        _resolve_targets(cells)
    return cells


def _fold_index(cells: dict[str, _Cell]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for name in cells:
        idx.setdefault(fold(name), []).append(name)
    return idx


def _resolve_targets(cells: dict[str, _Cell]) -> None:
    """Rewrite an ``X`` target that names no cell exactly but one cell up to case."""
    idx = _fold_index(cells)
    for c in cells.values():
        for k, (head, nets, target) in enumerate(c.instances):
            if target in cells:
                continue
            cands = idx.get(fold(target), [])
            if len(cands) == 1:
                c.instances[k] = (head, nets, cands[0])


def resolve_cell(cells: dict[str, _Cell], name: str) -> str:
    """The cell ``name`` names: exact, else the unique case-insensitive match."""
    if name in cells:
        return name
    cands = _fold_index(cells).get(fold(name), [])
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise NetlistError(f"top cell {name!r} matches {len(cands)} cells only by case: {cands}")
    raise NetlistError(f"top cell {name!r} not in netlist ({len(cells)} cells)")


def _instance_m(params: dict[str, str]) -> str | None:
    """A subckt instance's ``m`` as written, when it isn't 1 (a non-number counts as not 1)."""
    for k, v in params.items():
        if k.lower() == "m":
            mt = _VALUE.match(v.strip())
            if mt is None:
                return v
            x = float(mt.group(1)) * _SCALE.get((mt.group(2) or "").lower(), 1.0)
            return None if x == 1 else v
    return None


#: GF180 resistor models whose names don't say "res" (xschem and Magic write them as ``X`` lines).
_GF_RES = re.compile(r"^([np]polyf_[us]|[np]plus_[us]|rm\d|tm\d)")


def _primitive_kind(target: str) -> str | None:
    t = fold(target)
    if any(k in t for k in ("nfet", "pfet", "nmos", "pmos")) and "cap" not in t:
        return "M"
    if "diode" in t:
        return "D"
    if "npn" in t or "pnp" in t:
        return "Q"
    if "cap" in t:
        return "C"
    if "res" in t or _GF_RES.match(t):
        return "R"
    return None


def _primitive_device_roles(target: str, n_nets: int) -> tuple[str, tuple[str, ...]] | None:
    """If ``target``'s name says which device kind it is, and ``n_nets`` fits that kind."""
    kind = _primitive_kind(target)
    if kind is None:
        return None
    n_roles = len(ROLES[kind])
    if n_nets == n_roles or (kind in "RQ" and n_nets == n_roles - 1):
        return kind, ROLES[kind][:n_nets]
    return None


def _element(
    head: str, pos: list[str], params: dict[str, str]
) -> tuple[str, list[str], str | None, str | None] | None:
    """Return (kind, nets, model, value), or None for an element letter this reader skips."""
    kind = head[0].upper()
    if kind == "M":
        if len(pos) < 5:
            raise NetlistError(f"{head}: MOS needs 4 nets and a model")
        return kind, pos[:4], pos[4], None
    if kind == "D":
        if len(pos) < 3:
            raise NetlistError(f"{head}: diode needs 2 nets and a model")
        return kind, pos[:2], pos[2], None
    if kind in ("R", "C"):
        # [nets...] [value] [model]: the model is the last non-numeric token after >= 2 nets.
        model = value = None
        rest = list(pos)
        if len(rest) > 2 and not _is_number(rest[-1]):
            model = rest.pop()
        if len(rest) > 2 and _is_number(rest[-1]):
            value = rest.pop()
        maxn = 3 if kind == "R" else 2
        if not 2 <= len(rest) <= maxn:
            raise NetlistError(f"{head}: cannot split nets/value/model in {pos}")
        return kind, rest, model or kind, value
    if kind == "Q":
        rest = list(pos)
        while rest and _is_number(rest[-1]):
            rest.pop()  # trailing area factor
        if len(rest) < 4 or len(rest) > 5:
            raise NetlistError(f"{head}: BJT needs 3-4 nets and a model")
        return kind, rest[:-1], rest[-1], None
    return None


def _device_counts(cells: dict[str, _Cell]) -> dict[str, int]:
    memo: dict[str, int] = {}

    def count(name: str, depth: int = 0, stack: tuple[str, ...] = ()) -> int:
        if name in memo:
            return memo[name]
        if name in stack:
            if _primitive_kind(name) is not None:
                return 1  # primitive wrapper recursion, not a real cycle
            raise NetlistError(f"instance recursion through {name!r}")
        if depth > 200:
            raise NetlistError(f"instance recursion through {name!r}")
        c = cells[name]
        n = len(c.elements)
        for _, _, target in c.instances:
            n += count(target, depth + 1, stack + (name,)) if target in cells else 1
        memo[name] = n
        return n

    return {name: count(name) for name in cells}


def choose_top(cells: dict[str, _Cell]) -> tuple[str, list[str]]:
    instantiated = {t for c in cells.values() for _, _, t in c.instances}
    cands = [n for n in cells if n not in instantiated]
    if not cands:
        raise NetlistError("no uninstantiated cell to use as top")
    counts = _device_counts(cells)
    cands.sort(key=lambda n: (-counts[n], n))
    return cands[0], cands[1:]


def flatten(
    cells: dict[str, _Cell], top: str | None = None, *, merge_split_nets: bool = False
) -> FlatNetlist:
    warnings: list[str] = []
    if top is None:
        top, others = choose_top(cells)
        if others:
            shown = ", ".join(others[:5]) + (" ..." if len(others) > 5 else "")
            warnings.append(f"top cell {top!r} chosen; other uninstantiated cells: {shown}")
    else:
        top = resolve_cell(cells, top)

    names: list[str] = []
    aliases: list[list[str]] = []
    devices: list[Device] = []
    skipped: Counter = Counter()
    black: Counter = Counter()
    warned_primitive_recursion: set[str] = set()
    split_merges: dict[str, set[str]] = {}
    multiplied: dict[tuple[str, str], list] = {}  # (cell, m as written) -> [first path, count]
    ambiguous: dict[str, list[str]] = {}
    fidx = _fold_index(cells)

    def new_net(name: str) -> int:
        names.append(name)
        aliases.append([name])
        return len(names) - 1

    def expand(
        cell: _Cell, prefix: str, portmap: dict[str, int], depth: int, path: tuple[str, ...]
    ) -> None:
        if depth > 200:
            raise NetlistError(f"instance recursion through {cell.name!r}")
        local = dict(portmap)

        def gid(net: str) -> int:
            key = net
            if merge_split_nets:
                m = _SPLIT_NET.match(net)
                if m:
                    base = m.group(1)
                    split_merges.setdefault(prefix + base, set()).add(prefix + net)
                    key = base
            g = local.get(key)
            if g is None:
                g = local[key] = new_net(prefix + key)
            if merge_split_nets and key != net:
                alias = prefix + net
                if alias not in aliases[g]:
                    aliases[g].append(alias)
            return g

        for head, pos, params in cell.elements:
            el = _element(head, pos, params)
            if el is None:
                skipped[head[0].upper()] += 1
                continue
            kind, nets, model, value = el
            roles = ROLES[kind][: len(nets)]
            net_ids = tuple(gid(n) for n in nets)
            if (
                merge_split_nets
                and kind in ("R", "C")
                and len(net_ids) >= 2
                and net_ids[0] == net_ids[1]
            ):
                skipped[f"{kind}_split"] += 1
                continue
            devices.append(Device(prefix + head, kind, model, roles, net_ids, params, value))
        for head, nets, target in cell.instances:
            child = cells.get(target)
            if child is None or target in path:
                # A target matching several cells only by case is a black box, even when its
                # name would read as a primitive device.
                cands = fidx.get(fold(target), []) if child is None else []
                prim = None if len(cands) > 1 else _primitive_device_roles(target, len(nets))
                if prim is not None:
                    kind, roles = prim
                    devices.append(
                        Device(
                            prefix + head,
                            kind,
                            target,
                            roles,
                            tuple(gid(n) for n in nets),
                            cell.xparams.get(head, {}),
                        )
                    )
                    if child is not None and target not in warned_primitive_recursion:
                        warned_primitive_recursion.add(target)
                        warnings.append(
                            f"{target!r}: primitive wrapper recursion treated as a device ({kind})"
                        )
                else:
                    if child is not None:
                        raise NetlistError(f"instance recursion through {target!r}")
                    if len(cands) > 1 and target not in ambiguous:
                        ambiguous[target] = cands
                        warnings.append(
                            f"{target}: X target matches {len(cands)} cells only by case "
                            f"({', '.join(cands)}); read as a black box"
                        )
                    black[target] += 1
                    for n in nets:
                        gid(n)
                continue
            if len(nets) != len(child.ports):
                warnings.append(
                    f"{prefix + head}: {len(nets)} nets for {len(child.ports)} ports of {target!r}"
                )
            mv = _instance_m(cell.xparams.get(head, {}))
            if mv is not None:
                hit = multiplied.setdefault((target, mv), [prefix + head, 0])
                hit[1] += 1
            sub_prefix = prefix + head + "/"
            childmap: dict[str, int] = {}
            for port, net in zip(child.ports, nets, strict=False):
                g = gid(net)
                if port in childmap and childmap[port] != g:
                    warnings.append(f"{sub_prefix}{port}: port listed twice with different nets")
                childmap[port] = g
                alias = sub_prefix + port
                if alias != names[g]:
                    aliases[g].append(alias)
            expand(child, sub_prefix, childmap, depth + 1, path + (target,))

    root = cells[top]
    expand(root, "", {p: new_net(p) for p in dict.fromkeys(root.ports)}, 0, (top,))
    for (target, mv), (first, n) in multiplied.items():
        warnings.append(
            f"{first}: m={mv} on a subckt instance of {target}; its devices are read once "
            "(not expanded)" + (f"; {n} instances" if n > 1 else "")
        )
    if merge_split_nets:
        r_drop = skipped.get("R_split", 0)
        c_drop = skipped.get("C_split", 0)
        warnings.append(
            f"split nets merged: {len(split_merges)} base nets; R dropped: {r_drop}; "
            f"C dropped: {c_drop}"
        )
    return FlatNetlist(top, names, aliases, devices, warnings, skipped, black)


def read_netlist(
    path: str | Path,
    top: str | None = None,
    *,
    merge_split_nets: bool = False,
    include_root: str | Path | None = None,
) -> FlatNetlist:
    """Read and flatten a SPICE netlist. Raises ``NetlistError`` or ``OSError``.

    ``merge_split_nets`` and ``include_root`` are opt-in (default off/None = today's behaviour);
    see the module docstring.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    parse_skipped: Counter = Counter()
    include_warnings: list[str] = []
    kwargs: dict = {}
    if include_root is not None:
        kwargs = dict(
            source_dir=path.resolve().parent,
            include_root=Path(include_root).resolve(),
            warnings=include_warnings,
            _visited=set(),
        )
    cells = parse(text, parse_skipped, **kwargs)
    if not cells:
        raise NetlistError(f"{path}: no cells or devices found")
    nl = flatten(cells, top, merge_split_nets=merge_split_nets)
    nl.skipped.update(parse_skipped)
    nl.warnings = include_warnings + nl.warnings
    return nl
