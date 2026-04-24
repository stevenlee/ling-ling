# Release Assistant Tool Implementation Plan

This plan outlines the creation of a `ReleaseAssistant` tool within the `System_Engine/maintenance` directory. This tool will help the user prepare for a project release by auditing code functions and generating comprehensive release notes.

## User Review Required

> [!IMPORTANT]
> The tool will scan your Python source code. While it only reads files, it will send function signatures and docstrings to the configured LLM for analysis. Please ensure your LLM provider is configured and you are comfortable with this.

## Proposed Changes

### System Engine Maintenance

#### [NEW] [release_helper.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/maintenance/release_helper.py)
A new utility script that:
1.  Scans `System_Engine/` for all `.py` files.
2.  Uses the `ast` module to extract function names, arguments, and docstrings.
3.  Performs basic linting (e.g., identifies functions missing docstrings).
4.  Interfaces with `LLMClient` to generate a high-level summary of the project's capabilities.
5.  Outputs a formatted `RELEASE_NOTE.md` to the project root.

### Integration

#### [NEW] [@ling-release.md](file:///Users/stevenlee/projects/ling-ling/lings-desktop/toLingLing/@ling-release.md)
A command trigger file that allows the user to invoke the release helper through the existing agentic workflow.

## Verification Plan

### Automated Tests
-   Run `python3 System_Engine/maintenance/release_helper.py` directly and verify it generates `RELEASE_NOTE.md`.
-   Check the content of `RELEASE_NOTE.md` for accuracy and aesthetic quality.

### Manual Verification
-   Trigger the release check via the `@ling-release.md` command file and observe the `PromptWatcher` processing it.
