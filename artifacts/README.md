# Querier OOMKill investigation artifacts

Evidence for the `max_chunks_per_query` enforcement change.

## Images

| File | What it shows |
| --- | --- |
| `querier-oom-heap-before-after.png` | Peak heap for one querier goroutine serving one query that matches 20,000 chunks: 1435.8 MiB with the limit unenforced, 8.9 MiB once the query is rejected. |
| `max-chunks-per-query-evidence.png` | `git grep` on `main` showing `MaxChunksPerQuery` had no callers, plus the new tests passing. |

## Raw output

`heap-harness-output.txt` is the verbatim `go test -v` output the chart is built from.

## Reproducing the measurement

`heap_harness_test.go.txt` is a throwaway harness kept out of the package because the
unbounded case takes about nine minutes. To run it:

```bash
cp artifacts/heap_harness_test.go.txt pkg/storage/heap_harness_test.go
go test ./pkg/storage/ -run TestQuerierHeapUnderChunkLimit -v -count=1
rm pkg/storage/heap_harness_test.go
```

Two caveats on the numbers. The wall clock is dominated by the test's mock chunk client,
which rescans every chunk for each batch, so it is a relative signal rather than a
prediction of production latency. And the "after" query is rejected rather than served more
cheaply — converting an unbounded query into a fast HTTP 400 is the whole point of the change.
