"""
Shared graph read/query logic for CLI, MCP handlers, and GraphifyTool.

Keeps one implementation so subprocess + interpreter mismatches are not required
for agent-facing graph tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph

from .analyze import god_nodes
from .lexical_query import terms_from_question


def _anchor_terms(phrase: str) -> list[str]:
    """Terms for matching a path endpoint; camelCase-aware with whitespace fallback."""
    t = terms_from_question(phrase)
    if t:
        return t
    return [x.lower() for x in phrase.split() if len(x) > 1]
from .serve import (
    _bfs,
    _communities_from_graph,
    _dfs,
    _find_node,
    _score_nodes,
    _subgraph_to_text,
)


def load_graph_nx(graph_path: str | Path) -> nx.Graph:
    """Load graph.json into a NetworkX graph. Raises on missing file or parse errors."""
    gp = Path(graph_path).resolve()
    if not gp.exists():
        raise FileNotFoundError(f"Graph file not found: {gp}")
    if gp.suffix != ".json":
        raise ValueError(f"Graph path must be a .json file: {gp}")
    raw = json.loads(gp.read_text(encoding="utf-8"))
    try:
        return json_graph.node_link_graph(raw, edges="links")
    except TypeError:
        return json_graph.node_link_graph(raw)


def run_graph_query(
    G: nx.Graph,
    question: str,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
) -> str:
    depth = max(1, min(int(depth), 6))
    terms = terms_from_question(question)
    scored = _score_nodes(G, terms)
    start_nodes = [nid for _, nid in scored[:5]]
    if not start_nodes:
        gods = god_nodes(G, top_n=5)
        if not gods:
            return (
                "No keyword overlap with graph labels or file paths, and no hub nodes "
                "were available for fallback."
            )
        start_nodes = [g["id"] for g in gods]
        fb_depth = min(2, depth)
        nodes, edges = (
            _dfs(G, start_nodes, fb_depth)
            if mode == "dfs"
            else _bfs(G, start_nodes, fb_depth)
        )
        header = (
            "No keyword overlap between your question and graph node labels or file paths.\n"
            "Showing a neighborhood of the most connected hub nodes instead "
            "(try symbols or path segments from your codebase, e.g. module or class names).\n\n"
        )
        return header + _subgraph_to_text(G, nodes, edges, token_budget)

    nodes, edges = _dfs(G, start_nodes, depth) if mode == "dfs" else _bfs(G, start_nodes, depth)
    labels = [G.nodes[n].get("label", n) for n in start_nodes]
    header = (
        f"Traversal: {mode.upper()} depth={depth} | Start: {labels} | {len(nodes)} nodes\n\n"
    )
    return header + _subgraph_to_text(G, nodes, edges, token_budget)


def run_explain(G: nx.Graph, label: str) -> str:
    matches = _find_node(G, label)
    if not matches:
        return f"No node matching '{label}' found."
    nid = matches[0]
    d = G.nodes[nid]
    lines = [
        f"Node: {d.get('label', nid)}",
        f"  ID:        {nid}",
        f"  Source:    {d.get('source_file', '')} {d.get('source_location', '')}".rstrip(),
        f"  Type:      {d.get('file_type', '')}",
        f"  Community: {d.get('community', '')}",
        f"  Degree:    {G.degree(nid)}",
    ]
    neighbors = list(G.neighbors(nid))
    if neighbors:
        lines.append(f"\nConnections ({len(neighbors)}):")
        for nb in sorted(neighbors, key=lambda n: G.degree(n), reverse=True)[:20]:
            raw = G.edges[nid, nb]
            edata = next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw
            rel = edata.get("relation", "")
            conf = edata.get("confidence", "")
            lines.append(f"  --> {G.nodes[nb].get('label', nb)} [{rel}] [{conf}]")
        if len(neighbors) > 20:
            lines.append(f"  ... and {len(neighbors) - 20} more")
    return "\n".join(lines)


def run_shortest_path(
    G: nx.Graph,
    source: str,
    target: str,
    max_hops: int = 8,
) -> str:
    src_scored = _score_nodes(G, _anchor_terms(source))
    tgt_scored = _score_nodes(G, _anchor_terms(target))
    if not src_scored:
        return f"No node matching source '{source}' found."
    if not tgt_scored:
        return f"No node matching target '{target}' found."
    src_nid, tgt_nid = src_scored[0][1], tgt_scored[0][1]
    try:
        path_nodes = nx.shortest_path(G, src_nid, tgt_nid)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return (
            f"No path found between '{G.nodes[src_nid].get('label', src_nid)}' and "
            f"'{G.nodes[tgt_nid].get('label', tgt_nid)}'."
        )
    hops = len(path_nodes) - 1
    if hops > max_hops:
        return f"Path exceeds max_hops={max_hops} ({hops} hops found)."
    segments: list[str] = []
    for i in range(len(path_nodes) - 1):
        u, v = path_nodes[i], path_nodes[i + 1]
        raw = G.edges[u, v]
        edata = next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw
        rel = edata.get("relation", "")
        conf = edata.get("confidence", "")
        conf_str = f" [{conf}]" if conf else ""
        if i == 0:
            segments.append(G.nodes[u].get("label", u))
        segments.append(f"--{rel}{conf_str}--> {G.nodes[v].get('label', v)}")
    return f"Shortest path ({hops} hops):\n  " + " ".join(segments)


def format_god_nodes_report(G: nx.Graph, top_n: int = 10) -> str:
    nodes = god_nodes(G, top_n=int(top_n))
    if not nodes:
        return "No hub nodes found (graph may be empty or only file-level nodes)."
    lines = ["God nodes (most connected non-file entities):"]
    lines += [f"  {i}. {n['label']} — {n['degree']} edges" for i, n in enumerate(nodes, 1)]
    return "\n".join(lines)


def run_graph_stats(G: nx.Graph) -> str:
    communities = _communities_from_graph(G)
    confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
    total = len(confs) or 1
    return (
        f"Nodes: {G.number_of_nodes()}\n"
        f"Edges: {G.number_of_edges()}\n"
        f"Communities: {len(communities)}\n"
        f"EXTRACTED: {round(confs.count('EXTRACTED') / total * 100)}%\n"
        f"INFERRED: {round(confs.count('INFERRED') / total * 100)}%\n"
        f"AMBIGUOUS: {round(confs.count('AMBIGUOUS') / total * 100)}%\n"
    )


def run_get_neighbors(G: nx.Graph, label: str, relation_filter: str = "") -> str:
    matches = _find_node(G, label)
    if not matches:
        return f"No node matching '{label}' found."
    nid = matches[0]
    rel_filter = relation_filter.lower()
    lines = [f"Neighbors of {G.nodes[nid].get('label', nid)}:"]
    for neighbor in G.neighbors(nid):
        raw = G.edges[nid, neighbor]
        d = next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw
        rel = d.get("relation", "")
        if rel_filter and rel_filter not in rel.lower():
            continue
        lines.append(
            f"  --> {G.nodes[neighbor].get('label', neighbor)} [{rel}] [{d.get('confidence', '')}]"
        )
    return "\n".join(lines)


def run_get_community(G: nx.Graph, community_id: int) -> str:
    communities = _communities_from_graph(G)
    nodes = communities.get(int(community_id), [])
    if not nodes:
        return f"Community {community_id} not found."
    lines = [f"Community {community_id} ({len(nodes)} nodes):"]
    for n in nodes:
        d = G.nodes[n]
        lines.append(f"  {d.get('label', n)} [{d.get('source_file', '')}]")
    return "\n".join(lines)


def dispatch_tool(G: nx.Graph, tool_name: str, arguments: dict[str, Any]) -> str:
    """Dispatch MCP-style tool names to runtime implementations."""
    if tool_name == "query_graph":
        return run_graph_query(
            G,
            arguments.get("question", ""),
            arguments.get("mode", "bfs"),
            int(arguments.get("depth", 3)),
            int(arguments.get("token_budget", 2000)),
        )
    if tool_name == "shortest_path":
        return run_shortest_path(
            G,
            arguments.get("source", ""),
            arguments.get("target", ""),
            int(arguments.get("max_hops", 8)),
        )
    if tool_name in ("get_node", "explain"):
        label = arguments.get("label") or arguments.get("node_label", "")
        return run_explain(G, str(label))
    if tool_name == "god_nodes":
        return format_god_nodes_report(G, int(arguments.get("top_n", 10)))
    if tool_name == "graph_stats":
        return run_graph_stats(G)
    if tool_name == "get_neighbors":
        return run_get_neighbors(
            G,
            arguments.get("label", ""),
            str(arguments.get("relation_filter", "")),
        )
    if tool_name == "get_community":
        return run_get_community(G, int(arguments.get("community_id", 0)))
    return f"Unknown graph tool: {tool_name}"
