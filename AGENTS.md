# System_Engine Contributor Instructions

Before changing this repository, read
`System_Engine/DesignDoc/Engineering_Conventions.md` in full and follow it.

Key gates:

- User and higher-priority instructions take precedence over repository conventions.
- Default to a feature branch. Commit or push to `main` only when the repository owner
  explicitly requests it.
- A request to change or build does not itself authorize a commit, push, or pull request.
- Before an authorized code delivery, run `make check` and report its exact result.
- Preserve unrelated working-tree changes; never overwrite another contributor's work.
