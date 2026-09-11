"""
toxc IR — the typed operator graph.

This is the language-neutral (JSON) intermediate representation that the `.tox`
importer emits and everything downstream (passes, lowering, runtime) consumes.
It is deliberately independent of MLIR: the MLIR `tox` dialect is a *compile
backend* that ingests this same structure. Keeping the IR as plain data lets the
Python reference runtime and the future MLIR/C++ path share one contract.

Model (see docs/design/tox-to-pi.md §4):
  * Operator families carry typed data on their ports: TOP (texture), CHOP
    (channels), SOP (geometry), DAT (table). M1 exercises TOP only.
  * A node has ordered `inputs` (edge = reference to a producing node's output).
  * Feedback is represented by `delay` edges (loop-carried state); the buffers on
    those edges are the "state vector". Not used in the M1 slice but modeled here
    so it doesn't need retrofitting.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

FAMILIES = {"TOP", "CHOP", "SOP", "DAT"}


@dataclass
class Port:
    """A reference to an output of another node (an edge tail)."""

    node: str
    index: int = 0
    delay: int = 0  # 0 = same-frame edge; >0 = feedback edge delayed N frames

    @staticmethod
    def parse(d: Any) -> "Port":
        if isinstance(d, str):
            return Port(node=d)
        return Port(node=d["node"], index=int(d.get("index", 0)), delay=int(d.get("delay", 0)))


@dataclass
class Node:
    id: str
    op: str  # kernel name, e.g. "gaussian_blur"
    family: str = "TOP"
    params: dict = field(default_factory=dict)
    inputs: list[Port] = field(default_factory=list)
    # Filled in by passes:
    out_type: dict | None = None  # e.g. {"w":512,"h":512,"fmt":"rgba8"} after infer_format

    @staticmethod
    def parse(d: dict) -> "Node":
        fam = d.get("family", "TOP")
        if fam not in FAMILIES:
            raise ValueError(f"node {d.get('id')!r}: unknown family {fam!r}")
        return Node(
            id=d["id"],
            op=d["op"],
            family=fam,
            params=dict(d.get("params", {})),
            inputs=[Port.parse(x) for x in d.get("inputs", [])],
            out_type=d.get("out_type"),
        )


@dataclass
class Graph:
    output: str  # id of the sink node
    nodes: dict[str, Node] = field(default_factory=dict)
    version: str = "0.1"
    services: list = field(default_factory=list)  # I/O service specs (OSC/MIDI in)
    chops: list = field(default_factory=list)  # control-rate CHOP DAG feeding exprs

    # -- (de)serialization -----------------------------------------------------
    @staticmethod
    def from_json(text_or_obj: Any) -> "Graph":
        obj = json.loads(text_or_obj) if isinstance(text_or_obj, (str, bytes)) else text_or_obj
        nodes = {}
        for nd in obj["nodes"]:
            n = Node.parse(nd)
            if n.id in nodes:
                raise ValueError(f"duplicate node id {n.id!r}")
            nodes[n.id] = n
        g = Graph(
            output=obj["output"],
            nodes=nodes,
            version=obj.get("version", "0.1"),
            services=list(obj.get("services", [])),
            chops=list(obj.get("chops", [])),
        )
        g.validate()
        return g

    @staticmethod
    def load(path: str) -> "Graph":
        with open(path) as fh:
            return Graph.from_json(fh.read())

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "output": self.output,
            "nodes": [
                {
                    "id": n.id,
                    "op": n.op,
                    "family": n.family,
                    "params": n.params,
                    "inputs": [asdict(p) for p in n.inputs],
                    **({"out_type": n.out_type} if n.out_type else {}),
                }
                for n in self.nodes.values()
            ],
            # I/O services (OSC/MIDI In) live off the TOP render DAG, so they must
            # be serialized explicitly or a round-tripped IR .json loses them (and
            # a deployed graph would ignore its MIDI/OSC input).
            **({"services": self.services} if self.services else {}),
            **({"chops": self.chops} if self.chops else {}),
        }

    # -- structural helpers ----------------------------------------------------
    def validate(self) -> None:
        if self.output not in self.nodes:
            raise ValueError(f"output node {self.output!r} not present")
        for n in self.nodes.values():
            for p in n.inputs:
                if p.node not in self.nodes:
                    raise ValueError(f"{n.id!r} references missing node {p.node!r}")

    def topo_order(self) -> list[str]:
        """Kahn topo sort. Delay (feedback) edges are cut so cycles are legal:
        a delayed input reads last frame's value, so it doesn't constrain order."""
        deps: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        for n in self.nodes.values():
            for p in n.inputs:
                if p.delay == 0:
                    deps[n.id].add(p.node)
        order, ready = [], [nid for nid, d in deps.items() if not d]
        ready.sort()
        seen = set(ready)
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for m in self.nodes:
                if nid in deps[m]:
                    deps[m].discard(nid)
                    if not deps[m] and m not in seen:
                        ready.append(m)
                        seen.add(m)
                        ready.sort()
        if len(order) != len(self.nodes):
            stuck = set(self.nodes) - set(order)
            raise ValueError(
                f"non-delay cycle among {sorted(stuck)} " f"(feedback must use delay>0 edges)"
            )
        return order

    def reachable_from_output(self) -> set[str]:
        seen, stack = set(), [self.output]
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            for p in self.nodes[nid].inputs:
                stack.append(p.node)
        return seen
