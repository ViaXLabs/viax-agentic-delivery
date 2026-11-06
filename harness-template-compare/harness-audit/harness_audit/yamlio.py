"""YAML loading with line numbers, plus discovery of Harness documents on disk."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_BYTES = 1_000_000  # a pipeline definition larger than this is not a pipeline


class LDict(dict):
    """A dict that remembers the 1-based line on which its mapping started."""

    line = 0


class _Loader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node):
    loader.flatten_mapping(node)
    d = LDict(loader.construct_pairs(node, deep=True))
    d.line = node.start_mark.line + 1
    return d


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass
class Doc:
    path: str  # posix path relative to the repo / snapshot root
    kind: str  # "pipeline" | "template" | "other"
    body: dict


@dataclass
class ParseError:
    path: str
    message: str


def _kind(body: dict) -> str:
    for key in ("pipeline", "template"):
        if isinstance(body.get(key), dict):
            return key
    return "other"


def load_docs(root: Path, base: Path | None = None) -> tuple[list[Doc], list[ParseError]]:
    """Parse every YAML file under *base* (default *root*); report paths relative to *root*.

    Unparseable files are returned as errors, never raised: a broken pipeline in one
    team's repo must not stop the audit, and it is itself a finding.
    """
    base = base or root
    docs: list[Doc] = []
    errors: list[ParseError] = []
    for f in sorted(base.rglob("*")):
        if f.suffix.lower() not in (".yaml", ".yml") or ".git" in f.parts or not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if f.stat().st_size > MAX_BYTES:
            errors.append(ParseError(rel, f"skipped: larger than {MAX_BYTES} bytes"))
            continue
        try:
            for body in yaml.load_all(f.read_text(encoding="utf-8"), Loader=_Loader):
                if isinstance(body, dict):
                    docs.append(Doc(rel, _kind(body), body))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            errors.append(ParseError(rel, " ".join(str(exc).split())[:300]))
    return docs, errors
