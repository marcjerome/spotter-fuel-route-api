import numpy as np
import pytest

from planner.optimizers import (
    STRATEGIES,
    FuelPlan,
    FuelProblem,
    InfeasibleRouteError,
    greedy,
    linear_program,
    mixed_integer_program,
    naive,
)


def make_problem(positions, prices, total, start_fuel=0.0, stop_penalty=0.0):
    return FuelProblem(
        positions=np.array(positions, dtype=float),
        prices=np.array(prices, dtype=float),
        total_miles=float(total),
        tank_gallons=50.0,
        mpg=10.0,
        start_fuel_gallons=start_fuel,
        stop_penalty=stop_penalty,
    )


def assert_valid(problem: FuelProblem, plan: FuelPlan):
    """Tank never runs dry, never overflows, and the destination is reached."""
    assert (plan.purchases >= -1e-9).all()
    assert (plan.arrival_fuel >= -1e-6).all()
    assert (plan.arrival_fuel + plan.purchases <= problem.tank_gallons + 1e-6).all()
    final = problem.start_fuel_gallons + plan.gallons - problem.total_miles / problem.mpg
    assert final >= -1e-6


def random_problem(rng, n=60, total=2500.0, start_fuel=0.0):
    # Stations every <= 300 miles guarantees feasibility with a 500 mile range.
    positions = np.sort(np.concatenate(([0.0], rng.uniform(0, total, n - 1))))
    for i in range(1, len(positions)):
        positions[i] = min(positions[i], positions[i - 1] + 300)
    prices = rng.uniform(2.8, 4.5, n).round(3)
    return make_problem(positions, prices, total, start_fuel)


class TestGreedy:
    def test_buys_just_enough_to_reach_cheaper_station(self):
        problem = make_problem([0, 200], [4.0, 3.0], 600)
        plan = greedy(problem)
        # 20 gal at $4 to reach the cheap station, then 40 gal at $3 to finish.
        np.testing.assert_allclose(plan.purchases, [20, 40])
        assert plan.cost(problem) == pytest.approx(200.0)

    def test_fills_up_when_cheapest_station_is_here(self):
        problem = make_problem([0, 400], [3.0, 4.0], 800)
        plan = greedy(problem)
        # Fill the tank at $3 (reaches mile 500), buy only the remaining 30 gal at $4.
        np.testing.assert_allclose(plan.purchases, [50, 30])
        assert_valid(problem, plan)

    def test_start_fuel_covers_whole_trip(self):
        problem = make_problem([100], [3.0], 300, start_fuel=40)
        plan = greedy(problem)
        assert plan.gallons == 0
        assert plan.stop_indices == []

    def test_uses_start_fuel_before_buying(self):
        problem = make_problem([0, 250], [4.0, 3.0], 600, start_fuel=30)
        plan = greedy(problem)
        # 30 gal reaches the cheap station at mile 250 without buying at $4; 5 gal remain.
        np.testing.assert_allclose(plan.purchases, [0, 30])


class TestInfeasible:
    @pytest.mark.parametrize("strategy", sorted(STRATEGIES))
    def test_gap_longer_than_range(self, strategy):
        problem = make_problem([0, 100, 700], [3, 3, 3], 900)
        with pytest.raises(InfeasibleRouteError, match="stretch"):
            STRATEGIES[strategy](problem)

    @pytest.mark.parametrize("strategy", sorted(STRATEGIES))
    def test_first_station_out_of_reach(self, strategy):
        problem = make_problem([50], [3], 400, start_fuel=2)
        with pytest.raises(InfeasibleRouteError, match="first station"):
            STRATEGIES[strategy](problem)

    def test_no_stations(self):
        with pytest.raises(InfeasibleRouteError, match="No fuel stations"):
            greedy(make_problem([], [], 800))


class TestStrategiesAgree:
    @pytest.mark.parametrize("seed", range(25))
    @pytest.mark.parametrize("start_fuel", [0.0, 20.0, 50.0])
    def test_greedy_matches_lp_optimum(self, seed, start_fuel):
        problem = random_problem(np.random.default_rng(seed), start_fuel=start_fuel)
        g, lp = greedy(problem), linear_program(problem)
        assert_valid(problem, g)
        assert_valid(problem, lp)
        assert g.cost(problem) == pytest.approx(lp.cost(problem), rel=1e-7)

    @pytest.mark.parametrize("seed", range(10))
    def test_naive_never_beats_optimum(self, seed):
        problem = random_problem(np.random.default_rng(seed))
        plan = naive(problem)
        assert_valid(problem, plan)
        assert plan.cost(problem) >= linear_program(problem).cost(problem) - 1e-6

    @pytest.mark.parametrize("seed", range(5))
    def test_milp_without_penalty_matches_lp(self, seed):
        problem = random_problem(np.random.default_rng(seed), n=40)
        assert mixed_integer_program(problem).cost(problem) == pytest.approx(
            linear_program(problem).cost(problem), rel=1e-4
        )

    @pytest.mark.parametrize("seed", range(5))
    def test_stop_penalty_reduces_stops(self, seed):
        rng = np.random.default_rng(seed)
        base = random_problem(rng, n=40)
        penalised = make_problem(base.positions, base.prices, base.total_miles, stop_penalty=50)
        plan = mixed_integer_program(penalised)
        assert_valid(penalised, plan)
        assert len(plan.stop_indices) <= len(greedy(base).stop_indices)
        # Fewer stops can never be cheaper in pure fuel terms than the optimum.
        assert plan.cost(base) >= linear_program(base).cost(base) - 1e-6


class TestWorkedExamples:
    """The hand-solved examples in docs/MATH.md, sections 1, 4 and 7."""

    def test_warm_up_lp_and_its_dual(self):
        from scipy.optimize import linprog

        a, b, c = np.array([[2, 1], [1, 1]]), np.array([6, 4]), np.array([3, 2])
        primal = linprog(c, A_ub=-a, b_ub=-b, method="highs")
        dual = linprog(-b, A_ub=a.T, b_ub=c, method="highs")
        np.testing.assert_allclose(primal.x, [2, 2])
        np.testing.assert_allclose(dual.x, [1, 1])
        assert primal.fun == pytest.approx(-dual.fun) == pytest.approx(10)  # strong duality

    three_stations = staticmethod(lambda **kw: make_problem([0, 300, 450], [3.5, 3.0, 3.4], 900, **kw))

    @pytest.mark.parametrize("strategy", [greedy, linear_program])
    def test_three_station_optimum(self, strategy):
        problem = self.three_stations()
        plan = strategy(problem)
        np.testing.assert_allclose(plan.purchases, [30, 50, 10], atol=1e-6)
        np.testing.assert_allclose(plan.arrival_fuel, [0, 0, 35], atol=1e-6)
        assert plan.cost(problem) == pytest.approx(289.0)

    def test_three_station_dual(self):
        from scipy.optimize import linprog

        a = np.array([[1, 0, 0], [1, 1, 0], [1, 1, 1], [-1, 0, 0], [-1, -1, 0], [-1, -1, -1]])
        b = np.array([30, 45, 90, -50, -80, -95])
        dual = linprog(-b, A_ub=a.T, b_ub=[3.5, 3.0, 3.4], method="highs")
        np.testing.assert_allclose(dual.x, [0.5, 0, 3.4, 0, 0.4, 0], atol=1e-9)
        assert -dual.fun == pytest.approx(289.0)

    def test_three_station_naive(self):
        problem = self.three_stations()
        plan = naive(problem)
        np.testing.assert_allclose(plan.purchases, [50, 0, 40])
        assert plan.cost(problem) == pytest.approx(311.0)

    @pytest.mark.parametrize("penalty,stops,cost", [(20, 3, 289.0), (22, 2, 310.5)])
    def test_three_station_stop_penalty_crossover(self, penalty, stops, cost):
        problem = self.three_stations(stop_penalty=penalty)
        plan = mixed_integer_program(problem)
        assert len(plan.stop_indices) == stops
        assert plan.cost(problem) == pytest.approx(cost)
