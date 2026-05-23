---
title: Notes on Cache Coherence Protocols
tags: [systems, caching, mesi]
---

# Notes on Cache Coherence Protocols

Modern multi-core CPUs do not have a single shared memory in the simple
sense undergraduates first learn. Each core has its own L1 and L2 caches,
and the cores communicate through cache coherence protocols whose details
have outsized effects on real-world performance.

## MESI: The Baseline

The MESI protocol assigns each cache line one of four states: Modified,
Exclusive, Shared, or Invalid. The protocol's correctness rule is simple:
a line is in Modified state in **at most one** cache. When another core
wants to read that line, the protocol must transition the writer to either
Shared or Invalid before the reader can proceed.

This sounds simple but the bandwidth implications are dramatic. A
cache-line ping-pong between two cores can saturate the interconnect with
coherence traffic while no useful work happens. Profilers usually surface
this as "high LLC misses" or "elevated coherence stalls" depending on
which counters they expose.

### A Worked Example

Imagine a global counter incremented by every thread:

```c
// Don't do this in real code.
volatile int counter = 0;

void worker(void) {
    for (int i = 0; i < 1000000; i++) {
        __sync_fetch_and_add(&counter, 1);
    }
}
```

Each `__sync_fetch_and_add` triggers a coherence transaction. The cache
line holding `counter` ping-pongs between every core that runs `worker`.
The throughput drops by orders of magnitude as you add cores — not because
of contention on the counter value itself, but because of coherence
traffic.

The fix is to give each thread its own counter and sum them at the end.
A reduce-pattern.

## MOESI: Adding the Owner State

MOESI adds an Owned state, which lets a line be Modified-but-shared.
Instead of writing dirty lines back to memory when another reader appears,
the owner forwards them directly. This saves a memory round trip in the
common reader-writer case.

```rust
// pseudocode for an owner forwarding read
fn handle_read(line: CacheLine, requester: CoreId) {
    if line.state == State::Owned {
        // forward directly without memory write-back
        send_to(requester, line.data);
        line.state = State::Owned;  // stays owned
    } else if line.state == State::Modified {
        // write back to memory, demote to Shared
        write_back(line);
        line.state = State::Shared;
        send_to(requester, line.data);
    }
}
```

AMD's processors have used MOESI variants for years. Intel's protocols
have a similar effect through different transition rules.

## MESIF: Adding the Forward State

Intel's MESIF assigns one cache the Forward state — the designated
forwarder when multiple caches hold a Shared line. This avoids the
"multiple-cache response storm" where every cache holding a Shared copy
tries to respond simultaneously.

The Forward state is a hint to the protocol, not a correctness requirement.
The protocol still works if all caches respond, but performance is worse.

## Practical Implications

If you are writing performance-sensitive shared-memory code:

1. **Avoid false sharing**: pad your data so unrelated fields don't share
   a cache line.
2. **Use thread-local accumulators**: reduce at the end, not on every
   update.
3. **Profile coherence counters**, not just instruction counts.
4. **Beware atomics in hot loops**: even contended atomics with low
   apparent contention can saturate interconnect bandwidth.

## Closing Thought

Cache coherence is one of those areas where the abstraction (shared
memory) lies about the performance model. You can write correct code that
performs terribly, and the cause won't show up in any single-threaded
profile. Understanding MESI/MOESI/MESIF state machines is essentially
mandatory for systems work at scale.
