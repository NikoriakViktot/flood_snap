# Flood API — Specification (v1)

## 1. Purpose

The Flood API is the **client-facing HTTP interface** for the flood detection system.

It provides two functions:

1. **Scene discovery** — query Copernicus CDSE catalogue for Sentinel-1 imagery
2. **Job submission** — trigger flood detection pipeline via Control Plane contracts

> **This service does not process imagery.**
> **It does not manage state.**
> **It is a thin HTTP gateway.**

---

## 2. Current Endpoints

### 2.1 `POST /flood-event`

Query Copernicus CDSE catalogue for Sentinel-1 scenes over Bukovyna basin
within a date range, then download them via aria2.

**Parameters:** `start` (date string), `end` (date string)

**Flow:**
1. Authenticate with CDSE (OAuth2 password grant)
2. Query OData catalogue for SENTINEL-1 scenes intersecting bbox
3. Write download URLs to `/data/urls.txt`
4. Trigger aria2 download in `flood_downloader` container

**Returns:** `{"scenes": N}`

### 2.2 Planned Extensions

| Endpoint | Purpose |
|----------|---------|
| `POST /pipeline` | Submit `flood.pipeline.v1` contract to Control Plane |
| `GET /runs` | Proxy to Control Plane `/runs` |
| `GET /masks` | List available water mask outputs |
| `GET /masks/{scene}` | Download water mask GeoTIFF |

---

## 3. External Dependencies

| Service | URL | Auth |
|---------|-----|------|
| Copernicus CDSE Identity | `identity.dataspace.copernicus.eu` | OAuth2 password |
| Copernicus CDSE Catalogue | `catalogue.dataspace.copernicus.eu` | Bearer token |
| Copernicus CDSE Zipper | `zipper.dataspace.copernicus.eu` | Bearer token |

Credentials: `CDSE_USER` / `CDSE_PASS` environment variables.

---

## 4. Constraints

### 4.1 MUST

* Authenticate with CDSE before every catalogue query (tokens are short-lived)
* Write download URLs to shared `/data` volume
* Use aria2 in the downloader container for actual downloads (not in-process)

### 4.2 MUST NOT

* Process SAR imagery — that's the SAR Worker's job
* Manage pipeline state — that's the Control Plane's job
* Call workers directly — all orchestration goes through Control Plane

### 4.3 SHOULD

* Validate date range before querying CDSE
* Return scene IDs and download status
* Support configurable AOI (currently hardcoded bbox)

---

## 5. Docker

```dockerfile
# FastAPI app on port 8000
```

Environment variables:

| Var | Required | Description |
|-----|----------|-------------|
| `CDSE_USER` | Yes | Copernicus Data Space username |
| `CDSE_PASS` | Yes | Copernicus Data Space password |
| `REDIS_URL` | Yes | Redis connection for job submission |
| `DATA_DIR` | Yes | Shared volume root (`/data`) |

---

## 6. Integration with Control Plane

Future: the API will submit contracts to the Control Plane:

```python
# POST to Control Plane
requests.post("http://control_plane:8004/api/contracts", json={
    "type": "flood.pipeline.v1",
    "spec_version": "v1",
    "payload": {
        "scene": "S1B_IW_GRDH_....SAFE",
        "basin_geojson": "/work/basin_bc.geojson"
    }
})
```

This replaces direct `LPUSH flood_jobs` to Redis.
The Control Plane becomes the **sole entry point** for pipeline execution.
