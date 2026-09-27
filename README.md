# Fuel Route Planner API

A Django REST Framework API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON) plus a link to an interactive map,
- where to fuel up and how many gallons to buy at each stop, chosen for the lowest cost,
- the total fuel cost for a vehicle with a **500 mile range** at **10 MPG** (a 50 gallon tank).

Fuel prices come from the provided price sheet (`data/fuel-prices.csv`). Routing uses the free
[OSRM](https://project-osrm.org/) API, with **exactly one routing call per new route**. Repeat
requests are served from cache and make no external calls.

## Quick start

### Docker (PostgreSQL + Redis + Celery)

```bash
docker compose up --build
```

This starts four services: `web` (gunicorn), `worker` (Celery), `db` (PostgreSQL) and `redis`
(the job queue and shared route cache). On first start `web` runs migrations and imports the
stations. Then open:

| URL | What |
| --- | --- |
| http://localhost:8000/api/docs/ | Swagger UI (try the API here) |
| http://localhost:8000/api/redoc/ | ReDoc |
| http://localhost:8000/api/schema/ | OpenAPI 3 schema |

### Local (SQLite)

Requires Python 3.12+ (Django 6.1).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # or: uv sync
python manage.py migrate
python manage.py createcachetable
python manage.py load_stations
python manage.py runserver
pytest
```

Without `REDIS_URL`, background jobs run inline in the web process, so `mode=async` works
locally with no broker. To run a real worker locally, set `REDIS_URL=redis://localhost:6379/0`
and start `celery -A config worker -Q io,cpu`.

## API

### `POST /api/v1/route-plan/`

```json
{
  "start": "New York, NY",
  "finish": "Los Angeles, CA",
  "strategy": "greedy",
  "start_fuel_gallons": 50,
  "max_detour_miles": 10,
  "stop_penalty": 25,
  "mode": "sync"
}
```

| Field | Default | Notes |
| --- | --- | --- |
| `start`, `finish` | required | `"City, ST"`, `"City, State"` or `"lat,lng"`. Must be in the USA. |
| `strategy` | `greedy` | `greedy`, `lp`, `milp` or `naive` (see [Strategies](#strategies)). |
| `start_fuel_gallons` | `50` (full) | 0–50. With `0` the vehicle fuels up at a station near the start before leaving. |
| `max_detour_miles` | `10` | How far off the route a station may be. |
| `stop_penalty` | `25` | `$` per stop, used only by `milp`. |
| `mode` | `sync` | `async` queues the plan for a background worker (see [Background jobs](#background-jobs)). |

`GET /api/v1/route-plan/?start=Dallas,%20TX&finish=Chicago,%20IL` accepts the same fields as
query parameters.

Response (trimmed):

```json
{
  "start":  {"query": "Dallas, TX", "label": "Dallas, TX", "latitude": 32.79, "longitude": -96.77},
  "finish": {"query": "Chicago, IL", "label": "Chicago, IL", "latitude": 41.84, "longitude": -87.68},
  "strategy": "greedy",
  "assumptions": {"mpg": 10, "vehicle_range_miles": 500, "tank_capacity_gallons": 50,
                  "start_fuel_gallons": 50, "max_detour_miles": 10, "stop_penalty": null},
  "route": {"distance_miles": 961.0, "duration_hours": 17.04,
            "geometry": {"type": "LineString", "coordinates": [[-96.77, 32.79], "..."]}},
  "fuel_stops": [
    {"sequence": 1,
     "station": {"id": 3539, "opis_id": 68213, "name": "CADOO MILLS",
                 "address": "I-30/US-67, EXIT 87 & FM-1903", "city": "Caddo Mills", "state": "TX",
                 "latitude": 33.07, "longitude": -96.23},
     "mile_marker": 40.0, "detour_miles": 3.5, "price_per_gallon": 2.801,
     "fuel_on_arrival_gallons": 46.0, "gallons_purchased": 4.0, "cost": 11.21}
  ],
  "summary": {"total_fuel_cost": 133.09, "total_gallons_purchased": 46.1,
              "average_price_per_gallon": 2.887, "number_of_stops": 4,
              "stations_near_route": 189, "stations_considered": 84,
              "savings": {
                "vs_naive":         {"baseline_cost": 138.26, "amount": 5.18,  "percent": 3.74},
                "vs_average_price": {"baseline_cost": 151.6,  "amount": 18.51, "percent": 12.21},
                "average_price_near_route": 3.288, "per_gallon_vs_average": 0.402}},
  "map_url": "http://localhost:8000/api/v1/route-plan/map/?start=Dallas%2C+TX&...",
  "meta": {"routing_api_calls": 1, "routing_cache_hit": false,
           "timings_ms": {"geocoding": 0.04, "routing": 2013.3, "station_matching": 6.3, "optimization": 0.3}}
}
```

`summary.savings` reports what the plan saves, like fuel apps do. It compares against two
baselines: `vs_naive` (a driver who fills up only when the tank can't reach the next station)
and `vs_average_price` (the same gallons bought at the average price of stations near the
route).

Errors: `400` for invalid input or an unknown or non-US location, `422` when no plan is possible
(for example a stretch longer than 500 miles without stations), `429` when a client sends more
than 10 synchronous `milp` requests a minute (use `mode=async` instead), and `502` when the
routing service fails.

### Background jobs

With `"mode": "async"` the API validates the input (including the locations) and returns
`202 Accepted` immediately, with a `Location` header pointing at the job:

```json
{"id": "e008e153-…", "status": "pending", "params": {"start": "Seattle, WA", "...": "..."},
 "status_url": "http://localhost:8000/api/v1/route-plan/jobs/e008e153-…/",
 "map_url": null, "created_at": "…", "started_at": null, "finished_at": null,
 "result": null, "error": null}
```

Poll `GET /api/v1/route-plan/jobs/{id}/` until `status` is `succeeded` (then `result` holds the
same body as a synchronous plan and `map_url` links to its map) or `failed` (then
`error` is `{"status_code": 502, "detail": "..."}`). If an identical request is already queued,
running, or finished within the last hour, you get the same job back, so many clients asking for
the same trip share one computation.

### `GET /api/v1/route-plan/map/?…`

The same inputs, rendered as a Leaflet map with the route, numbered fuel stops and a summary
sidebar. The `map_url` field in every response links here. The map reuses the cached route, so
opening it makes no extra routing call.

### `GET /api/v1/stats/`

Running totals of how often the free routing API is really called: route lookups, OSRM HTTP
calls (including any failover), cache hits and misses, the hit rate, and the active cache
backend. `python manage.py demo_reset` sets them back to zero and clears the route cache.

### `GET /api/v1/stations/`

A paginated list of the imported stations. Filters: `state`, `city`, `max_price`,
`ordering=price|-price`.

### Demo

[`docs/postman_collection.json`](docs/postman_collection.json) has every call above in demo
order, and [`docs/DEMO.md`](docs/DEMO.md) is the 5-minute walkthrough script.

## How it works

```
"City, ST" ──► offline geocoder ──► OSRM (1 call, cached) ──► stations within N miles ──► optimiser ──► response
               (Census Gazetteer)                              (in-memory KD-tree)
```

1. **Geocoding stations (once, offline).** The price sheet has no coordinates, and its addresses
   are highway exits such as `I-44, EXIT 283 & US-69`, so they geocode poorly. `load_stations`
   places each station at the centroid of its city, using the US Census Gazetteer (places plus
   county subdivisions, public domain, `data/us_places.csv`). This matches **97%** of US rows.
   The 620 Canadian rows are skipped, duplicate OPIS ids keep their cheapest price, and 175
   stations in unmatched hamlets are stored without coordinates.
2. **Geocoding the request.** `"City, ST"` inputs use the same offline index, so resolving a
   location takes microseconds and makes no API call.
3. **Routing.** A single OSRM request returns the full route geometry and distance. Results are
   cached in the database cache for a week, which is shared by all gunicorn workers and survives
   restarts. If the primary server fails, a second public OSRM server is tried.
4. **Stations on the route.** The route is densified to 1 mile steps and put in a KD-tree on the
   unit sphere, then every station is queried against it at once (vectorised with numpy and
   scipy). This gives each station a mile marker and its distance from the route in a few
   milliseconds, with no spatial database needed. When several stations share a town, only the
   cheapest is kept, since a pricier station at the same spot can never be the better choice.
5. **Optimisation.** The selected strategy decides how much fuel to buy at each station.

## Strategies

The full derivation, from a two-variable LP solved by hand, through a three-station worked
example and its dual, to shadow prices on a real route, is in **[docs/MATH.md](docs/MATH.md)**.

The problem: stations at mile markers `x_i` sell fuel at price `p_i`. Choose the gallons `b_i`
to buy at each so the tank never runs dry and never exceeds 50 gallons, minimising `Σ p_i·b_i`.

| Strategy | Idea | Guarantee |
| --- | --- | --- |
| `naive` | Drive until the tank can't reach the next station, then fill up. What an unplanned driver does. | Baseline |
| `greedy` (default) | If a cheaper station is within one tank, buy just enough to reach it. Otherwise fill up and go to the cheapest station in range. | Optimal when fuel is divisible (Lin, Gertsch & Russell 2007); O(n·k) |
| `lp` | The same problem as a linear program (lower-triangular constraints on cumulative purchases), solved with HiGHS. | Exact optimum; checks `greedy` |
| `milp` | Adds binary stop variables `y_i` with `b_i ≤ 50·y_i` and minimises `Σ p_i·b_i + stop_penalty·Σ y_i`. | Optimal cost/stops trade-off (2 s time limit) |

The cheapest plans often make many small top-ups, because they grab every slightly cheaper
station. `milp` puts a price on each stop, which usually gives a plan with a third of the stops
for about 1% more money.

### Benchmark

`python manage.py benchmark --repeat 5 --csv docs/benchmark.csv` runs every strategy on 8 US
routes, with a full and an empty starting tank.

Averages across all 15 feasible route and start-fuel runs (full output in `docs/benchmark.csv`):

| Variant | Saving vs naive | Gap to optimum | Avg stops | Median solve time |
| --- | --- | --- | --- | --- |
| naive | 0.0% | 9.56% | 2.9 | 0.09 ms |
| **greedy** | **8.3%** | **0.00%** | 9.3 | **0.11 ms** |
| lp | 8.3% | 0.00% | 9.6 | 12.4 ms |
| milp $10/stop | 6.7% | 1.76% | **3.5** | 479 ms |
| milp $50/stop | 4.4% | 4.56% | 3.1 | 652 ms |

Selected routes (start with an empty tank):

| Route | Miles | naive | greedy = lp | milp $10/stop |
| --- | --- | --- | --- | --- |
| New York → Los Angeles | 2,810 | $929.49 (6 stops) | $852.61 (20) | $865.70 (7) |
| Seattle → Miami | 3,303 | $1,101.41 (7) | $1,026.91 (21) | $1,042.88 (8) |
| Chicago → Houston | 1,083 | $368.06 (3) | $318.14 (10) | $329.84 (3) |
| Boston → Washington | 439 | $188.83 (1) | $146.98 (8) | $149.74 (3) |
| Phoenix → Kansas City | 1,248 | $448.05 (3) | $363.19 (5) | $371.40 (3) |

Takeaways:

- **Greedy matched the LP optimum on every route** (also checked on 75 random problems in the
  tests) and is about 100× faster, so it is the default.
- **Planning saves up to 22%** over the naive driver (8.3% on average). The gain is largest on routes with big price spreads and near zero on short trips a full tank already covers.
- **The practical sweet spot is `milp` at about $10/stop**: roughly a third of the stops for
  under 2% extra cost. It is slower, and on coast-to-coast routes it can hit its 2 s time limit
  and return the best plan found so far instead of a proven optimum. An exact dynamic program
  for a fixed number of stops (Khuller, Malekian & Mestre, *To Fill or not to Fill*) would be
  the next step.
- The `lp` and `greedy` stop counts can differ when several plans tie on cost (degenerate LP
  solutions).

## Scaling: keeping heavy plans off the web server

Most requests are cheap: with a cached route, a greedy plan takes about 8 ms of server time, and
3 gunicorn workers handle about 120 requests/s on a laptop. Two things are expensive: `milp`
solves (up to about 2 s of CPU each) and uncached routes (1–3 s waiting on OSRM). Run inside the
web server, a burst of either ties up the workers and slows everyone else.

The design keeps fast requests synchronous and moves heavy ones to Celery workers:

```
                 ┌──────── fast path: greedy / lp (about 20 ms) ─────────► 200 OK + plan
client ──► web ──┤
 (gunicorn,      └──────── mode=async ──► 202 + job ──► Redis ──► Celery workers
  stateless)                                (job id = hash     ├─ queue "cpu": milp solves
     ▲                                       of the inputs)    └─ queue "io":  routing
     └──── GET /route-plan/jobs/{id}/  ◄──── result stored in PostgreSQL ◄────┘
```

- **Separate queues.** CPU-bound `milp` jobs go to the `cpu` queue and everything else to `io`,
  so each can get its own workers and concurrency. Workers take one job at a time
  (`prefetch_multiplier=1`) and have a hard 45 s time limit.
- **Durable jobs.** Job state lives in the `RoutePlanJob` table, not in Celery, so it survives
  restarts and can be inspected in the admin. `acks_late` redelivers a job whose worker died, and
  finished jobs are never recomputed.
- **Retries.** When OSRM fails, a job retries twice with backoff (5 s, then 10 s). A synchronous
  request can only fail.
- **Deduplication.** Identical requests share one job.
- **Throttling.** DRF limits synchronous `milp` to 10/min per client, which pushes heavy callers
  to `mode=async`. Job submissions are limited to 60/min.
- **Shared cache.** Routes are cached in Redis and shared by web and workers.
- **Horizontal scaling.** Web and workers hold no state (station data is read-only and loaded per
  process), so both scale by adding containers: `docker compose up --scale worker=3`.

Measured locally (8 cores, 3 gunicorn workers with 4 threads each, 4 Celery processes, cached
routes), greedy latency while 40 `milp` plans run at the same time:

| How `milp` runs | greedy p50 | greedy p95 | `milp` requests |
| --- | --- | --- | --- |
| Synchronously in the web server | 84 ms | **1,294 ms** | held web threads for 2.1 s at p50 (4.0 s at p95) |
| As background jobs (`mode=async`) | 100 ms | **240 ms** | 17 ms to submit; all 40 done in 10.8 s |

The remaining slowdown comes from sharing one machine's CPUs. In production the workers run on
separate hosts.

**Next steps for production:** self-host OSRM (the `osrm-backend` image with a US map extract)
to cut routing from seconds to milliseconds and remove the external dependency, add API keys so
throttles apply per customer, send webhooks as an alternative to polling, and export queue depth
and job duration metrics.

## Assumptions and limitations

- **Station locations are approximate** (city centroids, within a few miles), so `detour_miles`
  is a straight-line estimate and the fuel for detours is not counted.
- **Coverage follows the price sheet.** California has only 8 stations, all in the Imperial
  Valley, so trips that start in California and set `start_fuel_gallons=0` are infeasible
  (`422`). That is why the default start is a full tank.
- Stations within `max_detour_miles` of the start count as "fuel up before leaving town".
- The vehicle arrives at the destination with an empty tank, and every station is assumed to be
  open and to allow partial fills.
- The public OSRM servers are best effort and typically take 1–3 s. For production, set
  `OSRM_BASE_URLS` to a self-hosted OSRM, which also removes the only external dependency.

## Project layout

```
config/            settings (env driven), urls, celery.py (Celery app), settings_test.py
stations/          FuelStation model, offline geocoder, load_stations command, list endpoint
planner/
  locations.py     "City, ST" / "lat,lng" parsing
  routing.py       OSRM client with cache and failover
  station_index.py in-memory KD-tree of stations
  optimizers.py    naive / greedy / lp / milp
  services.py      pipeline + response shaping
  models.py        RoutePlanJob (background job state and result)
  jobs.py          job submission, deduplication, queue routing
  tasks.py         Celery task (with routing retries)
  throttles.py     rate limits for sync milp and job submissions
  views.py         API, job, map and stats views (drf-spectacular docs)
  management/commands/benchmark.py, demo_reset.py
data/              fuel-prices.csv, us_places.csv (Census Gazetteer 2025)
```
