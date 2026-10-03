"""
Talks to the OSM API, Overpass, and (optionally) osmcha to find
#ttmappyhour changesets in a given UTC time window, worldwide, and pull
down the data needed to run checks on them.
"""
import logging
import time
from datetime import datetime

import requests
import xml.etree.ElementTree as ET

import config
import geo_utils

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "osm-ttmappyhour-quality-check/1.0"}


class OverpassUnavailable(Exception):
    """
    Raised when Overpass context could not be fetched -- either every
    mirror failed every retry, or the circuit breaker is currently open.
    Distinct from "Overpass answered and there's genuinely nothing
    nearby" (which returns an empty result normally, not an exception),
    so callers can tell the difference between "no context exists" and
    "we couldn't find out" -- the latter needs to be retried later, the
    former is a final, correct answer.
    """
    pass


_OVERPASS_CIRCUIT_THRESHOLD = 1
_overpass_consecutive_failures = 0


def overpass_circuit_is_open():
    return _overpass_consecutive_failures >= _OVERPASS_CIRCUIT_THRESHOLD


def _get(url, params=None, headers=None, timeout=60, max_attempts=3):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, params=params, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_attempts:
                wait = 2 * attempt
                log.info("OSM API request failed (attempt %d/%d): %s -- retrying in %ds",
                         attempt, max_attempts, e, wait)
                time.sleep(wait)
    raise last_exc


def _parse_osm_dt(s):
    return datetime.strptime(s.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")


def fetch_changesets_in_window(start_dt, end_dt):
    if not config.HASHTAG or len(config.HASHTAG.strip()) < 3:
        raise ValueError(
            f"config.HASHTAG is empty or too short ({config.HASHTAG!r}) -- "
            f"refusing to scan, since this would match almost any changeset "
            f"on the planet instead of a specific campaign's."
        )

    found = {}
    unresolved = []
    cursor_end = end_dt
    hashtag_needle = f"#{config.HASHTAG}".lower()
    log.info("Matching changesets against hashtag needle: %r", hashtag_needle)

    while True:
        params = {
            "time": f"{start_dt.isoformat()}Z,{cursor_end.isoformat()}Z",
            "closed": "true",
        }
        try:
            resp = _get(f"{config.OSM_API_BASE}/changesets.json", params=params)
        except requests.RequestException as e:
            log.warning(
                "Persistent failure scanning changesets from %s to %s -- "
                "queuing this slice for later retry instead of blocking "
                "the whole window: %s",
                start_dt.isoformat(), cursor_end.isoformat(), e,
            )
            unresolved.append((start_dt.isoformat(), cursor_end.isoformat()))
            break

        data = resp.json().get("changesets", [])
        if not data:
            break

        for cs in data:
            tags = cs.get("tags", {})
            haystack = " ".join([tags.get("comment", ""), tags.get("hashtags", "")]).lower()
            idx = haystack.find(hashtag_needle)
            if idx == -1:
                continue
            end_idx = idx + len(hashtag_needle)
            boundary_ok = end_idx >= len(haystack) or not haystack[end_idx].isalnum()
            if boundary_ok:
                found[cs["id"]] = cs

        if len(data) < 100:
            break

        oldest_seen = min((c.get("closed_at") or c.get("created_at")) for c in data)
        new_cursor_end = _parse_osm_dt(oldest_seen)
        if new_cursor_end <= start_dt or new_cursor_end >= cursor_end:
            break
        cursor_end = new_cursor_end

    return list(found.values()), unresolved


def fetch_changeset_meta(changeset_id):
    try:
        resp = _get(f"{config.OSM_API_BASE}/changeset/{changeset_id}.json")
        return resp.json().get("changeset")
    except requests.RequestException as e:
        log.warning("Could not fetch metadata for pending changeset %s: %s", changeset_id, e)
        return None


def fetch_changeset_diff(changeset_id):
    resp = _get(f"{config.OSM_API_BASE}/changeset/{changeset_id}/download")
    root = ET.fromstring(resp.content)
    out = {"create": [], "modify": [], "delete": []}
    for action in root:
        if action.tag not in out:
            continue
        for el in action:
            if el.tag not in ("node", "way", "relation"):
                continue
            item = {"type": el.tag, "id": int(el.get("id"))}
            if el.tag == "node":
                lat_str, lon_str = el.get("lat"), el.get("lon")
                item["lat"] = float(lat_str) if lat_str is not None else None
                item["lon"] = float(lon_str) if lon_str is not None else None
            if el.tag == "way":
                item["nodes"] = [int(nd.get("ref")) for nd in el.findall("nd")]
            item["tags"] = {t.get("k"): t.get("v") for t in el.findall("tag")}
            out[action.tag].append(item)
    return out


def fetch_node_coords(node_ids, changeset_nodes):
    coords = {}
    missing = []
    for nid in node_ids:
        n = changeset_nodes.get(nid)
        if n is not None and n.get("lat") is not None and n.get("lon") is not None:
            coords[nid] = (n["lon"], n["lat"])
        else:
            missing.append(nid)

    for i in range(0, len(missing), 700):
        batch = missing[i:i + 700]
        try:
            resp = _get(f"{config.OSM_API_BASE}/nodes.json", params={"nodes": ",".join(map(str, batch))})
            for el in resp.json().get("elements", []):
                lon, lat = el.get("lon"), el.get("lat")
                if lon is not None and lat is not None:
                    coords[el["id"]] = (lon, lat)
        except requests.RequestException as e:
            log.warning("Could not resolve %d node coords: %s", len(batch), e)
    return coords


def fetch_overpass_context(min_lat, min_lon, max_lat, max_lon, exclude_way_ids, exclude_node_ids):
    global _overpass_consecutive_failures

    if _overpass_consecutive_failures >= _OVERPASS_CIRCUIT_THRESHOLD:
        log.info(
            "Overpass circuit breaker open (%d consecutive full failures this run) "
            "-- skipping context fetch instantly instead of retrying",
            _overpass_consecutive_failures,
        )
        raise OverpassUnavailable("circuit breaker open")

    buf_deg = config.OVERPASS_CONTEXT_BUFFER_M / 111000
    s, w, n, e = min_lat - buf_deg, min_lon - buf_deg, max_lat + buf_deg, max_lon + buf_deg
    query = f"""
    [out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];
    (
      way["building"]({s},{w},{n},{e});
      way["highway"]({s},{w},{n},{e});
    );
    out body;
    >;
    out skel qt;
    """

    data = None
    last_err = None
    for endpoint in config.OVERPASS_ENDPOINTS:
        for attempt in range(1, config.OVERPASS_RETRIES + 1):
            try:
                resp = requests.post(
                    endpoint, data={"data": query}, headers=HEADERS,
                    timeout=config.OVERPASS_HTTP_TIMEOUT_S,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                last_err = e
                log.info("Overpass %s attempt %d/%d failed: %s",
                         endpoint, attempt, config.OVERPASS_RETRIES, e)
        if data is not None:
            break

    if data is None:
        _overpass_consecutive_failures += 1
        log.warning(
            "All Overpass endpoints failed after retries (%d consecutive full "
            "failures this run): %s", _overpass_consecutive_failures, last_err,
        )
        raise OverpassUnavailable(str(last_err))

    _overpass_consecutive_failures = 0
    nodes, ways = {}, []
    for el in data.get("elements", []):
        if el["type"] == "node" and el["id"] not in exclude_node_ids:
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way" and el["id"] not in exclude_way_ids:
            ways.append({"id": el["id"], "nodes": el.get("nodes", []), "tags": el.get("tags", {})})
    return ways, nodes


def fetch_live_node(node_id):
    try:
        resp = _get(f"{config.OSM_API_BASE}/node/{node_id}.json")
        el = resp.json().get("elements", [{}])[0]
        if el.get("type") != "node" or el.get("visible") is False:
            return None
        lon, lat = el.get("lon"), el.get("lat")
        if lon is None or lat is None:
            return None
        return (lon, lat)
    except Exception as e:
        log.info("Live re-check of node %s failed (treated as unconfirmed): %s", node_id, e)
        return None


def fetch_ways_using_node(node_id):
    query = f"[out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];way(bn:{node_id});out ids;"
    try:
        resp = requests.post(
            config.OVERPASS_ENDPOINTS[0], data={"data": query}, headers=HEADERS,
            timeout=config.OVERPASS_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        return {el["id"] for el in data.get("elements", []) if el["type"] == "way"}
    except Exception as e:
        log.info("Could not verify node %s's real connections (treated as unconfirmed): %s", node_id, e)
        return None


def fetch_live_way_geometry(way_id):
    try:
        resp = _get(f"{config.OSM_API_BASE}/way/{way_id}/full.json")
        elements = resp.json().get("elements", [])
        way = next((e for e in elements if e["type"] == "way" and e["id"] == way_id), None)
        if way is None:
            return None
        node_coords = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
        return geo_utils.build_way_geometry(way.get("nodes", []), node_coords, way.get("tags"))
    except Exception as e:
        log.info("Live re-check of way %s failed (non-fatal, keeping original result): %s", way_id, e)
        return None


def fetch_osmcha_flags(changeset_id):
    if not config.OSMCHA_TOKEN:
        return {}
    try:
        resp = _get(
            f"{config.OSMCHA_API_BASE}/changesets/{changeset_id}/",
            headers={"Authorization": f"Token {config.OSMCHA_TOKEN}"},
        )
        return resp.json()
    except requests.RequestException as e:
        log.info("osmcha lookup failed for %s (non-fatal): %s", changeset_id, e)
        return {}
