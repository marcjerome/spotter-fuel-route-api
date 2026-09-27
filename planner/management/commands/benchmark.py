"""Compare fuel strategies on real routes: cost, number of stops, and solve time.

Each route is fetched from OSRM once (then cached), so the benchmark measures
only the optimisers. Example:

    python manage.py benchmark --repeat 5 --csv benchmark.csv
"""

import csv
import statistics
import time
from pathlib import Path

from django.core.management.base import BaseCommand

from planner.locations import resolve_location
from planner.optimizers import STRATEGIES, InfeasibleRouteError
from planner.routing import get_route
from planner.services import build_problem, prune_dominated
from planner.station_index import get_station_index

ROUTES = [
    ("New York, NY", "Los Angeles, CA"),
    ("Seattle, WA", "Miami, FL"),
    ("Chicago, IL", "Houston, TX"),
    ("Dallas, TX", "Chicago, IL"),
    ("Denver, CO", "Atlanta, GA"),
    ("San Francisco, CA", "Salt Lake City, UT"),
    ("Boston, MA", "Washington, DC"),
    ("Phoenix, AZ", "Kansas City, MO"),
]

# (label, strategy, stop penalty)
VARIANTS = [
    ("naive", "naive", 0.0),
    ("greedy", "greedy", 0.0),
    ("lp", "lp", 0.0),
    ("milp $10/stop", "milp", 10.0),
    ("milp $50/stop", "milp", 50.0),
]


class Command(BaseCommand):
    help = "Benchmark fuel strategies (cost, stops, runtime) across a set of US routes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--repeat", type=int, default=3, choices=range(1, 101), metavar="N",
            help="Timed runs per variant, 1-100 (median reported).",
        )
        parser.add_argument(
            "--start-fuel", type=float, nargs="+", default=[0.0, 50.0], help="Start fuel levels (gallons)."
        )
        parser.add_argument("--max-detour", type=float, default=10.0)
        parser.add_argument("--csv", dest="csv_path", type=Path, help="Also write raw results to this CSV file.")

    def handle(self, *args, repeat, start_fuel, max_detour, csv_path, **options):
        index = get_station_index()
        rows = []
        for start_q, finish_q in ROUTES:
            start, finish = resolve_location(start_q), resolve_location(finish_q)
            route, _ = get_route(start.coords, finish.coords)
            candidates = prune_dominated(index.along_route(route.lat_lng, max_detour, route.distance_miles))
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"\n{start.label} -> {finish.label}: {route.distance_miles:.0f} mi, "
                    f"{len(candidates)} candidate stations"
                )
            )
            for fuel in start_fuel:
                try:
                    results = self._run_variants(candidates, route.distance_miles, fuel, max_detour, repeat)
                except InfeasibleRouteError as exc:
                    self.stdout.write(self.style.WARNING(f"  start fuel {fuel:g} gal: infeasible. {exc}"))
                    continue
                baseline = results["naive"]["cost"]
                optimum = results["lp"]["cost"]
                self.stdout.write(f"  start fuel {fuel:g} gal")
                self.stdout.write(
                    f"    {'variant':<15}{'cost':>10}{'vs naive':>10}{'vs opt':>9}{'stops':>7}{'time ms':>10}"
                )
                for label, r in results.items():
                    saving = (baseline - r["cost"]) / baseline * 100 if baseline else 0.0
                    gap = (r["cost"] - optimum) / optimum * 100 if optimum else 0.0
                    self.stdout.write(
                        f"    {label:<15}{r['cost']:>10.2f}{saving:>9.1f}%{gap:>8.2f}%{r['stops']:>7}"
                        f"{r['ms']:>10.2f}"
                    )
                    rows.append(
                        {
                            "route": f"{start.label} -> {finish.label}",
                            "distance_miles": round(route.distance_miles, 1),
                            "candidates": len(candidates),
                            "start_fuel_gallons": fuel,
                            "variant": label,
                            "cost": round(r["cost"], 2),
                            "saving_vs_naive_pct": round(saving, 2),
                            "gap_to_optimum_pct": round(gap, 3),
                            "stops": r["stops"],
                            "median_ms": round(r["ms"], 3),
                        }
                    )

        self._print_summary(rows)
        if csv_path:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.stdout.write(self.style.SUCCESS(f"\nWrote {len(rows)} rows to {csv_path}"))

    def _run_variants(self, candidates, total_miles, fuel, max_detour, repeat):
        results = {}
        for label, strategy, penalty in VARIANTS:
            problem = build_problem(candidates, total_miles, fuel, max_detour, penalty)
            timings = []
            plan = None
            for _ in range(repeat):
                t0 = time.perf_counter()
                plan = STRATEGIES[strategy](problem)
                timings.append((time.perf_counter() - t0) * 1000)
            assert plan is not None
            results[label] = {
                "cost": plan.cost(problem),
                "stops": len(plan.stop_indices),
                "ms": statistics.median(timings),
            }
        return results

    def _print_summary(self, rows):
        self.stdout.write(self.style.MIGRATE_HEADING("\nAverages across all routes and start fuel levels"))
        self.stdout.write(f"  {'variant':<15}{'vs naive':>10}{'vs opt':>9}{'stops':>7}{'time ms':>10}")
        for label, *_ in VARIANTS:
            subset = [r for r in rows if r["variant"] == label]
            self.stdout.write(
                f"  {label:<15}"
                f"{statistics.mean(r['saving_vs_naive_pct'] for r in subset):>9.1f}%"
                f"{statistics.mean(r['gap_to_optimum_pct'] for r in subset):>8.2f}%"
                f"{statistics.mean(r['stops'] for r in subset):>7.1f}"
                f"{statistics.mean(r['median_ms'] for r in subset):>10.2f}"
            )
