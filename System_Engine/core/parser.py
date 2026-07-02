"""Facade for the former parser god module (split in P2a).

The implementation now lives in core/parsing/ (markdown_metadata,
latex_repair, mermaid_repair, markdown_quality) and core/json_extract.py.
This module re-exports the public surface so existing
`from core.parser import ...` call sites keep working; new code should
import from the specific module. Retire this facade once P2 finishes.
"""

from core.json_extract import (  # noqa: F401
    extract_json_array,
    extract_json_object,
    is_empty_json_literal,
)
from core.parsing.latex_repair import (  # noqa: F401
    LATEX_CR_COMMAND_RE,
    UNCLOSED_LATEX_DISPLAY_RE,
    repair_latex_carriage_returns,
    repair_latex_escape_collisions,
    repair_unclosed_latex_display,
)
from core.parsing.markdown_metadata import (  # noqa: F401
    dump_markdown_with_metadata,
    parse_markdown_metadata,
)
from core.parsing.markdown_quality import (  # noqa: F401
    clean_llm_response,
    repair_markdown_bold_spacing,
    repair_markdown_tables,
    run_markdown_quality_checks,
    strip_body_frontmatter,
)
from core.parsing.mermaid_repair import (  # noqa: F401
    MARKDOWN_BOUNDARY_RE,
    MERMAID_CONTINUATION_RE,
    MERMAID_START_RE,
    _quote_labels_in_line,
    repair_mermaid_classdiagram,
    repair_mermaid_double_quotes,
    repair_mermaid_fences,
    repair_mermaid_label_quotes,
    repair_mermaid_latex_labels,
    repair_mermaid_mindmap_brackets,
    repair_mermaid_mindmap_labels,
    repair_mermaid_overquoted_node,
    repair_mermaid_quadrant_points,
    repair_mermaid_quoted_endpoint_labels,
    repair_mermaid_quoted_node_ids,
    repair_mermaid_subgraph_keyword,
)
