"""Internal line-filtering helpers shared by built-in plugins (ignored by discovery)."""

from typing import Callable, List


def strip_markdown_items(
    lines: List[str], predicate: Callable[[str], bool]
) -> List[str]:
    """Remove top-level list items matching ``predicate(line)`` along with indented continuation lines."""
    out: List[str] = []
    skipping = False
    for line in lines:
        stripped = line.lstrip()
        is_indented = len(line) > len(stripped) and stripped != ""
        if skipping:
            if is_indented:
                continue
            skipping = False
        if not is_indented and stripped and predicate(stripped):
            skipping = True
            continue
        out.append(line)
    return out


def strip_markdown_section(lines: List[str], heading_prefix: str) -> List[str]:
    """Remove an entire ``## ...`` section starting with ``heading_prefix`` until the next ``## `` or EOF."""
    out: List[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                in_section = False
            if stripped.startswith(heading_prefix):
                in_section = True
                continue
        if not in_section:
            out.append(line)
    return out
