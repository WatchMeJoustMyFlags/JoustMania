package actions

import (
	"math/rand/v2"
	"strconv"
	"sync/atomic"
)

// nonceGen produces a fresh, unique edge-trigger nonce per dispatch. The game
// coordinator applies an edge-triggered intervention on nonce CHANGE, so two
// consecutive dispatches of the same flag MUST carry different nonces. We use a
// monotonic counter (guarantees strict change even within the same millisecond)
// plus a random suffix (collision-resistant across agent restarts, where the
// counter resets to 0). The format is "<counter>-<random>"; it is opaque to the
// reader, which only compares it for inequality.
type nonceGen struct {
	counter atomic.Uint64
	rng     func() uint64
}

func newNonceGen() *nonceGen {
	return &nonceGen{rng: rand.Uint64}
}

// next returns the next unique nonce. Safe for concurrent use.
func (n *nonceGen) next() string {
	c := n.counter.Add(1)
	return strconv.FormatUint(c, 36) + "-" + strconv.FormatUint(n.rng()&0xffffffffff, 36)
}
