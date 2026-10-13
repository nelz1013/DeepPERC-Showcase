"""SPICE name case.

SPICE model and cell names are case-insensitive. Every comparison of a model or cell name against
a pattern or table key folds both sides with ``fold``; the names themselves stay as the netlist
wrote them, so reports keep the netlist's spelling. Net names are not folded.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase


def fold(name: str) -> str:
    return name.lower()


def fold_globs(globs: Iterable[str]) -> tuple[str, ...]:
    return tuple(fold(g) for g in globs)


def glob_any(name: str, folded_globs: Iterable[str]) -> bool:
    """``name`` matches one of ``folded_globs`` (already folded by ``fold_globs``)."""
    n = fold(name)
    return any(fnmatchcase(n, g) for g in folded_globs)
