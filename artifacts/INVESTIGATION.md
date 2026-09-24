# Querier OOMKills — read-path memory investigation

Triggering alert (untrusted third-party text, treated as a symptom report only):

> `[firing] Querier pods repeatedly OOMKilled` — multiple querier pods OOMKilled in the
> last 15m, p99 query latency elevated, no deploy marker.

No deploy marker plus elevated p99 points away from a regression introduced by a release and
toward **load-shape-dependent memory amplification** on the query read path: a query shape that
costs far more memory than the response it produces. That is what this investigation looked for.

## What was examined

Accumulation points on the read path, ordered by how much memory they can hold relative to the
bytes actually returned to the caller:

| Area | Bounded by | Verdict |
|---|---|---|
| `logql.readStreams` (querier log read) | query `limit` | Bounded, not a suspect |
| `AccumulatedStreams` (sharded log downstream) | query `limit` | Bounded, not a suspect |
| `split_by_interval` response accumulation | early exit once `limit` is reached | Bounded for logs; see note below |
| `mergeOrderedNonOverlappingStreams` (final page assembly) | **nothing** — allocated per entry | **Root finding** |
| `SingleTenantQuerier.awaitSeries` (`/series`) | **nothing** | Secondary finding, not fixed here |

## Root finding — one allocation per returned log line

`mergeOrderedNonOverlappingStreams` assembles the final page of a split log query. When the
splits together return more entries than the requested `limit`, it drains a priority queue of
per-label streams, popping exactly `limit` times.

The queue's `Pop` did not just pop. It copied the head stream so the unconsumed remainder could
be pushed back:

```go
if len(stream.Entries) > 1 {
    remaining := *stream          // heap-allocates a logproto.Stream
    remaining.Entries = remaining.Entries[1:]
    heap.Push(pq, &remaining)
}
```

Because the loop runs `limit` times, **the merge allocated one throwaway `logproto.Stream` for
every log line it returned**. A query returning 5,000 lines churned 5,000 short-lived objects
purely as bookkeeping, and that garbage lands in the same heap the read path is already
straining. Under concurrency this is sustained allocation rate, which is precisely what turns
into an OOMKill when the collector cannot keep pace — and it inflates p99 at the same time,
matching both halves of the alert.

The behaviour is also a cliff rather than a gradient. Measured on the existing
`BenchmarkResponseMerge`, with 100 streams × 1,000 logs across 10 responses:

| `limit` | allocs/op |
|---|---|
| 100,000 (equal to total — merge returns everything) | 811 |
| 99,999 (one less — merge goes through the queue) | 101,128 |

Asking for one fewer log line cost **125× more allocations** for a near-identical response.

### The fix

Advance the head stream in place and repair the heap (`heap.Fix`) instead of popping and
re-pushing a copy. This is the standard k-way merge formulation and allocates nothing while
draining. Output is unchanged.

`pkg/querier/queryrange/ordering.go` gains `popEntry`, which returns the next entry plus its
labels; `Pop` becomes a plain `heap.Interface` implementation.

### Measured effect

Median of 8 runs, `-benchtime 50x -cpu 4`. Full data in `bench-before.txt` / `bench-after.txt`,
chart in `merge-allocations-before-after.png`.

| Benchmark | allocs/op | B/op | ns/op |
|---|---|---|---|
| `mergeOrderedNonOverlappingStreams` (limited) | 101,128 → 1,228 (**−98.8%**) | 34.25 → 29.67 MB (−13.4%) | 14.05 → 11.63 ms (−17.2%) |
| split-heavy merge, `limit=10000` | 10,474 → 474 (**−95.5%**) | 2.54 → 2.08 MB (−18.0%) | 1.28 → 0.98 ms (−23.5%) |
| split-heavy merge, `limit=1000` | 1,374 → 374 (−72.8%) | 455.7 → 408.8 KB (−10.3%) | 180.5 → 156.0 µs (−13.5%) |
| `mergeStreams` (limited) — *control* | 1,341 → 1,341 (0.0%) | 80.36 → 80.36 MB (0.0%) | 43.44 → 42.25 ms |
| `mergeStreams` (unlimited) — *control* | 1,341 → 1,341 (0.0%) | 80.36 → 80.36 MB (0.0%) | 40.13 → 41.11 ms |
| `mergeOrderedNonOverlappingStreams` (unlimited) — *control* | 811 → 811 (0.0%) | 8.69 → 8.69 MB (0.0%) | 1.77 → 1.59 ms |

The three controls never touch the priority queue and moved 0.0% on both allocation metrics,
which confirms the change is confined to the intended path.

### Confidence that output is unchanged

`mergeOrderedNonOverlappingStreams` had **no correctness test** — only benchmarks. One was added
first and **landed green against `main`** before any production code changed, so it pins existing
behaviour rather than the rewrite. It covers 48 combinations (both directions × 5 split/stream
shapes × 5 limits) and diffs every case against `mergeStreams`, the independent
sort-everything reference implementation already in the test utils.

`go test ./pkg/...` passes; `go vet` and `gofmt` are clean.

## Scope and honest caveats

**Which process this runs in.** The merge is reached from `split_by_interval`, `querysharding`,
`engine_router`, and the log result cache — all query-frontend middleware
(`QueryFrontendTripperware` is a dependency of `QueryFrontend` only). So the saving lands on the
query-frontend, and on the **querier pod's own heap only in deployments where the two share a
process**, i.e. single binary (`-target=all`) and SSD `read`. In a microservices deployment with
separate `querier` and `query-frontend` pods, this reduces frontend pressure rather than querier
pressure. The alert named querier pods; that gap is real and worth stating rather than papering
over. It is still a read-path memory fix with a clear measured win, and it is the cleanest
behaviour-preserving one found.

**What this does not do.** It does not add or change any limit, so it cannot stop a genuinely
oversized query from exhausting memory. It reduces the cost of queries that are already being
served. If the OOMKills are driven by one pathological query rather than by aggregate allocation
rate, the fix below matters more.

## Secondary finding — unbounded `/series` accumulation in the querier (not fixed here)

This one *is* querier-local. `SingleTenantQuerier.awaitSeries`
(`pkg/querier/querier.go:419`) collects every matching series from both ingesters and the store
into `sets`, then dedupes into a second full-size slice, with **no cap at any point**:

```go
var sets [][]logproto.SeriesIdentifier
for i := 0; i < 2; i++ {
    // ...
    sets = append(sets, s...)
}
// ... then dedupe all of it into response.Series
```

Peak memory is therefore `raw_total + deduped`, driven entirely by tenant cardinality and the
requested time range. Grafana's label browser and dashboard variable queries hit `/series`
constantly with broad matchers, so this is a realistic way for a single request to OOM a querier.

It was deliberately left out of this PR. The obvious lever, `max_query_series`, defaults to 500
and is documented as *"the maximum of unique series that is returned by a **metric** query"* —
reusing it for `/series` would reject the everyday label-browser requests that legitimately
return thousands of series. Bounding this path properly needs either a new limit or streaming
dedup, which is a design decision rather than a drive-by fix, and it changes user-visible
behaviour in a way this change does not.

## Reproducing

```bash
go test ./pkg/querier/queryrange/ -run XXX \
  -bench 'BenchmarkResponseMerge' -benchmem -benchtime 50x -count=8 -cpu 4

# regenerate the report and chart
python3 artifacts/bench_report.py artifacts/bench-before.txt artifacts/bench-after.txt artifacts/
```

Note: this checkout requires Go 1.26.7 per `go.mod`; the toolchain installed in the sandbox was
1.22.2, so 1.26.7 was fetched and used explicitly for all builds, tests, and benchmarks above.

## Artifacts

| File | Contents |
|---|---|
| `merge-allocations-before-after.png` | Before/after allocation chart and full metric table |
| `merge-fix-explained.png` | The defect, the code change, and the correctness evidence |
| `bench-before.txt` / `bench-after.txt` | Raw `go test -bench` output (8 runs each) |
| `benchmark-summary.json` | Parsed medians and deltas |
| `benchmark-report.html` / `fix-explainer.html` | Sources rendered to the PNGs |
| `bench_report.py` | Parser and report generator |
