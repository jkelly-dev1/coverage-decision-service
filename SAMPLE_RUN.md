# Sample run

Captured 2026-08-23 on one Linux machine: Postgres 17.6 and Python 3.13 in
Docker, driver on Python 3.13.7. Every command below was executed exactly as
written, in this order, after `cd stack && docker compose down -v` removed both
containers and the volume.

Two alterations, both stated here and nowhere else. The `docker compose up`
banner is reduced to its final two lines, because compose reprints each step as
it works. And the closing `check_readme_numbers.py` block is its output AFTER
the README figures were synced to this run; the captured attempt named the
previous run's figures, which is exactly what that script is for.

What moves between runs and what does not. Every microsecond figure moves, and
so do the small duplicate counts in experiment 4, which are races. What does NOT
move is the ordering and the ratios: the tail is always about ten times longer
without a queue-covering budget, the fail-open crossover is always base_rate /
(1 - base_rate) exactly, a short TTL is always free, and the unique constraint
always writes exactly one row per event. The README leads with those and says
so.

```
$ cd stack && docker compose up -d && cd ..
 Container cds-postgres  Healthy 
 Container cds-service  Started 

$ python3 scripts/load.py
generating ... ok
schema ... ok
COPY 5000
COPY 2000
COPY 3333
COPY 18992
COPY 20000

coverage.member               5000
coverage.merchant             2000
coverage.network_status       3333
truth.arrival                20000
truth.event                  18992
status changes pending         194  (applied by the driver, not loaded here)
manifest e4e234793313ea2a28c600e19e99e4118556d4fcabeff7cbbd48db7579439ca4
load verified against the generator

$ python3 scripts/exp1_degradation.py
3000 requests, concurrency 64, pool 16, budget 50 ms

no-op floor (GET /healthz, touches nothing)
  p50 5573 us   p95 16019 us   p99 22435 us   max 33955 us   9408 rps

budget covers the queue
  delay    0 ms  p50   16306  p95    50686  p99    53365  max    60060 us   real answers  93.47%   3015 rps
  delay   10 ms  p50   37880  p95    50507  p99    51216  max    53722 us   real answers  78.23%   1752 rps
  delay   25 ms  p50   50231  p95    52723  p99    56855  max    64794 us   real answers  19.97%   1345 rps
  delay   40 ms  p50   50172  p95    52065  p99    54560  max    60181 us   real answers  18.60%   1271 rps
  delay   60 ms  p50   50405  p95    54367  p99    58281  max    65256 us   real answers   0.00%   1209 rps
  delay  100 ms  p50   50295  p95    54227  p99    63595  max    77956 us   real answers   0.00%   1209 rps
  delay  200 ms  p50   50455  p95    54412  p99    59069  max    69360 us   real answers   0.00%   1204 rps

budget covers only the query (a statement timeout alone)
  delay    0 ms  p50   10369  p95    41724  p99    60765  max   101781 us   real answers 100.00%   3747 rps
  delay   10 ms  p50   42626  p95   105508  p99   140239  max   238606 us   real answers 100.00%   1450 rps
  delay   25 ms  p50  105209  p95   192941  p99   276194  max   505263 us   real answers 100.00%    595 rps
  delay   40 ms  p50  166346  p95   416688  p99   548015  max   816367 us   real answers  98.73%    358 rps
  delay   60 ms  p50  207020  p95   520262  p99   686413  max  1017262 us   real answers   0.00%    302 rps
  delay  100 ms  p50  206600  p95   514443  p99   679271  max  1025638 us   real answers   0.00%    303 rps
  delay  200 ms  p50  206418  p95   517038  p99   677645  max  1332592 us   real answers   0.00%    303 rps

reference bound = budget 50 ms + floor p99 22435 us = 72435 us
  budget covers the queue : worst p99    63595 us  0.88x the bound
  statement timeout alone : worst p99   686413 us  9.48x the bound
  THE HEADLINE: the same 50 ms timeout produces a tail 10.8 times longer
  when it does not cover the queue.

wrote results/exp1_degradation.json

$ python3 scripts/exp2_fail_open_closed.py
6000 distinct events, 1921 of them covered (0.3202)

  delay    0 ms  closed   fell back   6.93%   wrongly denied   149   wrongly approved     3   accuracy 0.9747
  delay    0 ms  open     fell back   6.28%   wrongly denied    11   wrongly approved   264   accuracy 0.9542
  delay   25 ms  closed   fell back  80.32%   wrongly denied  1549   wrongly approved     0   accuracy 0.7418
  delay   25 ms  open     fell back  80.18%   wrongly denied     3   wrongly approved  3280   accuracy 0.4528
  delay   40 ms  closed   fell back  83.43%   wrongly denied  1580   wrongly approved     1   accuracy 0.7365
  delay   40 ms  open     fell back  83.00%   wrongly denied     2   wrongly approved  3397   accuracy 0.4335
  delay   60 ms  closed   fell back 100.00%   wrongly denied  1921   wrongly approved     0   accuracy 0.6798
  delay   60 ms  open     fell back 100.00%   wrongly denied     0   wrongly approved  4079   accuracy 0.3202
  delay  200 ms  closed   fell back 100.00%   wrongly denied  1921   wrongly approved     0   accuracy 0.6798
  delay  200 ms  open     fell back 100.00%   wrongly denied     0   wrongly approved  4079   accuracy 0.3202

control at 0 ms: the two policies differ by 123 decisions out of 6000

cost is measured in members turned away; a bill costs `price` of them
delay          0.25      0.50      1.00      3.00     10.00     30.00
0 ms           open      open    closed    closed    closed    closed
25 ms          open    closed    closed    closed    closed    closed
40 ms          open    closed    closed    closed    closed    closed
60 ms          open    closed    closed    closed    closed    closed
200 ms         open    closed    closed    closed    closed    closed

at full degradation the crossover is analytic: fail-open wins while
price < base_rate / (1 - base_rate) = 0.4709
  measured winners by price: 0.25->open, 0.50->closed, 1.00->closed, 3.00->closed, 10.00->closed, 30.00->closed

prediction: held
wrote results/exp2_fail_open_closed.json

$ python3 scripts/exp3_cache_and_kill_switch.py
12000 events, 194 coverage changes, 127 events affected by one
datastore delay 20 ms, timeout 500 ms, concurrency 16

  ttl       0 ms  hits   0.00%  p50  20902  p99   29177 us  accuracy 0.9998  affected wrong   2/127 (0.0157)
  ttl     250 ms  hits  28.68%  p50  20520  p99   27900 us  accuracy 0.9998  affected wrong   2/127 (0.0157)
  ttl    1000 ms  hits  49.14%  p50  20239  p99   26671 us  accuracy 0.9994  affected wrong   7/127 (0.0551)
  ttl    5000 ms  hits  70.27%  p50      4  p99   24100 us  accuracy 0.9965  affected wrong  42/127 (0.3307)
  ttl   30000 ms  hits  75.17%  p50      4  p99   24058 us  accuracy 0.9918  affected wrong  99/127 (0.7795)
  ttl  300000 ms  hits  75.17%  p50      4  p99   23668 us  accuracy 0.9918  affected wrong  99/127 (0.7795)

marginal trade, per step
        0 -> 250     ms   hit rate +0.2868   affected error +0.0000   0.00 error per point of hit
      250 -> 1000    ms   hit rate +0.2047   affected error +0.0394   0.19 error per point of hit
     1000 -> 5000    ms   hit rate +0.2112   affected error +0.2756   1.30 error per point of hit
     5000 -> 30000   ms   hit rate +0.0491   affected error +0.4488   9.14 error per point of hit
    30000 -> 300000  ms   hit rate +0.0000   affected error +0.0000   no additional hit rate

hit rate saturates at a TTL of 30000 ms
A TTL of 250 ms buys 28.68% hit rate at NO additional error over running with no cache at all.

kill switch
  answer=False  p50     1 us    7411 rps  changed  3794 decisions (31.62%)  accuracy 0.6821
  answer=True   p50     1 us    7009 rps  changed  8206 decisions (68.38%)  accuracy 0.3179

prediction A (longer TTL never hurts latency): held
prediction B (no TTL buys hit rate for free): REFUTED

wrote results/exp3_cache_and_kill_switch.json

$ python3 scripts/exp4_idempotency.py
SPREAD: the natural arrival stream, replays land far from their originals
  none               deliveries  8000  distinct  7575  rows  8000  duplicates  425  disagreeing   0
  check_then_insert  deliveries  8000  distinct  7575  rows  7581  duplicates    6  disagreeing   0
  unique             deliveries  8000  distinct  7575  rows  7575  duplicates    0  disagreeing   0

BURST: 150 events x 20 simultaneous copies, 45 ms delay against a 50 ms budget
  none               deliveries  3000  distinct   150  rows  3000  duplicates 2850  DISAGREEING  39
  check_then_insert  deliveries  3000  distinct   150  rows   163  duplicates   13  DISAGREEING   0
  unique             deliveries  3000  distinct   150  rows   150  duplicates    0  DISAGREEING   0

check-then-insert wrote 6 duplicate rows under the spread workload
check-then-insert wrote 13 duplicate rows under the burst, 0 of them for events that ended up with two DIFFERENT answers on record
the unique constraint wrote 0 duplicates under the same burst
no guard at all wrote 2850, and left 39 of 150 events with two CONTRADICTORY decisions on record

THE SHAPE OF THE RESULT: check-then-insert removes 99.54% of the duplicates
without removing all of them, which is the worst kind of guarantee: it
survives every test anybody writes by hand and fails at volume.

prediction: REFUTED
wrote results/exp4_idempotency.json

$ python -m pytest -q
........................................................................ [ 98%]
.                                                                        [100%]
88 passed in 0.89s

$ python3 scripts/check_readme_numbers.py
69 figures re-derived from results/*.json and checked against README.md
all present
```
