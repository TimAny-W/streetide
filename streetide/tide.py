"""Timed street flood from OSM Map API, with a schematic fallback."""
from __future__ import annotations

import logging
import math
import time
from functools import lru_cache
from pathlib import Path

import networkx as nx

from streetide.osm_map import fetch_osm_graph

log = logging.getLogger("streetide")
CACHE = Path("data/graphs")

WALK_MPS = 1.4
BIKE_MPS = 4.5


def _speed(mode: str) -> float:
    return BIKE_MPS if mode == "bike" else WALK_MPS


def _grid_graph(lat: float, lng: float, dist_m: int, step_m: float = 80.0):
    graph = nx.MultiDiGraph()
    n = max(8, min(int(dist_m / step_m), 20))
    dlat = step_m / 111_320.0
    dlng = step_m / (111_320.0 * max(0.25, math.cos(math.radians(lat))))
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            graph.add_node((i, j), y=lat + i * dlat, x=lng + j * dlng)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            for di, dj in ((1, 0), (0, 1)):
                nxt = (i + di, j + dj)
                if nxt not in graph:
                    continue
                graph.add_edge((i, j), nxt, length=step_m)
                graph.add_edge(nxt, (i, j), length=step_m)
    log.warning("using schematic grid (%s nodes)", graph.number_of_nodes())
    return graph


def nearest_node(graph, lng: float, lat: float):
    best, best_d = None, float("inf")
    cos = max(0.25, math.cos(math.radians(lat)))
    for node, data in graph.nodes(data=True):
        if graph.degree(node) < 1:
            continue
        dx = (float(data["x"]) - lng) * 111_320.0 * cos
        dy = (float(data["y"]) - lat) * 111_320.0
        dist = dx * dx + dy * dy
        if dist < best_d:
            best, best_d = node, dist
    if best is None:
        raise RuntimeError("walk graph has no connected street nodes")
    return best


def _xy(lat0, lng0, lat, lng):
    cos = max(0.25, math.cos(math.radians(lat0)))
    return (lng - lng0) * 111_320.0 * cos, (lat - lat0) * 111_320.0


def _ll(lat0, lng0, x, y):
    cos = max(0.25, math.cos(math.radians(lat0)))
    return lat0 + y / 111_320.0, lng0 + x / (111_320.0 * cos)


def snap_to_road(graph, lat: float, lng: float):
    """Project the click onto the nearest car-road segment."""
    best = None
    seen = set()
    px, py = _xy(lat, lng, lat, lng)
    for u, v, data in graph.edges(data=True):
        key = tuple(sorted((str(u), str(v))))
        if key in seen:
            continue
        seen.add(key)
        nu, nv = graph.nodes[u], graph.nodes[v]
        ax, ay = _xy(lat, lng, float(nu["y"]), float(nu["x"]))
        bx, by = _xy(lat, lng, float(nv["y"]), float(nv["x"]))
        abx, aby = bx - ax, by - ay
        ab2 = abx * abx + aby * aby
        if ab2 < 1e-6:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab2))
        sx, sy = ax + t * abx, ay + t * aby
        dist = math.hypot(px - sx, py - sy)
        if best is None or dist < best[0]:
            slat, slng = _ll(lat, lng, sx, sy)
            best = (dist, u, v, slat, slng)
    if best is None:
        raise RuntimeError("no road to snap to")
    return best


def attach_click(graph, lat: float, lng: float):
    dist, u, v, slat, slng = snap_to_road(graph, lat, lng)
    g = graph.copy()
    oid = "__click__"
    g.add_node(oid, x=slng, y=slat)
    nu, nv = g.nodes[u], g.nodes[v]

    def meters(a, b):
        return math.hypot(*_xy(a[0], a[1], b[0], b[1]))

    lu = max(0.5, meters((slat, slng), (float(nu["y"]), float(nu["x"]))))
    lv = max(0.5, meters((slat, slng), (float(nv["y"]), float(nv["x"]))))
    for node, length in ((u, lu), (v, lv)):
        g.add_edge(oid, node, length=length)
        g.add_edge(node, oid, length=length)
    log.info("snapped click to road in %.1fm (nodes %s–%s)", dist, u, v)
    return g, oid, slat, slng


def _graph_to_payload(graph: nx.MultiDiGraph) -> dict:
    nodes = {str(n): {"x": float(d["x"]), "y": float(d["y"])} for n, d in graph.nodes(data=True)}
    edges = []
    for u, v, data in graph.edges(data=True):
        edges.append({"u": str(u), "v": str(v), "length": float(data.get("length", 1.0))})
    return {"nodes": nodes, "edges": edges}


def _graph_from_payload(payload: dict) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for nid, d in payload["nodes"].items():
        graph.add_node(nid, x=float(d["x"]), y=float(d["y"]))
    for e in payload["edges"]:
        graph.add_edge(e["u"], e["v"], length=float(e["length"]))
    return graph


@lru_cache(maxsize=8)
def load_graph(lat: float, lng: float, dist_m: int = 1200):
    import json

    key_lat, key_lng = round(lat, 3), round(lng, 3)
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"carv2_{key_lat}_{key_lng}_{dist_m}.json"
    if path.exists():
        log.info("graph cache hit → %s", path.name)
        return _graph_from_payload(json.loads(path.read_text(encoding="utf-8"))), "osm"

    log.info("fetching OSM lat=%.4f lng=%.4f cover=%sm", lat, lng, dist_m)
    t0 = time.perf_counter()
    try:
        graph = fetch_osm_graph(lat, lng, dist_m)
        path.write_text(json.dumps(_graph_to_payload(graph)), encoding="utf-8")
        log.info("OSM Map API graph ready in %.1fs, cached %s", time.perf_counter() - t0, path.name)
        return graph, "osm"
    except Exception:
        log.exception("OSM Map API failed after %.1fs", time.perf_counter() - t0)
        return _grid_graph(lat, lng, dist_m), "grid"


def flood(lat: float, lng: float, minutes: float = 15.0, mode: str = "walk") -> dict:
    minutes = max(1.0, min(float(minutes), 25.0))
    mode = "bike" if mode == "bike" else "walk"
    speed = _speed(mode)
    max_m = speed * minutes * 60.0
    if mode == "bike":
        dist = int(min(2500, max(1400, max_m * 0.48)))
    else:
        dist = int(min(1100, max(500, max_m)))

    t_all = time.perf_counter()
    log.info("tide start %s %.4f,%.4f  %s min", mode, lat, lng, minutes)
    graph, source = load_graph(lat, lng, dist)
    routed, origin, slat, slng = attach_click(graph, lat, lng)
    meters = nx.single_source_dijkstra_path_length(routed, origin, cutoff=max_m, weight="length")
    log.info("dijkstra reachable=%s source=%s", len(meters), source)

    bands = (minutes / 3.0, 2 * minutes / 3.0, minutes)
    edges = []
    for u, v, data in routed.edges(data=True):
        if u == origin or v == origin:
            continue
        if u not in meters or v not in meters:
            continue
        t_sec = max(meters[u], meters[v]) / speed
        if t_sec > minutes * 60.0:
            continue
        nu, nv = routed.nodes[u], routed.nodes[v]
        coords = [[float(nu["x"]), float(nu["y"])], [float(nv["x"]), float(nv["y"])]]
        edges.append({"t": round(t_sec, 2), "coords": coords})

    edges.sort(key=lambda e: e["t"])
    elapsed = round(time.perf_counter() - t_all, 2)
    log.info("tide ready in %.1fs streets=%s source=%s", elapsed, len(edges), source)
    return {
        "origin": {"lat": slat, "lng": slng},
        "minutes": minutes,
        "mode": mode,
        "speed_mps": speed,
        "bands_s": [round(b * 60.0, 1) for b in bands],
        "edge_count": len(edges),
        "elapsed_s": elapsed,
        "source": source,
        "edges": edges,
    }
