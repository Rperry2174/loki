package queryrange

import (
	"container/heap"
	"sort"

	"github.com/grafana/loki/v3/pkg/logproto"
)

/*
Utils for manipulating ordering
*/

type entries []logproto.Entry

func (m entries) start() int64 {
	if len(m) == 0 {
		return 0
	}
	return m[0].Timestamp.UnixNano()
}

type byDir struct {
	markers   []entries
	direction logproto.Direction
	labels    string
}

func (a byDir) Len() int      { return len(a.markers) }
func (a byDir) Swap(i, j int) { a.markers[i], a.markers[j] = a.markers[j], a.markers[i] }
func (a byDir) Less(i, j int) bool {
	x, y := a.markers[i].start(), a.markers[j].start()

	if a.direction == logproto.BACKWARD {
		return x > y
	}
	return y > x
}
func (a byDir) EntriesCount() (n int) {
	for _, m := range a.markers {
		n += len(m)
	}
	return n
}

func (a byDir) merge() []logproto.Entry {
	result := make([]logproto.Entry, 0, a.EntriesCount())

	sort.Sort(a)
	for _, m := range a.markers {
		result = append(result, m...)
	}
	return result
}

// priorityqueue is used for extracting a limited # of entries from a set of sorted streams
type priorityqueue struct {
	streams   []*logproto.Stream
	direction logproto.Direction
}

func (pq *priorityqueue) Len() int { return len(pq.streams) }

func (pq *priorityqueue) Less(i, j int) bool {
	if pq.direction == logproto.FORWARD {
		return pq.streams[i].Entries[0].Timestamp.UnixNano() < pq.streams[j].Entries[0].Timestamp.UnixNano()
	}
	return pq.streams[i].Entries[0].Timestamp.UnixNano() > pq.streams[j].Entries[0].Timestamp.UnixNano()

}

func (pq *priorityqueue) Swap(i, j int) {
	pq.streams[i], pq.streams[j] = pq.streams[j], pq.streams[i]
}

func (pq *priorityqueue) Push(x interface{}) {
	stream := x.(*logproto.Stream)
	pq.streams = append(pq.streams, stream)
}

// Pop implements heap.Interface and removes the exhausted stream at the end of
// the queue. Callers draining entries in order should use popEntry instead.
func (pq *priorityqueue) Pop() interface{} {
	n := pq.Len()
	stream := pq.streams[n-1]
	pq.streams[n-1] = nil // avoid memory leak
	pq.streams = pq.streams[:n-1]
	return stream
}

// popEntry returns the next entry in direction order along with the labels of
// the stream it came from. It must not be called on an empty queue.
//
// The head stream is advanced in place and the heap repaired, so draining the
// queue does not allocate. Re-pushing a copy of the head instead would cost one
// logproto.Stream per entry returned, i.e. one short-lived allocation for every
// log line in the response.
func (pq *priorityqueue) popEntry() (string, logproto.Entry) {
	head := pq.streams[0]
	labels, entry := head.Labels, head.Entries[0]

	if len(head.Entries) > 1 {
		head.Entries = head.Entries[1:]
		heap.Fix(pq, 0)
	} else {
		heap.Pop(pq)
	}

	return labels, entry
}
