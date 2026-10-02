"""Citation existence checks. Grammar fixtures are shared with the Web parser."""

from __future__ import annotations

import re
from collections.abc import Iterable


def citation_numbers(markdown: str) -> list[int]:
    excluded: list[tuple[int, int]] = []
    reference_definitions: set[str] = set()
    offset = 0
    fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            excluded.append((offset, offset + len(line)))
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not line[marker.end() :].strip()
            ):
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
            excluded.append((offset, offset + len(line)))
        elif re.match(r"^(?: {4}|\t)|^ {0,3}\[[^\]]+\]:", line):
            excluded.append((offset, offset + len(line)))
            definition = re.match(r"^ {0,3}\[([^\]]+)\]:", line)
            if definition:
                reference_definitions.add(definition[1])
        offset += len(line)

    def escaped(index: int) -> bool:
        count = 0
        while index > 0 and markdown[index - 1] == chr(92):
            count += 1
            index -= 1
        return count % 2 == 1

    result: list[int] = []
    i = 0
    while i < len(markdown):
        start = i
        i += 1
        if any(left <= start < right for left, right in excluded) or escaped(start):
            continue
        if markdown[start] == "`":
            run = re.match(r"`+", markdown[start:])[0]
            i = start + len(run)
            for delimiter in re.finditer(r"`+", markdown[i:]):
                if len(delimiter[0]) == len(run):
                    i += delimiter.end()
                    break
            continue
        if markdown[start] != "[":
            continue
        depth = 1
        close = start + 1
        while close < len(markdown) and depth:
            if not escaped(close):
                if markdown[close] == "[":
                    depth += 1
                elif markdown[close] == "]":
                    depth -= 1
            close += 1
        if depth:
            continue
        label = markdown[start + 1 : close - 1]
        if label in reference_definitions:
            i = close
            continue
        if markdown[close : close + 1] == "(":
            nesting = 1
            end = close + 1
            while end < len(markdown) and nesting:
                if not escaped(end):
                    if markdown[end] == "(":
                        nesting += 1
                    elif markdown[end] == ")":
                        nesting -= 1
                end += 1
            i = end
            continue
        next_label = re.match(r"\[([^\]]*)\]", markdown[close:])
        if next_label and not (
            re.fullmatch(r"[1-9][0-9]*", label) and re.fullmatch(r"[1-9][0-9]*", next_label[1])
        ):
            i = close + next_label.end()
            continue
        i = close
        if (start > 0 and markdown[start - 1] == "!") or not re.fullmatch(r"[1-9][0-9]*", label):
            continue
        number = int(label) if len(label) <= 15 else 10**15
        if number not in result:
            result.append(number)
    return result


def cited_material_numbers(markdown: str, allowed: Iterable[int]) -> tuple[list[int], list[int]]:
    allowed_set = set(allowed)
    numbers = citation_numbers(markdown)
    return (
        [number for number in numbers if number in allowed_set],
        [number for number in numbers if number not in allowed_set],
    )
