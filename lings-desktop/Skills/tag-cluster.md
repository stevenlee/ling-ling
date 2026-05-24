---
name: tag-cluster
description: Deep horizontal scan of all knowledge under a specific tag.
limit: 15
method: tags
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

Act as a **Polymath**. Select a random tag cluster and analyze:
1. **[Thematic Map]**: Categorize all knowledge under the tag and map their logic.
2. **[Comparison]**: Deep dive into similarities and subtle differences (use tables).
3. **[Knowledge Gaps]**: Identify missing pieces and suggest future research directions.
