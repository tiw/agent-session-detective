# src/agent_session_detective/ir/library.py
"""The empirical co-load graph: which skills actually load together.

Built from the document alone — the observed counterpart to the declared
``requires:`` graph in the catalog. An edge is two skills attached (body /
stub / execution, never listing alone) in the same agent scope; its weight
is the number of agents that showed the pair, and those agent ids are the
evidence. ``diff_edges`` compares the two graphs: declared-but-never-seen
pairs are over-declaration, repeatedly-seen undeclared pairs are the
implicit dependencies the catalog never wrote down.

Everything here is a fact about observed loads; "should these load
together" is a library-design question this module deliberately leaves
elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .schema import AuditDocument
from .skill_tree import ATTACHMENT_KINDS


@dataclass
class CoLoadEdge:
    """Two skills attached in the same agent scope, with the evidence.

    ``a`` < ``b`` lexicographically; ``weight`` is the number of distinct
    agents that showed the pair; ``agent_ids`` is sorted.
    """

    a: str
    b: str
    weight: int
    agent_ids: List[str]

    def to_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "weight": self.weight,
            "agent_ids": self.agent_ids,
        }


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def co_load_graph(document: AuditDocument) -> List[CoLoadEdge]:
    """Observed co-loads, heaviest first.

    Grouping is by agent scope: one agent contributing the same skill three
    times still counts once, while a second agent showing the pair raises
    the weight to two — the weight measures agreement between agents, not
    observation volume.
    """
    loaded: Dict[str, set] = {}
    for skill in document.skills:
        for observation in skill.observations:
            if observation.kind in ATTACHMENT_KINDS:
                loaded.setdefault(observation.agent_id, set()).add(
                    skill.skill_id)

    agents_by_pair: Dict[Tuple[str, str], List[str]] = {}
    for agent_id, skill_ids in loaded.items():
        ordered = sorted(skill_ids)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                agents_by_pair.setdefault(
                    (ordered[i], ordered[j]), []).append(agent_id)

    graph = [
        CoLoadEdge(a=pair[0], b=pair[1],
                   weight=len(agent_ids), agent_ids=sorted(agent_ids))
        for pair, agent_ids in agents_by_pair.items()
    ]
    graph.sort(key=lambda e: (-e.weight, e.a, e.b))
    return graph


def diff_edges(
    declared: Dict[str, List[str]],
    edges: List[CoLoadEdge],
    min_weight: int = 2,
) -> dict:
    """Declared requires vs observed co-loads, in three buckets.

    ``over_declared``: declared pairs no agent ever showed together.
    ``implicit``: observed pairs the catalog never declares, once at least
    ``min_weight`` agents agree — a single sighting can be coincidence.
    ``consistent``: declared pairs the observation confirms.
    """
    declared_pairs = set()
    for name, requires in declared.items():
        for target in requires:
            if name != target:
                declared_pairs.add(_pair(name, target))
    observed = {_pair(e.a, e.b): e for e in edges}

    over_declared = [
        {"a": pair[0], "b": pair[1]}
        for pair in sorted(declared_pairs - set(observed))
    ]
    consistent = [
        {"a": pair[0], "b": pair[1], "weight": observed[pair].weight}
        for pair in sorted(declared_pairs & set(observed))
    ]
    implicit = [
        {"a": pair[0], "b": pair[1], "weight": e.weight,
         "agent_ids": e.agent_ids}
        for pair, e in observed.items()
        if pair not in declared_pairs and e.weight >= min_weight
    ]
    implicit.sort(key=lambda row: (-row["weight"], row["a"], row["b"]))
    return {
        "over_declared": over_declared,
        "implicit": implicit,
        "consistent": consistent,
        "totals": {
            "declared": len(declared_pairs),
            "observed": len(observed),
            "implicit_min_weight": min_weight,
        },
    }


def join_declared_observed(
    declared: Dict[str, List[str]],
    edges: List[CoLoadEdge],
) -> Tuple[List[CoLoadEdge], dict]:
    """Rewrite observed ids to the declared name they uniquely belong to.

    The two name spaces differ: the catalog carries bare names while
    observed ids are often namespaced. The rule mirrors D1
    (``merge_skill_identities`` in ``skills.py``): an observed id joins a
    declared name only when the match is unique — case-insensitive exact
    first, else a single ``":"``-suffixed twin. A name that appears
    un-namespaced keeps its twins as separate entities, and ambiguous ids
    are counted, never guessed. Unjoined ids pass through unchanged so
    their pairs stay visible as undeclared.
    """
    observed_ids = sorted({e.a for e in edges} | {e.b for e in edges})
    lowered = {name.lower(): name for name in declared}
    exact = {oid for oid in observed_ids if oid.lower() in lowered}
    resolved: Dict[str, str] = {oid: lowered[oid.lower()] for oid in exact}
    ambiguous: List[str] = []
    twins_by_declared: Dict[str, List[str]] = {}
    for observed in observed_ids:
        if observed in resolved:
            continue
        low = observed.lower()
        candidates = [lowered[key] for key in lowered
                      if low.endswith(":" + key)]
        if len(candidates) == 1:
            twins_by_declared.setdefault(candidates[0], []).append(observed)
        elif len(candidates) > 1:
            ambiguous.append(observed)
    exact_lowered = {oid.lower() for oid in exact}
    for name, twins in twins_by_declared.items():
        if name.lower() in exact_lowered:
            continue
        if len(twins) == 1:
            resolved[twins[0]] = name
        else:
            ambiguous.extend(twins)

    joined = []
    for edge in edges:
        a, b = _pair(resolved.get(edge.a, edge.a),
                     resolved.get(edge.b, edge.b))
        joined.append(CoLoadEdge(a=a, b=b, weight=edge.weight,
                                 agent_ids=list(edge.agent_ids)))
    joined.sort(key=lambda e: (-e.weight, e.a, e.b))
    report = {
        "renamed": {
            raw: name for raw, name in sorted(resolved.items())
            if raw != name
        },
        "ambiguous": sorted(set(ambiguous)),
        "note": ("observed ids join a declared name only when the match is "
                 "unique — exact first, else a lone ':'-suffixed twin; "
                 "ambiguous ids and twins of an un-namespaced name stay "
                 "unjoined"),
    }
    return joined, report


def library_report(
    document: AuditDocument,
    declared: Dict[str, List[str]],
    min_weight: int = 2,
) -> dict:
    """The wiring-facing library block: raw edges, identity join, diff.

    ``build_analyses`` stays document-pure; the CLI and web layers call
    this with the loaded catalog's ``{name: requires}`` map so the two
    name spaces are joined before ``diff_edges`` compares pairs.
    """
    edges = co_load_graph(document)
    joined, join_report = join_declared_observed(declared, edges)
    return {
        "edges": [e.to_dict() for e in edges],
        "join": join_report,
        "diff": diff_edges(declared, joined, min_weight=min_weight),
    }
