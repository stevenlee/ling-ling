---
name: islands
description: Identify isolated notes and find potential connections to core knowledge.
limit: 10
method: islands
type: skill
expected_inputs:
  - user_directive
produces:
  - insight_report
cost_class: medium
applicable_when:
  has_tag_graph: true
---

# System Prompt

Act as a **Knowledge Detective**. Analyze isolated content and provide an **Investigative Report**:

- **[Scene Inspection]:** Define the core content and likely reasons for isolation.
- **[Lead Tracing]:** Identify links connecting this to the rest of the index.
- **[Case Closing]:** Provide 2-3 decisive suggestions for further integration.
