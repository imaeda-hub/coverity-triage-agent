"""Mechanical grouping of CIDs that likely share one cause (spec D-52, D-53).

Two CIDs become group candidates when they have the same checker and
the same macro, the same function, or the same line. The worker subagent later
removes CIDs whose cause turns out to be different.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Issue


@dataclass
class WorkItem:
    id: str  # "12345" for a single CID, "G1" for a group
    cids: list[int] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return self.id.startswith("G")


def _keys(issue: Issue) -> list[tuple]:
    keys: list[tuple] = []
    if issue.macro:
        keys.append(("macro", issue.checker, issue.macro))
    if issue.function:
        keys.append(("function", issue.checker, issue.file, issue.function))
    if issue.line is not None:
        keys.append(("line", issue.checker, issue.file, issue.line))
    return keys


def build_work_items(issues: list[Issue], no_grouping: set[int] | None = None) -> list[WorkItem]:
    """Return work items in the order of the given issues (first CID of each item)."""
    items: list[WorkItem] = []
    group_no = 0
    for cids in _clusters(issues, no_grouping or set()):
        if len(cids) == 1:
            items.append(WorkItem(str(cids[0]), cids))
        else:
            group_no += 1
            items.append(WorkItem(f"G{group_no}", cids))
    return items


def _clusters(issues: list[Issue], no_grouping: set[int]) -> list[list[int]]:
    """CIDs joined when they share a key (union-find), in the order of their first CID."""
    parent = {issue.cid: issue.cid for issue in issues}

    def find(cid: int) -> int:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    owner: dict[tuple, int] = {}
    for issue in issues:
        if issue.cid in no_grouping:
            continue
        for key in _keys(issue):
            if key in owner:
                parent[find(issue.cid)] = find(owner[key])
            else:
                owner[key] = issue.cid

    members: dict[int, list[int]] = {}
    for issue in issues:
        members.setdefault(find(issue.cid), []).append(issue.cid)
    return list(members.values())
