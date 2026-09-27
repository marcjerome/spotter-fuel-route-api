# The math behind the fuel planner

This document builds the optimisation model step by step: from a two-variable LP solved by
hand, to a three-station trip solved with both algorithms, to the full model the API runs. It
then compares the algorithms on real routes.

- [1. Warm-up: a minimisation LP solved by hand](#1-warm-up-a-minimisation-lp-solved-by-hand)
- [2. The real problem in words](#2-the-real-problem-in-words)
- [3. The model](#3-the-model)
- [4. A three-station trip, solved by hand](#4-a-three-station-trip-solved-by-hand)
- [5. Algorithm 1: greedy](#5-algorithm-1-greedy)
- [6. Algorithm 2: linear programming](#6-algorithm-2-linear-programming)
- [7. Baseline and extension: naive and MILP](#7-baseline-and-extension-naive-and-milp)
- [8. Benchmark](#8-benchmark)
- [9. Shadow prices on a real route](#9-shadow-prices-on-a-real-route)
- [10. From math to code](#10-from-math-to-code)
- [References](#references)

---

## 1. Warm-up: a minimisation LP solved by hand

**Story.** A truck has two chances to buy fuel. Fuel at Station A costs \$3 per unit and at
Station B \$2 per unit. Station A comes earlier, so each unit bought there covers two units of
the route, while a unit bought at B covers one. The trip needs at least 6 units of route coverage
and at least 4 units of fuel in total. How much should it buy at each station?

**Primal (minimise cost).**

$$
\begin{aligned}
\min\quad & C = 3x_1 + 2x_2 \\
\text{s.t.}\quad & 2x_1 + x_2 \ge 6 \\
& x_1 + x_2 \ge 4 \\
& x_1, x_2 \ge 0
\end{aligned}
$$

In matrix form this is $\min\ c^\top x$ subject to $Ax \ge b,\ x \ge 0$, with

$$
A = \begin{bmatrix} 2 & 1 \\ 1 & 1 \end{bmatrix}, \quad
b = \begin{bmatrix} 6 \\ 4 \end{bmatrix}, \quad
c = \begin{bmatrix} 3 \\ 2 \end{bmatrix}.
$$

**Dual (maximise).** Transpose $A$, swap the roles of $b$ and $c$, and flip $\ge$ to $\le$:

$$
\begin{aligned}
\max\quad & P = 6y_1 + 4y_2 \\
\text{s.t.}\quad & 2y_1 + y_2 \le 3 \\
& y_1 + y_2 \le 2 \\
& y_1, y_2 \ge 0
\end{aligned}
$$

The dual has $\le$ constraints, so adding slack variables and running the ordinary maximisation
simplex method gives $y_1 = 1,\ y_2 = 1,\ P^* = 10$.

**Strong duality.** At the optimum, the primal and dual objectives are equal, so the cheapest
possible cost is $C^* = P^* = 10$. The primal solution $x_1 = 2,\ x_2 = 2$ confirms it:

$$
C = 3(2) + 2(2) = 10, \qquad 2(2) + 2 = 6\ \checkmark, \qquad 2 + 2 = 4\ \checkmark
$$

Both constraints are *binding*: they hold with equality.

**What the dual values mean.** $y_1$ and $y_2$ are the constraints' *shadow prices*: how much the
minimum cost rises if a requirement grows by one unit.

| Change | New optimum | New cost | Increase | Shadow price |
| --- | --- | ---: | ---: | ---: |
| $2x_1 + x_2 \ge 7$ | $x = (3, 1)$ | 11 | +1 | $y_1 = 1$ |
| $x_1 + x_2 \ge 5$ | $x = (1, 4)$ | 11 | +1 | $y_2 = 1$ |

**The lesson for fuel.** Station B is cheaper, yet the answer still buys at A. Fuel at B can't
help with the part of the trip before B. Cheap fuel is only useful if the truck can reach it.
The real model turns that abstract "coverage" constraint into literal fuel and distance.

> This story is a learning analogy; the real model below uses explicit fuel-balance constraints.
> The toy problem is also *not* a transportation problem (factories to cities, solved with
> Northwest Corner or Vogel's method). Refuelling along a route is an *inventory* problem: fuel
> is a stock that is topped up at stations and used up while driving.

---

## 2. The real problem in words

A truck drives a fixed route of $D$ miles. Along the way it passes $n$ stations, each at a known
mile marker with a known price. The truck:

- burns $1/\text{mpg} = 1/10$ gallon per mile,
- holds at most $C = 50$ gallons (500 miles of range),
- starts with $f_0$ gallons,
- must never run dry, and cannot hold more than a full tank.

**Decide** how many gallons to buy at each station so the truck reaches the destination at
the lowest total cost.

---

## 3. The model

### Notation

| Symbol | Meaning | Value / unit |
| --- | --- | --- |
| $i = 1..n$ | stations, sorted by position along the route | |
| $d_i$ | mile marker of station $i$ | miles |
| $p_i$ | price at station $i$ | \$/gal |
| $D$ | route length | miles |
| $m$ | fuel economy | 10 mi/gal |
| $C$ | tank capacity | 50 gal |
| $f_0$ | fuel at the start | gal (0 to 50) |
| $x_i$ | **decision:** gallons bought at station $i$ | gal |
| $f_i$ | fuel in the tank on arrival at station $i$ | gal |

### Inventory form (the textbook version)

Fuel works like inventory: what you arrive with, plus what you buy, minus what the next leg
burns.

$$
\begin{aligned}
\min\quad & \sum_{i=1}^{n} p_i\, x_i && \text{total fuel cost} \\
\text{s.t.}\quad
& f_1 = f_0 - \frac{d_1}{m} && \text{drive to the first station} \\
& f_{i+1} = f_i + x_i - \frac{d_{i+1} - d_i}{m} && \text{fuel balance between stations} \\
& f_n + x_n - \frac{D - d_n}{m} \ge 0 && \text{reach the destination} \\
& f_i \ge 0 && \text{never run dry} \\
& f_i + x_i \le C && \text{never overfill} \\
& x_i \ge 0
\end{aligned}
$$

### Cumulative form (what the code solves)

Substituting the balance equation repeatedly removes the $f_i$ variables. The fuel on arrival at
station $i$ is the starting fuel, plus everything bought before $i$, minus everything burned
getting there:

$$
f_i = f_0 + \sum_{k \lt i} x_k - \frac{d_i}{m}
$$

So the model needs only the $n$ purchase variables:

$$
\begin{aligned}
\min\quad & \sum_i p_i\, x_i \\
\text{s.t.}\quad
& \sum_{k \lt i} x_k \ \ge\ \frac{d_i}{m} - f_0 && \text{arrive at } i \text{ with fuel} \ge 0 \quad (n \text{ rows}) \\
& \sum_{k\le i} x_k \ \le\ C - f_0 + \frac{d_i}{m} && \text{leave } i \text{ with fuel} \le C \quad (n \text{ rows}) \\
& \sum_{k} x_k \ \ge\ \frac{D}{m} - f_0 && \text{reach the destination} \quad (1 \text{ row}) \\
& 0 \le x_i \le C
\end{aligned}
$$

Each constraint is a running total of purchases, so the constraint matrix is **lower
triangular**: strictly lower for the "arrive" rows and including the diagonal for the "leave"
rows. With 3 stations:

$$
\underbrace{\begin{bmatrix} 0&0&0 \\ 1&0&0 \\ 1&1&0 \end{bmatrix}}_{\text{arrive: } \ge}
\qquad
\underbrace{\begin{bmatrix} 1&0&0 \\ 1&1&0 \\ 1&1&1 \end{bmatrix}}_{\text{leave: } \le}
\qquad
\underbrace{\begin{bmatrix} 1&1&1 \end{bmatrix}}_{\text{finish: } \ge}
$$

The two forms describe exactly the same set of plans. The cumulative form has half the variables
and no equality constraints, which suits a sparse solver.

---

## 4. A three-station trip, solved by hand

```
Origin/A ─────── 300 mi ─────── B ──── 150 mi ──── C ─────────────── 450 mi ─────────────── LA
 $3.50/gal                    $3.00/gal         $3.40/gal
 mile 0                       mile 300          mile 450                                mile 900
```

The trip is $D = 900$ miles, so it needs 90 gallons. The tank holds 50 gallons (500 miles) and
starts empty ($f_0 = 0$).

### The LP, written out

$$
\begin{aligned}
\min\quad & 3.5x_1 + 3.0x_2 + 3.4x_3 \\
\text{s.t.}\quad
& x_1 \ge 30 && \text{reach B (300 mi = 30 gal)} \\
& x_1 + x_2 \ge 45 && \text{reach C (450 mi)} \\
& x_1 + x_2 + x_3 \ge 90 && \text{reach LA (900 mi)} \\
& x_1 \le 50 && \text{tank at A} \\
& x_1 + x_2 \le 80 && \text{tank at B: } (x_1 - 30) + x_2 \le 50 \\
& x_1 + x_2 + x_3 \le 95 && \text{tank at C: } (x_1 + x_2 - 45) + x_3 \le 50 \\
& x_1, x_2, x_3 \ge 0
\end{aligned}
$$

**Reasoning it out.** B is the cheapest station, so buy as much there as possible. The tank at B
caps $x_1 + x_2 \le 80$, and the truck must buy at least 30 gallons at A to reach B at all. So
$x_1 = 30$ and $x_2 = 50$. The remaining $90 - 80 = 10$ gallons come from C.

$$
x^* = (30,\ 50,\ 10), \qquad \text{cost} = 30(3.5) + 50(3.0) + 10(3.4) = 105 + 150 + 34 = 289 \text{ dollars}
$$

### The dual, and strong duality again

To use the recipe from section 1, write every constraint as $\ge$: multiply the three tank rows
by $-1$. That gives dual variables $y_1..y_6$, one per row:

$$
\begin{aligned}
\max\quad & 30y_1 + 45y_2 + 90y_3 - 50y_4 - 80y_5 - 95y_6 \\
\text{s.t.}\quad
& y_1 + y_2 + y_3 - y_4 - y_5 - y_6 \le 3.5 && (x_1) \\
& y_2 + y_3 - y_5 - y_6 \le 3.0 && (x_2) \\
& y_3 - y_6 \le 3.4 && (x_3) \\
& y \ge 0
\end{aligned}
$$

The optimal dual is $y = (0.5,\ 0,\ 3.4,\ 0,\ 0.4,\ 0)$. It satisfies all three constraints with
equality, and

$$
30(0.5) + 90(3.4) - 80(0.4) = 15 + 306 - 32 = 289 = \text{primal cost}\ \checkmark
$$

**Each shadow price has a plain meaning**, and re-solving with the solver confirms each one:

| Constraint | Shadow price | Meaning |
| --- | ---: | --- |
| reach B | \$0.50 | B needs one more gallon of lead-in, which must come from A (\$3.50) instead of B (\$3.00). |
| reach LA | \$3.40 | One more gallon of trip is bought at the last station, C. |
| tank at B | \$0.40 | One more gallon of tank at B would replace a C gallon (\$3.40) with a B gallon (\$3.00). |
| reach C, tanks at A and C | \$0 | Not binding: loosening them changes nothing. |

---

## 5. Algorithm 1: greedy

### The rule

At each station, with $f$ gallons in the tank:

1. **If a cheaper station is within one tank:** buy just enough to reach the *first* such
   station, and go there.
2. **Else, if the destination is within one tank:** buy just enough to finish.
3. **Else:** fill the tank, and go to the *cheapest* station within range.

In short: never carry expensive fuel past a cheaper station, and when this is the cheapest fuel
in reach, take as much as the tank holds.

### Trace on the three-station trip

| At | Fuel on arrival | Rule | Decision | Cost |
| --- | ---: | --- | --- | ---: |
| A (\$3.50, mile 0) | 0 gal | 1: B is cheaper and 300 mi away | buy 30 gal, exactly enough for B | \$105 |
| B (\$3.00, mile 300) | 0 gal | 3: nothing cheaper within 500 mi, LA is 600 mi away | fill up (50 gal), go to C | \$150 |
| C (\$3.40, mile 450) | 35 gal | 2: LA is 450 mi away | buy 45 − 35 = 10 gal | \$34 |
| **Total** | | | **90 gal, 3 stops** | **\$289** |

That's the same answer as the LP.

### Why greedy is optimal

This is a short exchange argument. Suppose an optimal plan carries a gallon bought at station
$i$ past a cheaper station $j$ that the truck could reach. Buying that gallon at $j$ instead is
feasible (the truck reaches $j$ anyway and has room) and costs less, which contradicts
optimality. So an optimal plan buys only enough at $i$ to reach the next cheaper station.
When no cheaper station is in range, every gallon bought here is the cheapest gallon available
for the next 500 miles, so filling the tank is never worse.

This is the classic result for the **fixed-route vehicle refuelling problem** (Lin, Gertsch &
Russell 2007). It holds because fuel can be bought in any amount; with a fixed cost per stop,
greedy is no longer optimal (see the MILP in section 7).

### Pseudocode

```text
cur ← first station;  fuel ← f0 − d_cur / m
loop
    j ← first station after cur with d_j − d_cur ≤ R and p_j < p_cur
    if j is none and D − d_cur ≤ R:              # rule 2
        buy max(0, (D − d_cur)/m − fuel);  stop
    if j exists:                                  # rule 1
        buy max(0, (d_j − d_cur)/m − fuel);  move to j
    else:                                         # rule 3
        buy C − fuel;  move to the cheapest station within R of cur
```

**Complexity:** $O(n \cdot k)$, where $k$ is the number of stations within one tank. It runs in
about 0.1 ms for a coast-to-coast route. A monotonic stack gives $O(n)$, but it isn't needed at
this size.

---

## 6. Algorithm 2: linear programming

The cumulative-form model from section 3 is passed to `scipy.optimize.linprog` using the
**HiGHS** solver. HiGHS picks the algorithm itself and typically uses the dual simplex method:
the same primal–dual idea as section 1, applied to $2n + 1$ constraint rows instead of 2.

| | Greedy | LP (HiGHS) |
| --- | --- | --- |
| Finds the optimum | yes, when fuel can be bought in any amount | yes, always (within solver tolerance) |
| Speed | about 0.1 ms | about 10–40 ms (building the matrix and solving) |
| Explains the result | the trace table | shadow prices (section 9) |
| Easy to extend | no: every new rule needs a new proof | yes: add a constraint (reserve, minimum fill, stop cost) |
| Role in the API | default strategy | checks greedy, and is the base for the MILP |

The test suite solves 75 random problems (3 starting fuel levels × 25 seeds) with both
algorithms and checks the costs match to $10^{-7}$ relative tolerance.

---

## 7. Baseline and extension: naive and MILP

### Naive (the baseline)

The naive driver fills up only when the tank can't reach the next station, then fills to the
top. The final fill buys just enough to finish, so every strategy buys the same total gallons
and the comparison is only about price. It uses no price information at all.

On the three-station trip, the truck can't reach B empty, so it fills 50 gal at A (\$175),
passes B with 20 gal, and buys 40 gal at C to finish (\$136). **Total \$311, 2 stops**, which is
\$22 (7.6%) more than the optimum.

### MILP (fewer stops)

The optimal plans often make many small top-ups. To put a price on each stop, add a binary
$y_i$ ("stop at station $i$") and a per-stop penalty $s$:

$$
\begin{aligned}
\min\quad & \sum_i p_i\, x_i + s \sum_i y_i \\
\text{s.t.}\quad & \text{all constraints from section 3} \\
& x_i \le C\, y_i && \text{buy only where you stop} \\
& y_i \in \{0, 1\}
\end{aligned}
$$

On the three-station trip, the best 2-stop plan is A = 45 gal and C = 45 gal (\$310.50), against
3 stops for \$289:

$$
289 + 3s \ \text{ vs. } \ 310.50 + 2s \quad\Longrightarrow\quad \text{2 stops are better once } s > 21.50
$$

The solver agrees: with $s = 20$ it returns 3 stops, and with $s = 22$ it returns 2.

MILP is NP-hard in general. HiGHS solves it by branch and bound with a 2 s time limit, so on
coast-to-coast routes it can return the best plan found so far rather than a proven optimum.

---

## 8. Benchmark

`python manage.py benchmark --repeat 5` runs 8 real routes (OSRM geometry, the real price sheet,
stations within 10 miles, cheapest station per town) with an empty and a full starting tank. The
raw results are in [`benchmark.csv`](benchmark.csv).

### Summary (15 feasible runs)

| Algorithm | Avg saving vs naive | Avg gap to optimum | Max gap | Optimal | Avg stops | Avg solve time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| naive | 0.0% | 9.56% | 28.47% | 2/15 | 2.9 | 0.09 ms |
| **greedy** | **8.3%** | **0.00%** | **0.00%** | **15/15** | 9.3 | **0.11 ms** |
| lp | 8.3% | 0.00% | 0.00% | 15/15 | 9.6 | 12.43 ms |
| milp \$10/stop | 6.7% | 1.76% | 3.89% | 2/15 | **3.5** | 479.44 ms |
| milp \$50/stop | 4.4% | 4.56% | 28.47% | 2/15 | 3.1 | 651.86 ms |

"Gap to optimum" compares *fuel cost only* with the LP. The MILP's gap is intentional: it
optimises fuel plus the stop penalty. Naive's two "optimal" runs are trips a full tank covers,
or ones with a single sensible stop. The San Francisco → Salt Lake City run with an empty tank
is infeasible: the price sheet has no station within 243 miles of San Francisco on that route.

### Per route (start with an empty tank)

Each cell shows fuel cost · number of stops · median solve time.

| Route | Miles | Stations | naive | greedy | lp | milp \$10/stop | milp \$50/stop |
| --- | ---: | ---: | --- | --- | --- | --- | --- |
| New York → Los Angeles | 2,810 | 223 | \$929.49 · 6 · 0.21 ms | \$852.61 · 20 · 0.23 ms | \$852.61 · 20 · 33.07 ms | \$865.70 · 7 · 1,028 ms | \$916.38 · 6 · 2,015 ms* |
| Seattle → Miami | 3,303 | 176 | \$1,101.41 · 7 · 0.14 ms | \$1,026.91 · 21 · 0.21 ms | \$1,026.91 · 22 · 18.79 ms | \$1,042.88 · 8 · 1,914 ms | \$1,043.93 · 8 · 2,011 ms* |
| Chicago → Houston | 1,083 | 81 | \$368.06 · 3 · 0.09 ms | \$318.14 · 10 · 0.06 ms | \$318.14 · 10 · 8.07 ms | \$329.84 · 3 · 481 ms | \$329.84 · 3 · 869 ms |
| Dallas → Chicago | 961 | 84 | \$286.55 · 2 · 0.05 ms | \$273.31 · 6 · 0.11 ms | \$273.31 · 6 · 7.50 ms | \$281.46 · 2 · 83 ms | \$281.46 · 2 · 26 ms |
| Denver → Atlanta | 1,394 | 93 | \$449.07 · 3 · 0.07 ms | \$412.65 · 13 · 0.07 ms | \$412.65 · 14 · 7.61 ms | \$419.74 · 4 · 379 ms | \$433.56 · 3 · 92 ms |
| Boston → Washington | 439 | 85 | \$188.83 · 1 · 0.03 ms | \$146.98 · 8 · 0.10 ms | \$146.98 · 9 · 7.16 ms | \$149.74 · 3 · 394 ms | \$188.83 · 1 · 21 ms |
| Phoenix → Kansas City | 1,248 | 38 | \$448.05 · 3 · 0.04 ms | \$363.19 · 5 · 0.05 ms | \$363.19 · 5 · 3.78 ms | \$371.40 · 3 · 109 ms | \$371.40 · 3 · 159 ms |

\* Hit the 2 s time limit, so this is the best plan found, not a proven optimum. For New York →
Los Angeles at \$50/stop, the 7-stop plan found at \$10/stop has a lower objective
($865.70 + 7 \times 50 = 1215.70$ against $916.38 + 6 \times 50 = 1216.38$).

### What the numbers say

1. **Greedy and LP always agree on cost**, and greedy is about 100× faster. That's why greedy is
   the default and LP is the check.
2. **Planning is worth 8.3% on average and up to 22%** (Boston → Washington), and most on routes
   with large price differences between regions.
3. **The cheapest plans make many stops.** Greedy averages 9.3 stops against naive's 2.9,
   because it tops up at every slightly cheaper station.
4. **MILP at \$10 per stop is the practical middle ground:** 3.5 stops on average for 1.8% more
   than the cheapest plan, while keeping most of the savings over naive (6.7%).
5. LP and greedy stop counts can differ by one when several plans tie on cost (degenerate LP
   solutions).

---

## 9. Shadow prices on a real route

The same dual values from sections 1 and 4, read from HiGHS for **New York → Los Angeles** with a
full starting tank (optimal cost \$698.59):

| Constraint | Shadow price | Meaning |
| --- | ---: | --- |
| tank at Overton, NE (\$2.899) | \$0.080/gal | cheap Nebraska fuel the plan would buy more of |
| tank at Big Springs, NE (\$3.074) | \$0.075/gal | |
| tank at Ogallala, NE (\$3.014) | \$0.060/gal | |
| tank at Golden, CO (\$3.199) | \$0.083/gal | the last cheap fuel before the Rockies |
| **all tank limits together** | **\$0.483/gal** | value of a 51 gal tank on this trip |
| reach the destination | \$3.282/gal | the marginal gallon; about \$0.33 per extra mile |

The total tank value includes the tank-row duals (\$0.383) and the purchase upper bounds
$x_i \le C$ (\$0.100). Re-solving with a 51 gal tank saves exactly \$0.483.

Shadow prices are only exact for small changes, while the same constraints stay binding. A
60 gal tank saves \$4.83, less than $10 \times 0.483$, because the plan changes as capacity grows.

---

## 10. From math to code

| Math | Code |
| --- | --- |
| $d_i,\ p_i,\ D,\ C,\ m,\ f_0,\ s$ | `FuelProblem.positions, prices, total_miles, tank_gallons, mpg, start_fuel_gallons, stop_penalty` in [`planner/optimizers.py`](../planner/optimizers.py) |
| $x_i$ | `FuelPlan.purchases` |
| $f_i$ | `FuelPlan.arrival_fuel` (computed with the cumulative formula) |
| Lower-triangular constraints | `_fuel_constraints()` |
| Greedy (section 5) | `greedy()` |
| LP (section 6) | `linear_program()`, using `linprog(method="highs")` |
| MILP (section 7) | `mixed_integer_program()`, using `milp()` |
| Naive (section 7) | `naive()` |
| Is there a feasible plan at all? | `check_feasible()`: no gap between stations longer than 500 mi |
| Stations near the route and their $d_i$ | [`planner/station_index.py`](../planner/station_index.py) (KD-tree) |
| Three-station trip and toy LP (sections 1 and 4) | `TestWorkedExamples` in [`planner/tests/test_optimizers.py`](../planner/tests/test_optimizers.py) |
| Benchmark (section 8) | [`planner/management/commands/benchmark.py`](../planner/management/commands/benchmark.py) |

---

## References

- S.-H. Lin, N. Gertsch, J. R. Russell. *A linear-time algorithm for finding optimal vehicle
  refueling policies.* Operations Research Letters 35(3), 2007. The greedy result for a fixed route.
- S. Khuller, A. Malekian, J. Mestre. *To fill or not to fill: The gas station problem.* ACM
  Transactions on Algorithms 7(3), 2011. Covers limited numbers of stops and route choice.
- Q. Huangfu, J. A. J. Hall. *Parallelizing the dual revised simplex method.* Mathematical
  Programming Computation 10, 2018. The dual simplex method in HiGHS.
