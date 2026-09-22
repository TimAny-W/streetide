"""Download a walk graph and turn it into timed street segments."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import networkx as nx

CACHE = Path("data/graphs")

WALK_MPS = 1.4
BIKE_MPS = 4.5


def _speed(mode: str) -> float:
    return BIKE_MPS if mode == "bike" else WALK_MPS


@lru_cache(maxsize=8)
def load_graph(lat: float, lng: float, dist_m: int = 1800):
    import osmnx as ox

    key_lat, key_lng = round(lat, 3), round(lng, 3)
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"walk_{key_lat}_{key_lng}_{dist_m}.graphml"
    if path.exists():
        return ox.load_graphml(path)
    graph = ox.graph_from_point((lat, lng), dist=dist_m, network_type="walk")
    ox.save_graphml(graph, path)
    return graph


def flood(lat: float, lng: float, minutes: float = 15.0, mode: str = "walk") -> dict:
    import osmnx as ox

    minutes = max(1.0, min(float(minutes), 25.0))
    mode = "bike" if mode == "bike" else "walk"
    speed = _speed(mode)
    max_m = speed * minutes * 60.0
    dist = int(min(2800, max(900, max_m * 1.15)))

    graph = load_graph(lat, lng, dist)
    origin = ox.distance.nearest_nodes(graph, lng, lat)
    meters = nx.single_source_dijkstra_path_length(graph, origin, cutoff=max_m, weight="length")

    bands = (minutes / 3.0, 2 * minutes / 3.0, minutes)
    edges = []
    for u, v, data in graph.edges(data=True):
        if u not in meters or v not in meters:
            continue
        t_sec = max(meters[u], meters[v]) / speed
        if t_sec > minutes * 60.0:
            continue
        geom = data.get("geometry")
        if geom is not None:
            coords = [[float(x), float(y)] for x, y in geom.coords]
        else:
            nu, nv = graph.nodes[u], graph.nodes[v]
            coords = [[float(nu["x"]), float(nu["y"])], [float(nv["x"]), float(nv["y"])]]
        if len(coords) < 2:
            continue
        edges.append({"t": round(t_sec, 2), "coords": coords})

    edges.sort(key=lambda e: e["t"])
    origin_node = graph.nodes[origin]
    return {
        "origin": {"lat": float(origin_node["y"]), "lng": float(origin_node["x"])},
        "minutes": minutes,
        "mode": mode,
        "speed_mps": speed,
        "bands_s": [round(b * 60.0, 1) for b in bands],
        "edge_count": len(edges),
        "edges": edges,
    }
