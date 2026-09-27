# Loom demo script (5 minutes)

The numbers below come from a dry run of this exact flow against `docker compose`. Your routing
latency will vary: the free OSRM servers take 1–10 s for a *new* route.

## Before recording

1. `docker compose up -d --build`, then check that http://localhost:8000/api/docs/ loads.
2. `docker compose exec web python manage.py demo_reset`: clears cached routes, the stats
   counters and old jobs, so the first call is a real routing call.
3. Import `docs/postman_collection.json` into Postman. Check the collection variable `baseUrl` is
   `http://localhost:8000`, and open the **Postman Console** (bottom bar) so the summary lines show.
4. Open these tabs:
   - `README.md` (the architecture and scaling diagrams)
   - `docs/MATH.md` (section 4 trace, section 8 benchmark)
   - `planner/services.py`, `planner/optimizers.py` (`greedy`), `planner/routing.py` (`get_route`)
   - a terminal with `docker compose logs -f worker`
5. Don't send any route requests before recording. The first call has to be a cache miss.

---

## 0:00–0:40 · Architecture

**Show:** the README "How it works" diagram, then the project tree.

> "This is a Django 6.1 and DRF API. A request goes through five steps: resolve the city names
> offline using US Census data, one call to the free OSRM routing API, find stations near the
> route with an in-memory KD-tree, run the fuel optimizer, and return the plan with a map link.
>
> The code is split into `stations`, which holds the price sheet imported into PostgreSQL, and
> `planner`, which holds routing, the optimizers and the API. The views are thin: all the logic
> is in `services.py` and in pure functions in `optimizers.py`, which are unit-tested on their own.
>
> It runs in Docker as four services: the web app, a Celery worker, PostgreSQL, and Redis for the
> route cache and job queue."

## 0:40–1:30 · One API call

**Postman:** folder **1. One API call**.

1. **Routing stats (before):** everything is 0 and `cache_backend` is `RedisCache`.
2. **Plan NY → LA:** point to:
   - `summary.total_fuel_cost` **$698.59**, 17 stops,
   - `savings.vs_naive` **$67.95 saved**,
   - `meta.routing_api_calls: 1` and `routing_cache_hit: false`,
   - the response time (about 2.6 s, almost all of it waiting on OSRM, per `meta.timings_ms.routing`).
3. **Send the same request again:** **31 ms**, `routing_api_calls: 0`. It came from Redis.
4. Open `{{mapUrl}}` in the browser: the route, numbered stops and the savings card.

> "The brief asked for one routing call, ideally. A new trip makes exactly one OSRM request,
> and a repeated trip makes none. Everything after routing (station matching and optimization)
> takes about 10 to 20 milliseconds."

## 1:30–2:10 · Many synchronous calls

**Postman:** Collection Runner on folder **2. Many synchronous calls**, **5 iterations**
(35 requests over 7 routes). Then show the last request, **Routing stats (after the batch)**.

- Dry-run result: **38 route lookups → 7 OSRM calls**, **81.6% cache hit rate**.
- Iterations 2–5 return in milliseconds.

**Code:** `planner/routing.py`, the `get_route()` function: the cache lookup, the counters, and
failover to a second OSRM server.

> "Each unique trip calls OSRM once; after that it's served from Redis, which the web servers and
> workers share. If the main routing server fails, the client tries a second public server, and
> the stats endpoint counts every real HTTP call so this claim can be checked."

## 2:10–3:30 · The algorithm

**Postman:** folder **3. Algorithms**, the same trip with each strategy.

| Strategy | Cost | Stops | Optimizer time |
| --- | ---: | ---: | ---: |
| naive (baseline) | \$766.54 | 5 | 0.4 ms |
| **greedy (default)** | **\$698.59** | 17 | **0.3 ms** |
| lp (HiGHS) | \$698.59 | 17 | 47 ms |
| milp, \$10 per stop | \$705.62 | **6** | about 1 s |

**Show:** `docs/MATH.md` section 4 (the three-station trace), then `greedy()` in `optimizers.py`.

> "I modelled this as a linear program: choose gallons at each station to minimize cost, with fuel
> balance constraints. The tank can't go below zero or above 50 gallons.
>
> The default is the **greedy algorithm** for the fixed-route refuelling problem. If a cheaper
> station is within 500 miles, buy just enough to reach it. Otherwise fill up here. On this small
> example it buys 30, 50 and 10 gallons for \$289, and the LP solver gives exactly the same answer.
>
> On real routes, greedy matched the LP optimum every time and is about 100 times faster, so it's
> the default, and the LP checks it in the tests. Compared with a driver who just fills up when
> low, it saves 8.3% on average and up to 22%.
>
> The cheapest plan makes 17 stops, which isn't practical, so there's also a MILP that charges
> for each stop. At \$10 a stop it gets down to 6 stops for \$7 more. It takes about a second,
> which leads to the next part."

## 3:30–4:20 · Background jobs

**Postman:** folder **4. Background job**. Submit, then show the **202** and `status_url`, then
**Poll the job** until it says `succeeded`. Show the worker log line in the terminal.

**Show:** the README "Scaling" table.

> "Heavy plans shouldn't hold a web worker. With `mode: async`, the API validates the input and
> returns 202 right away, and a Celery worker solves it on a separate CPU queue. Identical requests
> share one job, and routing failures retry with backoff.
>
> I load-tested this. While 40 MILP plans run synchronously, p95 latency for normal requests goes
> to 1.3 seconds. As background jobs it stays at 240 milliseconds."

## 4:20–4:50 · Edge cases and tests

**Postman:** folder **5. Errors**. Send all three: unknown city (400), outside the USA (400),
LA on an empty tank (422).

> "The 422 is a real property of the data: the price sheet has only 8 California stations, all
> near the Mexican border. That's why the default is a full starting tank, and why the error
> explains itself. There are 164 tests, including one that checks greedy against the LP on 75
> random problems."

## 4:50–5:00 · Close

> "Swagger docs are at `/api/docs`, the math write-up is in `docs/MATH.md`, and the whole stack
> starts with `docker compose up`. Thanks for watching."

---

## If something goes wrong

- **The first route takes 5–10 s or returns a 502:** the public OSRM server is slow. Wait a few
  seconds and send again (a failure isn't cached), or say "that's the free external API, which is
  why everything after the first call is cached".
- **MILP returns 429:** you sent more than 10 synchronous MILP requests in a minute. Run
  `demo_reset` again, or use `mode: async`.
- **The stats aren't zero at the start:** run `demo_reset` again.
