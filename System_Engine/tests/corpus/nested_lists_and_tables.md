---
title: Software Architecture Decision Record Template
tags: [architecture, adr, process]
---

# Software Architecture Decision Record Template

An Architecture Decision Record (ADR) captures one significant design
choice and the reasoning behind it. The format below has stayed roughly
stable across teams for a decade.

## Required Sections

Every ADR should contain these sections in this order:

1. **Context** — the situation that forces a decision
   - Business drivers
   - Technical constraints
   - Existing system shape
2. **Decision** — the choice being made
   - Stated as an imperative ("We will adopt X")
   - One option, not a list
3. **Consequences** — what changes as a result
   - Positive effects we expect
   - Negative effects we accept
   - Follow-up work this creates

## Optional Sections

These appear only when needed:

- **Alternatives Considered** — usually 2-3, with one-paragraph rationale for rejection each
- **References** — links to prior art, RFCs, or external systems
- **Author / Reviewers** — who decided, who endorsed

## Status Lifecycle

| Status | Meaning | Editable? |
|--------|---------|-----------|
| Draft | Author is still writing | Yes |
| Proposed | Open for review | Yes, by author |
| Accepted | Decision is in force | No (immutable) |
| Superseded | A later ADR overrides this | No |
| Deprecated | Decision is being phased out | No |

The "immutable once Accepted" rule is the critical one. ADRs are
history. If a decision changes, write a new ADR that supersedes the old.
Editing an Accepted ADR destroys the audit trail.

## Numbering Convention

ADRs are numbered sequentially with a monotonic ID:

| Format | Example | Notes |
|--------|---------|-------|
| `ADR-NNN` | `ADR-042` | Four digit zero-pad once past 99 |
| Title slug | `adr-042-event-sourcing` | Filename, lowercase, hyphens |
| Title in body | `# 042: Adopt Event Sourcing for Audit Log` | First line of file |

## Common Anti-Patterns

Writers fall into these traps:

- **Listing pros and cons without a decision**: the decision IS the point. A pros/cons list with no commitment is just a comparison table.
- **Conflating Context with Decision**: stating the problem as if it were the answer.
- **Omitting Negative Consequences**: every choice has costs. ADRs that list only upsides are sales pitches, not records.
- **Editing Accepted ADRs in place**: as above, this is irreversible damage to the audit trail.

## Tools

ADRs are plain markdown. Tooling exists but is optional:

- `adr-tools` (CLI) — adds boilerplate, manages numbering
- `log4brains` — generates a static-site browser
- Most teams just use a `docs/adrs/` folder with no tooling at all

The format is the value, not the tooling.
