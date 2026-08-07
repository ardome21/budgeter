"""Proposing merchants that are probably the same place.

Deliberately suggestive, never automatic. Wrongly merging two shops silently
corrupts every total they appear in and cannot be undone; leaving one shop
split is a click to fix. So a human confirms every merge.

The rule is deliberately one thing: **two merchants are proposed when their
first word is the same brand.** In a bank descriptor the first token is the
shop — 'RHINO MARKET & DELI CHARLOTTE NC', 'Rhino Mart', 'Rhino'. Everything
after it is a branch, a service, or noise.

That means a proposal answers "these share a brand — are they one place?", not
"these are the same". 'Uber Eats' and 'Uber Trip' are both proposed and are
*not* one place, which is exactly why the reviewer picks members individually
rather than accepting a group wholesale.
"""

# The brand has to be distinctive enough to mean something. 'Bar Crawl' and
# 'Bar Garden' share 'bar', which says nothing about them being one place.
MIN_BRAND_LENGTH = 4


def tokens(name: str) -> list[str]:
    return [t for t in name.lower().replace("&", " ").split() if t]


def edit_distance_at_most_one(a: str, b: str) -> bool:
    """True for typos: 'teeter'/'teater', 'target'/'targer'. Bounded at 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    return any(shorter == longer[:i] + longer[i + 1 :] for i in range(len(longer)))


def likely_same(a: str, b: str) -> bool:
    """Whether two canonical names share a brand worth reviewing together."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    brand_a, brand_b = ta[0], tb[0]
    if len(brand_a) < MIN_BRAND_LENGTH or len(brand_b) < MIN_BRAND_LENGTH:
        return False
    return edit_distance_at_most_one(brand_a, brand_b)


def group_names(names: list[str], rejected: set[tuple[str, str]]) -> list[list[str]]:
    """Connected components of the 'shares a brand' relation.

    A rejected pair breaks the link between those two names, so saying "no"
    genuinely removes the proposal rather than hiding it for a session.
    """
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if tuple(sorted((a, b))) in rejected:
                continue
            if likely_same(a, b):
                union(a, b)

    clusters: dict[str, list[str]] = {}
    for name in names:
        clusters.setdefault(find(name), []).append(name)
    return [sorted(group) for group in clusters.values() if len(group) > 1]
