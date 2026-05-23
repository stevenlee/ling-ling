---
title: Reading Notes — The Pragmatic Programmer
tags: [reading, books, programming]
---

# Reading Notes — The Pragmatic Programmer

These are notes from re-reading Hunt and Thomas's _The Pragmatic
Programmer_ (20th anniversary edition). The book has aged better than
most programming books of its era.

> [!quote] The DRY Principle
> "Every piece of knowledge must have a single, unambiguous, authoritative
> representation within a system."
>
> Hunt and Thomas, _The Pragmatic Programmer_, Chapter 8.

The DRY framing is the book's most-cited contribution. It's also widely
misapplied. The book is careful to scope DRY to **knowledge**, not code.
Two functions with the same shape that encode different concepts are not
DRY violations — they just look similar today.

## The Broken Windows Theory of Software

The authors borrow the criminological theory of broken windows and apply
it to codebases:

> [!warning] Don't Live with Broken Windows
> Fix bad designs, wrong decisions, and poor code when you see them.
> Don't leave broken windows (bad designs, wrong decisions, or poor code)
> unrepaired. Fix each one as soon as it is discovered.

This advice ages well. The cost of "I'll clean it up later" is almost
always higher than the cost of cleaning up now, because the broken window
signals to other contributors that mess is tolerated.

> [!info] Operational vs Theoretical
> The broken-windows theory in criminology turned out to be empirically
> shakier than once believed, but the software metaphor stands on its own
> regardless of whether the original sociology holds up.

## Tracer Bullets vs Prototypes

The book draws a careful distinction:

> [!note] Tracer Bullets
> Tracer bullets are end-to-end production code: thin, but real, slices
> through the architecture. They evolve into the final system.

> [!note] Prototypes
> Prototypes are throwaway probes that answer a single question and then
> get deleted. Their code is not meant to survive.

Confusing the two is how "prototypes" end up in production. A prototype
that looks like it works is the most expensive thing a team can produce.

## The Reading Hierarchy

> [!example] Reading the Book Yourself
> The 20th anniversary edition is the one to read. The original 1999
> edition has some dated examples (Java applet GUIs, CVS commands) that
> the new edition replaces with current ones.

The new edition's "Concurrency" chapter is significantly stronger than
the original. The original predates the multi-core era.

> [!tip] Companion Reading
> Pair this book with:
> - _Code Complete_ (Steve McConnell) for finer-grained craft advice
> - _A Philosophy of Software Design_ (John Ousterhout) for module-level thinking
> - _Designing Data-Intensive Applications_ (Martin Kleppmann) for distributed concerns

## Lasting Heuristics

> [!success] Heuristics worth memorising
> - **Good-Enough Software**: software has stakeholders; they get to set the bar, not you.
> - **Knowledge Portfolio**: invest regularly, diversify, manage risk like a financial portfolio.
> - **Estimate to Avoid Surprises**: not to be precise, but to surface assumptions.
> - **Tracer Bullets**: build the skeleton end-to-end before fleshing it out.
