# coverage-decision-service

[![CI](https://github.com/jkelly-dev1/coverage-decision-service/actions/workflows/ci.yml/badge.svg)](https://github.com/jkelly-dev1/coverage-decision-service/actions/workflows/ci.yml)

A card is presented. A service has a stated budget to answer whether the
business on the other end is one the plan covers. This measures what happens
when it cannot.

The question this repository refuses to answer is "what is the p99". A p99 from
one machine with the data one hop away and no network in between is worth almost
nothing, and publishing one as though it generalized would be the least honest
thing here. What is measured instead is the SHAPE of the degradation, what the
kill switch costs in decisions, what a cache TTL gets wrong, and whether a
replayed webhook is genuinely idempotent. The last two do not depend on the
hardware at all.

A learning project. Nothing is claimed here without a test behind it, the
figures are read from the shipped results files, and the predictions were
written before the runs. The ones that were refuted stayed in.

What you need to run it: Docker and Python 3.11 or later. `docker compose up -d`
starts one Postgres and one small Python service, both bound to loopback. There
is no corpus to download and no cloud account: the members, merchants, coverage
changes and authorizations are a pure function of a seed. The test suite needs
neither Docker nor Postgres.

Four questions, and the numbers that answer them:

1. Does a configured timeout actually bound the tail?
2. Fail-open or fail-closed, once the two errors are priced?
3. What does a cache buy, what does it get wrong, and what does the kill
   switch cost?
4. Is a replayed webhook genuinely idempotent?

In one paragraph. The same 50 ms timeout produces a tail 10.8 times longer
when it does not cover the wait for a connection, and every individual database
call respected the budget in both cases. Fail-open is the right choice only
while an unexpected bill costs less than 0.47 of a member turned away, and that
crossover is set by the coverage base rate rather than by anything about the
service. A 250 ms cache TTL buys a 28.7 percent hit rate for no additional error
at all, while a 30 second TTL gets 78 percent of the changed-coverage decisions
wrong. And check-then-insert removes 99.5 percent of duplicate decisions without
removing all of them, which is the worst kind of guarantee: it survives every
test written by hand and fails at volume.

## The world it runs against

Generated, not downloaded. Every value is a pure function of a seed, so a clone
reproduces the same traffic and every number below. No real person, provider,
card, merchant or payment appears anywhere, and there is no payment processor:
the webhook is a shape, not an implementation of anybody's API.

| | |
| --- | --- |
| members, on three plans | 5,000 |
| merchants | 2,000, of which 1,111 are health providers |
| coverage rows at the start | 3,333 |
| coverage changes during the run | 194 |
| authorization deliveries | 20,000 |
| distinct events among them | 18,992 |
| deliveries that are a processor retry | 1,008 |
| share that should be approved | 32.2 percent |

Traffic is concentrated on a minority of merchants: the busiest 10
percent take 68.8 percent of the volume. Real card traffic is heavily skewed,
and uniform merchant selection would have made the cache experiment a flat line
by construction, since 20,000 events over 2,000 merchants is ten touches each.

The answer key records what was true at each event's own timestamp, not what is
true at the end. An authorization arriving one millisecond before a coverage
change has a different right answer from the one after it, and a truth table
holding only the final state would score a correct service as wrong.

## 1. Does a timeout bound the tail?

Two arrangements of the same 50 ms budget:

- **budget covers the queue**. The deadline starts when the request arrives, and
  time spent waiting for a database connection comes out of it.
- **statement timeout only**. Wait as long as it takes for a connection, then
  give the query the full 50 ms. This is what a statement timeout alone buys,
  and it is what most services actually have.

Experiment 1 drives 3,000 requests at concurrency 64 against a pool of 16, which is above
saturation: below saturation the two are indistinguishable, so an experiment run
at low concurrency would report that the distinction does not matter.

### The no-op floor, measured first and published

`GET /healthz` touches no connection, no cache and no lock. It is what the
harness costs before any decision logic runs, and every figure below is only
meaningful above it.

| p50 | p95 | p99 | max | throughput |
| --- | --- | --- | --- | --- |
| 5,573 us | 16,019 us | 22,435 us | 33,955 us | 9,407 rps |

### What each arrangement does as the datastore slows

| Injected delay | p50 (covers queue) | p99 (covers queue) | p50 (statement only) | p99 (statement only) |
| --- | --- | --- | --- | --- |
| 0 ms | 16,306 us | 53,365 us | 10,369 us | 60,765 us |
| 10 ms | 37,880 us | 51,216 us | 42,626 us | 140,239 us |
| 25 ms | 50,231 us | 56,855 us | 105,209 us | 276,194 us |
| 40 ms | 50,172 us | 54,560 us | 166,346 us | 548,015 us |
| 60 ms | 50,405 us | 58,281 us | 207,020 us | 686,413 us |
| 200 ms | 50,455 us | 59,069 us | 206,418 us | 677,645 us |

The worst p99 is 63,595 us when the budget covers the queue and 686,413 us when
it does not: the same configured timeout, a tail 10.8 times longer. Every
individual database call respected 50 ms in both runs. The difference is
entirely in what the caller was made to wait for before the call started.

The prediction was that a configured timeout bounds the tail, and it is refuted.
Against a reference bound of the budget plus the measured floor (72,435 us), the
sound arrangement lands at 0.88 of it and the naive one at 9.48. The absolute
verdict for the sound arrangement sits near the boundary and moves between runs,
so the ratio is the headline and the verdict is not: the 99th percentile of a
sum is not the sum of the 99th percentiles, and a request unlucky in the
scheduler and unlucky in the queue lands beyond both.

### Two things the table says that are easy to miss

A budget costs you real answers before anything is even slow. At zero injected
delay, only 93.47 percent of requests got an answer from the datastore; the rest
hit the budget waiting for a connection. Bounding the tail is not free, and at
25 ms delay the sound arrangement answers 19.97 percent of requests from the
datastore while the naive one answers 100 percent: slowly.

Shedding load sustains more throughput than queueing it. At 200 ms delay the
sound arrangement serves 1,203 rps and the naive one 303. Giving up early
returns connections to the pool instead of holding them for callers who have
already given up.

Treat the microseconds as the shape and not the answer. One machine, loopback,
a Python service, a warm cache and a table that fits in memory.

## 2. Fail-open or fail-closed, priced

When the budget is blown the service must still answer.

- **fail-closed** denies: a covered member is turned away at the front desk.
- **fail-open** approves: an uncovered member is sent an unexpected bill.

This is the one section whose result does not depend on the machine it ran on.
The crossover is arithmetic over a stated cost model and a measured error count.

Experiment 2 replays 6,000 distinct events, 1,921 of them covered, under both policies:

| Injected delay | Policy | Fell back | Wrongly denied | Wrongly approved | Accuracy |
| --- | --- | --- | --- | --- | --- |
| 0 ms | closed | 6.93% | 149 | 3 | 0.9747 |
| 0 ms | open | 6.28% | 11 | 264 | 0.9542 |
| 25 ms | closed | 80.32% | 1,549 | 0 | 0.7418 |
| 25 ms | open | 80.18% | 3 | 3,280 | 0.4528 |
| 60 ms | closed | 100.00% | 1,921 | 0 | 0.6798 |
| 60 ms | open | 100.00% | 0 | 4,079 | 0.3202 |
| 200 ms | closed | 100.00% | 1,921 | 0 | 0.6798 |
| 200 ms | open | 100.00% | 0 | 4,079 | 0.3202 |

The 0 ms rows are the control. The fallback still fires on 416 and 377 of the
6,000 requests (6.9% and 6.3%) and produces every error in those rows, so
"barely consulted" is too strong; what is true is that the two runs' error
TOTALS differ by only 123 -- 152 against 275. That is a difference of totals, not
a count of decisions that differ: the two runs fell back on different requests,
so the number of decisions that actually disagree is at least 123 and at most
427, and this run does not record it.

### Where the answer flips

Cost is measured in members turned away. An unexpected bill costs `price` of
them, and the price is SWEPT because this repository does not know the real one
and neither does anybody who has not asked an actuary and a member services
team.

| Price of a bill | Cost of fail-closed | Cost of fail-open | Winner |
| --- | --- | --- | --- |
| 0.25 | 1,921 | 1,019.75 | fail-open |
| 0.50 | 1,921 | 2,039.5 | fail-closed |
| 1.00 | 1,921 | 4,079 | fail-closed |
| 3.00 | 1,921 | 12,237 | fail-closed |
| 10.00 | 1,921 | 40,790 | fail-closed |
| 30.00 | 1,921 | 122,370 | fail-closed |

The prediction that the winner flips inside the swept range held, and the
crossover is at 0.4709. At full degradation every decision is a fallback, so the
comparison reduces to arithmetic: fail-open wins exactly while

```
price  <  base_coverage_rate / (1 - base_coverage_rate)  =  0.3202 / 0.6798  =  0.4709
```

The crossover is a property of the book of business, not of the service; once
the service is fully degraded. Measured per delay it is 0.5287 at 0 ms, 0.4713
at 25 ms, 0.4647 at 40 ms, and 0.4709 at both 60 ms and 200 ms. It settles once
every request falls back. The settled value is the property of the book: at 0 ms
only a fraction of requests reach the fallback at all, so the ratio there is
still partly about the service. The consequence is visible in the priced table
above. At a price of 0.50 the winner is open at 0 ms and closed at 25 ms. What
decides fail-open versus fail-closed is what share of your traffic is covered
and what a wrong bill costs you relative to a wrong refusal. Nothing about your
p99 enters into it.

For this book, fail-open is defensible only if an unexpected bill costs less
than about half of turning a covered member away. That is an uncomfortable
position to defend, so the number is published rather than the conclusion.

## 3. What a cache buys, and what it gets wrong

Experiment 3 replays 12,000 events with a 20 ms datastore delay standing in for a real network hop,
and a generous 500 ms timeout so that NOTHING falls back and the cache is not
confounded with the budget. The world holds 194 coverage changes and 130 of
them fall inside the replayed window and are applied, which is the only reason
a cached answer can be stale. (The experiment replays the first 12,000
non-replay events, ending at 379,830 ms of a 600,000 ms timeline; 134 changes
are due by then and the 40-chunk application schedule applies 130.)

The denominator is stated and it is not 12,000. Only the 127 decisions whose
coverage had actually changed can be wrong because of staleness. Reporting stale
errors against every decision would divide a real effect by a number chosen to
make it look small.

| TTL | Hit rate | p50 | Accuracy | Wrong among the 127 affected |
| --- | --- | --- | --- | --- |
| none | 0.0000 | 20,902 us | 0.9998 | 2 (0.0157) |
| 250 ms | 0.2868 | 20,520 us | 0.9998 | 2 (0.0157) |
| 1 s | 0.4914 | 20,239 us | 0.9994 | 7 (0.0551) |
| 5 s | 0.7027 | 4 us | 0.9965 | 42 (0.3307) |
| 30 s | 0.7518 | 4 us | 0.9918 | 99 (0.7795) |
| 300 s | 0.7518 | 4 us | 0.9918 | 99 (0.7795) |

### The marginal trade is what decides a TTL

The averages hide the shape. What matters is what the NEXT increment buys and
what it costs.

| Step | Hit rate gained | Affected error added | Error per point of hit rate |
| --- | --- | --- | --- |
| none to 250 ms | +0.2868 | +0.0000 | 0.0000 |
| 250 ms to 1 s | +0.2047 | +0.0394 | 0.1924 |
| 1 s to 5 s | +0.2112 | +0.2756 | 1.3046 |
| 5 s to 30 s | +0.0491 | +0.4488 | 9.1441 |
| 30 s to 300 s | +0.0000 | +0.0000 | n/a |

The price of a point of hit rate rises by more than an order of magnitude across
the sweep. The last increment that gains anything costs 9.1441 error per point;
the first costs nothing at all. A 250 ms TTL buys a 28.68 percent hit rate at
no additional error over running with no cache at all.

The exact multiple between the cheapest and dearest step is not published,
because it is a ratio against a denominator near zero and it moves between runs
by a factor of two while the ordering does not. The per-step figures in the
table are what reproduce.

That REFUTES the prediction that every gain in hit rate is paid for with a
proportional gain in wrong answers. It is not a straight trade; it is a free
region followed by a cliff. The other prediction, that a longer TTL never hurts
latency, HELD. Median latency never rose, and past 30 s nothing changes at all
because the working set is fully resident.

### What the kill switch actually costs

"We have a kill switch" is a claim about configuration. This is the
measurement. The same 12,000 events, answered without touching the datastore:

| Kill switch answers | p50 | Throughput | Decisions changed | Accuracy |
| --- | --- | --- | --- | --- |
| deny everything | 1 us | 7,410 rps | 3,794 (31.62%) | 0.6821 |
| approve everything | 1 us | 7,009 rps | 8,206 (68.38%) | 0.3179 |

It answers in a microsecond and it changes a third or two thirds of the
decisions depending on which way it is set. The two shares sum to 1 by
construction: one flips exactly the approvals and the other exactly the denials.
A kill switch is not a safety feature you can leave undecided; it is a
pre-committed answer to "which error would you rather make to everybody at
once".

## 4. Is a replayed webhook idempotent?

A payment processor retries whatever it did not get an acknowledgement for.
Three implementations, driven concurrently against the real service:

- **none**: no guard, as a baseline.
- **check-then-insert**; look for an existing decision, write one if there is
  none. The implementation everybody writes first.
- **unique constraint**. The database refuses the second row and the caller is
  handed the decision that already stands.

This result is a correctness property rather than a timing one. Whether two
concurrent deliveries can both pass a check does not depend on how fast this
machine is.

SPREAD: the natural arrival stream, 8,000 deliveries at concurrency 32, where a
replay lands thousands of events after its original:

| Guard | Deliveries | Distinct events | Rows written | Duplicates | Contradictory |
| --- | --- | --- | --- | --- | --- |
| none | 8,000 | 7,575 | 8,000 | 425 | 0 |
| check-then-insert | 8,000 | 7,575 | 7,581 | 6 | 0 |
| unique constraint | 8,000 | 7,575 | 7,575 | 0 | 0 |

BURST: 150 events delivered 20 times each, all in flight together, with a 45 ms
delay against a 50 ms budget so that some deliveries answer from the datastore
and others fall back. This is what a retry storm actually looks like: the
processor retries because it did not hear back, and it did not hear back because
the service was slow, which means the retries arrive while it is still slow.

| Guard | Deliveries | Distinct events | Rows written | Duplicates | Contradictory |
| --- | --- | --- | --- | --- | --- |
| none | 3,000 | 150 | 3,000 | 2,850 | 39 |
| check-then-insert | 3,000 | 150 | 163 | 13 | 0 |
| unique constraint | 3,000 | 150 | 150 | 0 | 0 |

The prediction that check-then-insert is idempotent is refuted. It writes 6
duplicate rows under the spread workload and 13 under the burst. The check and
the insert are separate statements with the entire decision between them, so two
deliveries can both pass the check before either writes.

It removes 99.54 percent of the duplicates without removing all of them, and
that is the worst kind of guarantee. A guard that fails openly gets fixed. A
guard that fails 0.5 percent of the time survives every test anybody writes by
hand, passes review, and produces a steady trickle of double-recorded
authorizations at volume.

Without any guard, 39 of the 150 events ended up with two contradictory
decisions on record. Both "approved" and "denied" for one authorization, which
nobody can reconcile afterward. That happens because some deliveries reached the
datastore and others fell back, so the same event genuinely got different
answers at different instants.

One part of the prediction was wrong in the other direction and is kept. Its
duplicates were expected to disagree too; they do not, and the reason is
structural rather than lucky. The deliveries that slip past the check are the
ones racing in the same instant, before any row exists, so they are all doing
identical work against an identically slow datastore and all reach the same
answer. Contradictory pairs need deliveries spread across the burst, which is
what the unguarded run has and the guarded one does not.

Returning the original decision is the requirement, not refusing the replay. A
guard that raised on the second delivery would not be idempotent, it would be
fragile: the processor retries because it did not hear back, and answering the
retry with an error guarantees it retries again. No delivery under any guard
received an error.

## Reproducing it

```
cd stack && docker compose up -d && cd ..
python3 scripts/load.py
python3 scripts/exp1_degradation.py
python3 scripts/exp2_fail_open_closed.py
python3 scripts/exp3_cache_and_kill_switch.py
python3 scripts/exp4_idempotency.py
```

About fifteen minutes end to end, most of it in the deliberately slowed sweep
points. `SAMPLE_RUN.md` is the captured output of exactly that sequence.

The test suite needs neither Docker nor Postgres:

```
pip install pytest
python -m pytest -q
python3 scripts/check_readme_numbers.py
```

Take the stack down with `cd stack && docker compose down -v`.

## Claims backed by tests

The load-bearing rows are mutation-checked and say so.

| Claim | Test |
| --- | --- |
| Fail-closed denies and fail-open approves | `tests/test_decide.py::test_fail_closed_denies_and_fail_open_approves` |
| An unknown fallback policy raises rather than silently picking one (mutation-checked: default to closed and a typo'd sweep measures the wrong policy) | `tests/test_decide.py::test_an_unknown_fallback_policy_raises_rather_than_defaulting` |
| The two fallback sources are distinguishable in the record | `tests/test_decide.py::test_the_two_fallback_sources_are_distinguishable_in_the_record` |
| The service will not grow settings by being sent them | `tests/test_decide.py::test_config_ignores_a_key_it_does_not_know` |
| The budget covers the queue by default | `tests/test_decide.py::test_the_budget_covers_the_queue_by_default` |
| The kill switch answer is a separate decision from the fallback policy | `tests/test_decide.py::test_the_kill_switch_answer_is_separate_from_the_fallback_policy` |
| A TTL of zero disables the cache entirely | `tests/test_decide.py::test_a_ttl_of_zero_disables_the_cache_entirely` |
| An entry is returned inside the TTL and not outside it | `tests/test_decide.py::test_an_entry_is_returned_inside_the_ttl_and_not_outside_it` |
| A cached denial is not confused with a miss (mutation-checked: test with `if not value` and every denied merchant becomes a permanent miss) | `tests/test_decide.py::test_a_false_answer_is_cached_and_is_not_confused_with_a_miss` |
| An expiry is counted apart from a cold miss | `tests/test_decide.py::test_an_expiry_is_counted_apart_from_a_cold_miss` |
| Two plans at the same merchant are different cache entries | `tests/test_decide.py::test_two_plans_at_the_same_merchant_are_different_cache_entries` |
| The generated world is a pure function of the seed | `tests/test_generator_and_scoring.py::test_the_world_is_a_pure_function_of_the_seed` |
| Nothing in the generator reads the clock | `tests/test_generator_and_scoring.py::test_nothing_in_the_generator_reads_the_clock` |
| A replay carries its original's id and its original's timestamp | `tests/test_generator_and_scoring.py::test_a_replay_carries_its_original_id_and_its_original_timestamp` |
| Arrival timestamps are monotonic (mutation-checked: scatter them and experiment 3 scores correct decisions as wrong) | `tests/test_generator_and_scoring.py::test_arrival_timestamps_are_monotonic_for_first_deliveries` |
| A merchant that is not a provider has no coverage row at all | `tests/test_generator_and_scoring.py::test_a_merchant_that_is_not_a_provider_has_no_coverage_row_at_all` |
| A coverage change flips the answer rather than restating it | `tests/test_generator_and_scoring.py::test_a_change_flips_the_answer_rather_than_restating_it` |
| Traffic is concentrated on a minority of merchants | `tests/test_generator_and_scoring.py::test_the_traffic_is_concentrated_on_a_minority_of_merchants` |
| Coverage at an instant uses the latest row at or before it | `tests/test_generator_and_scoring.py::test_coverage_at_an_instant_uses_the_latest_row_at_or_before_it` |
| The two error kinds are counted apart (mutation-checked: swap them and the crossover inverts) | `tests/test_generator_and_scoring.py::test_the_two_error_kinds_are_counted_apart` |
| `decide()` itself is exercised end to end against a fake pool: the kill switch answers without touching the datastore, a timeout and an error are counted apart, the cache expires, two plans do not share a cached answer, and a replayed webhook returns its ORIGINAL decision | `tests/test_decide_path.py` (mutation-checked: no-op the kill switch, raise the cache TTL to forever, drop the plan from the cache key, or disable either replay guard -- each fails) |
| A driver-side failure is not scored as a wrong decision | `tests/test_generator_and_scoring.py::test_a_failed_request_is_not_scored_as_a_wrong_decision` |
| A percentile is a value that actually occurred | `tests/test_generator_and_scoring.py::test_the_percentile_is_a_value_that_actually_occurred` |
| The no-op floor is far cheaper than a real decision (mutation-checked: re-enable Nagle and the floor becomes 42 ms) | `tests/test_results_invariants.py::test_the_noop_floor_is_far_cheaper_than_a_real_decision` |
| Nothing falls back when the datastore is not slowed | `tests/test_results_invariants.py::test_nothing_falls_back_when_the_datastore_is_not_slowed` |
| Everything falls back once the delay exceeds the budget | `tests/test_results_invariants.py::test_everything_falls_back_once_the_delay_exceeds_the_budget` |
| A budget that covers the queue bounds the tail far better | `tests/test_results_invariants.py::test_a_budget_that_covers_the_queue_bounds_the_tail_far_better` |
| The experiment ran above pool saturation | `tests/test_results_invariants.py::test_the_experiment_ran_above_pool_saturation` |
| Shedding load sustains more throughput than queueing it | `tests/test_results_invariants.py::test_shedding_load_sustains_more_throughput_than_queueing_it` |
| No driver-side errors are hidden in the latency figures | `tests/test_results_invariants.py::test_no_driver_side_errors_are_hidden_in_the_latency_figures` |
| The two policies agree when nothing falls back | `tests/test_results_invariants.py::test_the_two_policies_agree_when_nothing_falls_back` |
| Fail-closed never bills anybody and fail-open never turns anybody away | `tests/test_results_invariants.py::test_fail_closed_never_bills_anybody_and_fail_open_never_turns_anybody_away` |
| The measured crossover matches the analytic one | `tests/test_results_invariants.py::test_the_measured_crossover_matches_the_analytic_one` |
| The crossover is a function of the base rate alone | `tests/test_results_invariants.py::test_the_crossover_is_a_function_of_the_base_rate_alone` |
| The winning policy flips inside the swept price range (mutation-checked: sweep only prices above 1 and the repository concludes what it exists to argue against) | `tests/test_results_invariants.py::test_the_winning_policy_flips_inside_the_swept_price_range` |
| The staleness denominator is the affected decisions, not all of them | `tests/test_results_invariants.py::test_the_staleness_denominator_is_the_affected_decisions_not_all_of_them` |
| A longer TTL never lowers the hit rate | `tests/test_results_invariants.py::test_a_longer_ttl_never_lowers_the_hit_rate` |
| A longer TTL never reduces the staleness error | `tests/test_results_invariants.py::test_a_longer_ttl_never_reduces_the_staleness_error` |
| Nothing fell back during the cache experiment | `tests/test_results_invariants.py::test_nothing_fell_back_during_the_cache_experiment` |
| The price of a point of hit rate rises sharply with the TTL | `tests/test_results_invariants.py::test_the_price_of_a_point_of_hit_rate_rises_sharply_with_the_ttl` |
| A short TTL buys hit rate for nothing | `tests/test_results_invariants.py::test_a_short_ttl_buys_hit_rate_for_nothing` |
| The kill switch answers without touching the datastore | `tests/test_results_invariants.py::test_the_kill_switch_answers_without_touching_the_datastore` |
| What the kill switch costs is measured and not asserted | `tests/test_results_invariants.py::test_what_the_kill_switch_costs_is_measured_and_not_asserted` |
| The unique constraint writes exactly one row per event | `tests/test_results_invariants.py::test_the_unique_constraint_writes_exactly_one_row_per_event` |
| Check-then-insert is not idempotent | `tests/test_results_invariants.py::test_check_then_insert_is_not_idempotent` |
| Check-then-insert removes most duplicates without removing them all | `tests/test_results_invariants.py::test_check_then_insert_removes_most_duplicates_without_removing_them_all` |
| The unguarded run leaves contradictory decisions on record | `tests/test_results_invariants.py::test_the_unguarded_run_leaves_contradictory_decisions_on_record` |
| The burst actually mixed real answers with fallbacks | `tests/test_results_invariants.py::test_the_burst_actually_mixed_real_answers_with_fallbacks` |
| Every delivery got an answer under every guard | `tests/test_results_invariants.py::test_every_delivery_got_an_answer_under_every_guard` |
| All four experiments measured the same generated world | `tests/test_results_invariants.py::test_all_four_experiments_measured_the_same_generated_world` |

## What this does not measure

- **NOT A p99 THAT GENERALIZES.** One machine, loopback, no network, a Python
  service and a datastore one hop away. A floor is published so a reader can
  see what the harness costs; the position of the curve still does not
  transfer. The SHAPE does, and the ratio between the two arrangements does.
- **One concurrency model.** A threaded Python service is not a production
  runtime and its behavior under load is its own.
- **Synthetic traffic and synthetic truth.** The arrival pattern, the merchant
  skew, the coverage base rate and the rate at which coverage changes are all
  invented, and every count of wrong decisions is a function of them.
- **THE 20 ms datastore delay in section 3 is a stand-in and not a
  measurement.** It represents a real network hop and was chosen, not observed.
- **No real payment integration.** No processor, no card network, no money.
- **The service is not hardened.** `POST /config` lets any caller change the
  budget, the fallback policy and the kill switch at runtime. That is what makes
  the sweeps possible. A real decision service must not do it.
- **Not a healthcare or payments claim.** The setting is chosen because it makes
  the cost asymmetry concrete. This repository is not evidence of domain
  experience in either.

## Related repositories

[roster-entity-resolution](https://github.com/jkelly-dev1/roster-entity-resolution)
is the one to read directly against this repository. It resolves four
disagreeing provider rosters into one record per provider and treats the
matching threshold as a cost decision rather than a tuning parameter, sweeping
what a false match costs against a missed one. This repository makes the same
move on a different axis: fail-open against fail-closed is the same shape of
argument applied to availability instead of matching, and section 2 here ends in
a crossover for the same reason section 2 there does. The two are independent
and share no code; that one needs a single Postgres container and this one needs
Postgres plus the service.

Both follow the same rules: no claim without a test, mutation checks on the
tests that matter, every number re-derived from the shipped results by script,
and predictions recorded before the run so the refuted ones survive. Four of
the six predictions here were refuted and all four are still in the code.

## License

MIT. See `LICENSE`.
