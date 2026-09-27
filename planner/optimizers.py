"""Fuel purchase strategies.

All strategies solve the same problem: stations sit at mile markers x_0 <= x_1
<= ... <= x_{n-1} along a route of length D, each selling fuel at price p_i.
The vehicle burns 1/mpg gallons per mile, holds at most C gallons, and starts
with f0 gallons. Choose how many gallons b_i to buy at each station so the
vehicle reaches the destination, minimising sum(p_i * b_i).

Strategies:
  naive   drive until the tank can't reach the next station, then fill up there.
          A baseline for what an unplanned driver pays.
  greedy  the classic gas station algorithm: if a cheaper station is within one
          tank, buy just enough to reach it; otherwise fill up and move to the
          cheapest station in range. Optimal when fuel is divisible.
  lp      the same problem as a linear program solved with HiGHS. Exact, used
          to confirm the greedy answer.
  milp    adds a fixed cost per stop (binary y_i, b_i <= C*y_i) and minimises
          fuel cost + stop_penalty * stops. Optimal plans for pure fuel cost
          often make many small top-ups; this trades a little money for far
          fewer stops.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, linprog, milp

EPS = 1e-9


class InfeasibleRouteError(ValueError):
    """The route cannot be completed with the stations available."""


@dataclass(frozen=True)
class FuelProblem:
    positions: np.ndarray  # mile markers, sorted ascending
    prices: np.ndarray  # $/gallon
    total_miles: float
    tank_gallons: float
    mpg: float
    start_fuel_gallons: float
    stop_penalty: float = 0.0  # $ per stop, used by the milp strategy

    @property
    def range_miles(self) -> float:
        return self.tank_gallons * self.mpg

    @property
    def n(self) -> int:
        return len(self.positions)


@dataclass(frozen=True)
class FuelPlan:
    purchases: np.ndarray  # gallons bought at each station (0 when skipped)
    arrival_fuel: np.ndarray  # gallons in tank on arrival at each station

    def cost(self, problem: FuelProblem) -> float:
        return float(problem.prices @ self.purchases)

    @property
    def gallons(self) -> float:
        return float(self.purchases.sum())

    @property
    def stop_indices(self) -> list[int]:
        return [int(i) for i in np.flatnonzero(self.purchases > 1e-6)]


def check_feasible(problem: FuelProblem) -> None:
    """Raise InfeasibleRouteError if no purchase plan can complete the route."""
    start_reach = problem.start_fuel_gallons * problem.mpg
    if start_reach + EPS >= problem.total_miles:
        return
    if problem.n == 0:
        raise InfeasibleRouteError("No fuel stations were found along the route.")
    if problem.positions[0] > start_reach + EPS:
        raise InfeasibleRouteError(
            f"The first station is at mile {problem.positions[0]:.1f}, beyond the "
            f"{start_reach:.1f} miles the starting fuel covers. Increase start_fuel_gallons "
            "or max_detour_miles."
        )
    stops = np.append(problem.positions, problem.total_miles)
    gaps = np.diff(stops)
    worst = int(np.argmax(gaps))
    if gaps[worst] > problem.range_miles + EPS:
        raise InfeasibleRouteError(
            f"There is a {gaps[worst]:.1f} mile stretch without stations after mile "
            f"{stops[worst]:.1f}, longer than the {problem.range_miles:.0f} mile range. "
            "Try a larger max_detour_miles."
        )


def _arrival_fuel(problem: FuelProblem, purchases: np.ndarray) -> np.ndarray:
    bought_before = np.concatenate(([0.0], np.cumsum(purchases)[:-1]))
    return problem.start_fuel_gallons + bought_before - problem.positions / problem.mpg


def naive(problem: FuelProblem) -> FuelPlan:
    check_feasible(problem)
    mpg, cap = problem.mpg, problem.tank_gallons
    purchases = np.zeros(problem.n)
    fuel = problem.start_fuel_gallons
    prev = 0.0
    for i, x in enumerate(problem.positions):
        fuel -= (x - prev) / mpg
        prev = x
        if fuel * mpg + EPS >= problem.total_miles - x:
            break
        next_x = problem.positions[i + 1] if i + 1 < problem.n else problem.total_miles
        if fuel * mpg + EPS < next_x - x:
            to_finish = (problem.total_miles - x) / mpg
            buy = to_finish - fuel if to_finish <= cap else cap - fuel
            purchases[i] = buy
            fuel += buy
    return FuelPlan(purchases, _arrival_fuel(problem, purchases))


def greedy(problem: FuelProblem) -> FuelPlan:
    check_feasible(problem)
    x, p = problem.positions, problem.prices
    mpg, cap, rng, total = problem.mpg, problem.tank_gallons, problem.range_miles, problem.total_miles
    purchases = np.zeros(problem.n)
    if problem.start_fuel_gallons * mpg + EPS >= total:
        return FuelPlan(purchases, _arrival_fuel(problem, purchases))

    # Drive from the origin (where nothing can be bought) to the first station;
    # stopping at a station is free, so visiting it never hurts.
    cur = 0
    fuel = problem.start_fuel_gallons - x[0] / mpg
    while True:
        # Next station within one tank that is cheaper than here.
        cheaper = None
        j = cur + 1
        while j < problem.n and x[j] - x[cur] <= rng + EPS:
            if p[j] < p[cur]:
                cheaper = j
                break
            j += 1

        if cheaper is None and total - x[cur] <= rng + EPS:
            # Nothing cheaper before the destination: buy just enough to finish.
            purchases[cur] = max(0.0, (total - x[cur]) / mpg - fuel)
            break
        if cheaper is not None:
            need = (x[cheaper] - x[cur]) / mpg
            purchases[cur] = max(0.0, need - fuel)
            fuel += purchases[cur] - need
            cur = cheaper
            continue

        # Everything reachable is pricier: fill up here, go to the cheapest of them.
        in_range = np.arange(cur + 1, j)
        nxt = int(in_range[np.argmin(p[in_range])])
        purchases[cur] = cap - fuel
        fuel = cap - (x[nxt] - x[cur]) / mpg
        cur = nxt

    return FuelPlan(purchases, _arrival_fuel(problem, purchases))


def linear_program(problem: FuelProblem) -> FuelPlan:
    """min p.b  s.t.  arrival fuel >= 0, arrival + purchase <= C, reach destination.

    With S = cumulative purchases, arrival fuel at station i is
    f0 + S_{i-1} - x_i/mpg, which makes every constraint a row of a
    (strictly) lower-triangular matrix.
    """
    check_feasible(problem)
    n, mpg, cap, f0 = problem.n, problem.mpg, problem.tank_gallons, problem.start_fuel_gallons
    if f0 * mpg + EPS >= problem.total_miles:
        return FuelPlan(np.zeros(n), _arrival_fuel(problem, np.zeros(n)))

    a_ub, b_ub = _fuel_constraints(problem)
    result = linprog(problem.prices, A_ub=a_ub, b_ub=b_ub, bounds=(0, cap), method="highs")
    if not result.success:
        raise InfeasibleRouteError(f"Linear program failed: {result.message}")
    purchases = np.where(result.x > 1e-7, result.x, 0.0)
    return FuelPlan(purchases, _arrival_fuel(problem, purchases))


def mixed_integer_program(problem: FuelProblem, time_limit_seconds: float = 2.0) -> FuelPlan:
    """LP plus binary stop variables: min p.b + penalty * sum(y), b_i <= C * y_i."""
    check_feasible(problem)
    n, mpg, cap, f0 = problem.n, problem.mpg, problem.tank_gallons, problem.start_fuel_gallons
    if f0 * mpg + EPS >= problem.total_miles:
        return FuelPlan(np.zeros(n), _arrival_fuel(problem, np.zeros(n)))

    a_fuel, b_fuel = _fuel_constraints(problem)
    eye = sparse.identity(n, format="csr")
    a = sparse.vstack(
        [
            sparse.hstack([a_fuel, sparse.csr_matrix((a_fuel.shape[0], n))]),
            sparse.hstack([eye, -cap * eye]),  # b_i - C*y_i <= 0
        ],
        format="csr",
    )
    upper = np.concatenate([b_fuel, np.zeros(n)])
    result = milp(
        c=np.concatenate([problem.prices, np.full(n, problem.stop_penalty)]),
        constraints=LinearConstraint(a, -np.inf, upper),
        integrality=np.concatenate([np.zeros(n), np.ones(n)]),
        bounds=Bounds(np.zeros(2 * n), np.concatenate([np.full(n, cap), np.ones(n)])),
        options={"time_limit": time_limit_seconds, "mip_rel_gap": 1e-4},
    )
    if result.x is None:
        raise InfeasibleRouteError(f"Mixed integer program failed: {result.message}")
    purchases = np.where(result.x[:n] > 1e-7, result.x[:n], 0.0)
    return FuelPlan(purchases, _arrival_fuel(problem, purchases))


def _fuel_constraints(problem: FuelProblem) -> tuple[sparse.csr_matrix, np.ndarray]:
    """A_ub @ b <= b_ub rows: arrival fuel >= 0, arrival + purchase <= C, finish reached."""
    n, mpg, cap, f0 = problem.n, problem.mpg, problem.tank_gallons, problem.start_fuel_gallons
    fuel_used = problem.positions / mpg
    ones = sparse.csr_matrix(np.ones((n, n)))
    inclusive = sparse.tril(ones, format="csr")  # S_i
    strict = sparse.tril(ones, k=-1, format="csr")  # S_{i-1}
    a_ub = sparse.vstack([-strict, inclusive, -sparse.csr_matrix(np.ones((1, n)))], format="csr")
    b_ub = np.concatenate([f0 - fuel_used, cap - f0 + fuel_used, [f0 - problem.total_miles / mpg]])
    return a_ub, b_ub


STRATEGIES: dict[str, Callable[[FuelProblem], FuelPlan]] = {
    "greedy": greedy,
    "lp": linear_program,
    "milp": mixed_integer_program,
    "naive": naive,
}
