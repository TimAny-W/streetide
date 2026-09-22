"""Pull a small bbox from the main OSM Map API (not Overpass) and build a walk graph."""
from __future__ import annotations

import logging
import math
import xml.etree.ElementTree as ET
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import networkx as nx

log = logging.getLogger("streetide")

OSM_MAP = "https://api.openstreetmap.org/api/0.6/map"
USER_AGENT = "Streetide/0.1 (educational project)"

CAR_HIGHWAYS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "road",
}


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bbox(lat: float, lng: float, dist_m: float) -> tuple[float, float, float, float]:
    dlat = dist_m / 111_320.0
    dlng = dist_m / (111_320.0 * max(0.25, math.cos(math.radians(lat))))
    west, south = lng - dlng, lat - dlat
    east, north = lng + dlng, lat + dlat
    return west, south, east, north


def fetch_osm_xml(lat: float, lng: float, dist_m: int, timeout_s: int = 30) -> bytes:
    """Dense cities overflow the 50k-node cap; shrink the box until OSM accepts it."""
    dist = min(int(dist_m), 450)
    last_error: Exception | None = None
    while dist >= 220:
        west, south, east, north = _bbox(lat, lng, dist)
        url = f"{OSM_MAP}?bbox={west:.6f},{south:.6f},{east:.6f},{north:.6f}"
        log.info("OSM Map API GET radius=%sm %s", dist, url)
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                data = resp.read()
            log.info("OSM Map API returned %s KB (radius %sm)", round(len(data) / 1024), dist)
            return data
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            log.warning("OSM HTTP %s at %sm: %s", exc.code, dist, body.replace("\n", " "))
            last_error = exc
            if exc.code != 400:
                raise
            dist = int(dist * 0.7)
    raise RuntimeError(f"OSM Map API rejected bbox even at ~200m: {last_error}") from last_error


def _offset_point(lat: float, lng: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / 111_320.0
    dlng = east_m / (111_320.0 * max(0.25, math.cos(math.radians(lat))))
    return lat + dlat, lng + dlng


def fetch_osm_graph(lat: float, lng: float, dist_m: int) -> nx.MultiDiGraph:
    """One bbox if small; a 3×3 mosaic when bike/long windows need more streets."""
    tile_r = 420
    if dist_m <= 700:
        return graph_from_osm_xml(fetch_osm_xml(lat, lng, dist_m))

    ring = 2 if dist_m >= 1800 else 1
    step_m = min(780, max(520, dist_m / (ring + 0.35)))
    graphs = []
    coords = [
        _offset_point(lat, lng, i * step_m, j * step_m)
        for i in range(-ring, ring + 1)
        for j in range(-ring, ring + 1)
    ]
    log.info("OSM mosaic %sx%s tiles, step=%sm, cover≈%sm", 2 * ring + 1, 2 * ring + 1, int(step_m), dist_m)
    for idx, (tlat, tlng) in enumerate(coords, start=1):
        try:
            g = graph_from_osm_xml(fetch_osm_xml(tlat, tlng, tile_r))
            graphs.append(g)
            log.info("tile %s/%s ok (%s edges)", idx, len(coords), g.number_of_edges())
        except Exception as exc:
            log.warning("tile %s/%s failed: %s", idx, len(coords), exc)
        time.sleep(0.12)

    if not graphs:
        raise RuntimeError("all OSM mosaic tiles failed")
    merged = nx.compose_all(graphs)
    log.info("mosaic merged %s nodes %s edges", merged.number_of_nodes(), merged.number_of_edges())
    return merged


def graph_from_osm_xml(xml_bytes: bytes) -> nx.MultiDiGraph:
    root = ET.fromstring(xml_bytes)
    nodes: dict[str, tuple[float, float]] = {}
    for el in root.findall("node"):
        nid = el.get("id")
        if nid is None:
            continue
        nodes[nid] = (float(el.get("lat")), float(el.get("lon")))

    graph = nx.MultiDiGraph()
    added = 0
    for way in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        hw = tags.get("highway")
        if hw not in CAR_HIGHWAYS:
            continue
        if tags.get("area") == "yes":
            continue
        refs = [nd.get("ref") for nd in way.findall("nd")]
        oneway = tags.get("oneway") in {"yes", "true", "1"}
        for a, b in zip(refs, refs[1:]):
            if a not in nodes or b not in nodes:
                continue
            lat1, lng1 = nodes[a]
            lat2, lng2 = nodes[b]
            length = _haversine_m(lat1, lng1, lat2, lng2)
            if length < 0.5:
                continue
            graph.add_node(a, y=lat1, x=lng1)
            graph.add_node(b, y=lat2, x=lng2)
            graph.add_edge(a, b, length=length)
            if not oneway:
                graph.add_edge(b, a, length=length)
            added += 1

    isolates = list(nx.isolates(graph))
    graph.remove_nodes_from(isolates)
    if added < 8:
        raise RuntimeError(f"OSM bbox had too few walkable ways ({added})")
    log.info("built walk graph: %s nodes, %s edges", graph.number_of_nodes(), graph.number_of_edges())
    return graph
