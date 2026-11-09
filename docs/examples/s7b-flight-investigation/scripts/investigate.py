def run(inputs):
    found = call("mcp__scat__search_scat_flights", {"batch": inputs["batch"], "callsign": inputs["callsign"], "limit": inputs["search_limit"]})
    if get(found, "status") != "ok":
        return {"status": "error", "flights": 0, "points": 0, "altitude_m": 0, "missing_points": 0, "external_ids": [], "synthetic": get(found, "synthetic", False)}
    items = found["data"]["items"]
    ids = []
    for item in items:
        ids = append(ids, item["source"]["external_id"])
    if len(ids) == 0:
        return {"status": "empty", "flights": 0, "points": 0, "altitude_m": 0, "missing_points": 0, "external_ids": [], "synthetic": get(found, "synthetic", False)}
    quality = call("mcp__scat__inspect_scat_collection_data_quality", {"batch": inputs["batch"]})
    track = call("mcp__scat__get_scat_track_points", {"batch": inputs["batch"], "external_id": ids[0], "limit": inputs["track_limit"]})
    if get(quality, "status") != "ok" or get(track, "status") != "ok":
        return {"status": "error", "flights": len(ids), "points": 0, "altitude_m": 0, "missing_points": 0, "external_ids": ids, "synthetic": get(found, "synthetic", False)}
    points = track["data"]["points"]
    altitude = points[0]["position"]["altitude_m"] if len(points) > 0 else 0
    return {"status": "ok", "flights": len(ids), "points": len(points), "altitude_m": altitude, "missing_points": quality["data"]["missing_point_count"], "external_ids": ids, "synthetic": get(found, "synthetic", False)}
