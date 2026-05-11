import re
import yaml
import logging

def parse_markdown_metadata(content: str) -> dict:
    """
    Extracts YAML frontmatter and body hashtags from markdown content.
    Returns a dictionary with 'tags' (list) and any other frontmatter fields.
    """
    metadata = {
        "tags": set()
    }
    
    # 1. Parse YAML Frontmatter
    frontmatter_match = re.search(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    remaining_content = content
    
    if frontmatter_match:
        try:
            yaml_data = yaml.safe_load(frontmatter_match.group(1))
            if yaml_data and isinstance(yaml_data, dict):
                # Merge all YAML data into metadata
                for key, value in yaml_data.items():
                    if key == 'tags':
                        if isinstance(value, list):
                            for t in value: metadata["tags"].add(str(t).strip())
                        elif isinstance(value, str):
                            for t in value.split(','): metadata["tags"].add(t.strip())
                    else:
                        metadata[key] = value
            remaining_content = content[frontmatter_match.end():]
        except Exception as e:
            logging.error(f"Parser: Failed to parse YAML frontmatter: {e}")

    # 2. Parse Body Hashtags
    # Regex: Look for # followed by word characters, ensuring it's not part of a header or URL
    # We use remaining_content to avoid parsing tags that might be inside YAML strings (though unlikely)
    hashtags = re.findall(r'(?:^|\s)#([\w\u4e00-\u9fff]+)', remaining_content)
    for tag in hashtags:
        metadata["tags"].add(tag.strip())
        
    # Convert tags set to sorted list and normalize
    from core.tag_manager import TagManager
    from core.config import TAG_MAP_FILE
    tm = TagManager(TAG_MAP_FILE)
    
    metadata["tags"] = sorted(list(set([tm.normalize(t) for t in metadata["tags"] if t])))
    
    return metadata

def dump_markdown_with_metadata(metadata: dict, content: str) -> str:
    """
    Combines metadata (as YAML frontmatter) and content into a single markdown string.
    """
    # Clean up metadata: convert sets to lists for YAML serialization
    clean_meta = {}
    for k, v in metadata.items():
        if isinstance(v, (set, list)):
            # Special handling for tags to ensure they are unique and sorted
            if k == "tags":
                clean_meta[k] = sorted(list(set(v)))
            else:
                clean_meta[k] = list(v)
        else:
            clean_meta[k] = v
            
    frontmatter = yaml.safe_dump(clean_meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n{content}"

MERMAID_START_RE = re.compile(
    r'^\s*(graph\s+(?:TD|TB|BT|RL|LR)|flowchart\s+(?:TD|TB|BT|RL|LR)|'
    r'sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline)\b',
    re.IGNORECASE
)

MARKDOWN_BOUNDARY_RE = re.compile(r'^\s*(#{1,6}\s+|---\s*$|\*\*\*\s*$|___\s*$)')
MERMAID_CONTINUATION_RE = re.compile(
    r'^\s*('
    r'graph\b|flowchart\b|subgraph\b|end\b|style\b|classDef\b|class\b|'
    r'click\b|linkStyle\b|direction\b|sequenceDiagram\b|participant\b|'
    r'note\b|activate\b|deactivate\b|alt\b|else\b|opt\b|loop\b|par\b|'
    r'stateDiagram\b|stateDiagram-v2\b|erDiagram\b|journey\b|gantt\b|'
    r'pie\b|mindmap\b|timeline\b|section\b|title\b|%%|'
    r'[\w".()[\]{}:/ -]+\s*(?:-->|---|-.->|==>|--|:|\|)'
    r')',
    re.IGNORECASE
)
MERMAID_NODE_LABEL_RE = re.compile(
    r'(?P<node>\b[A-Za-z][\w-]*)'
    r'(?P<open>[\[{])'
    r'(?P<label>[^\]\}\n]+)'
    r'(?P<close>[\]}])'
)
LATEX_CR_COMMAND_RE = re.compile(r'\r(ightarrow|ight|angle|brace|ceil|floor|vert|Vert)\b')

def repair_latex_carriage_returns(text: str) -> tuple[str, list[str]]:
    """
    Repairs Python/LLM text that contains an actual carriage return where a
    LaTeX command should have had a literal backslash-r, e.g. $\rightarrow$.
    """
    if not text:
        return "", []

    repaired = LATEX_CR_COMMAND_RE.sub(r'\\r\1', text)
    if repaired != text:
        return repaired, ["repaired_latex_carriage_return"]
    return text, []

def repair_mermaid_label_quotes(text: str) -> tuple[str, list[str]]:
    """
    Quotes Mermaid node labels in [] and {} shapes.
    This is intentionally limited to fenced Mermaid blocks and explicit
    NodeId[label] / NodeId{label} patterns.
    """
    if not text:
        return "", []

    lines = text.splitlines()
    output = []
    fixes = []
    in_mermaid = False

    def quote_label(match: re.Match) -> str:
        label = match.group("label").strip()
        if label.startswith(('"', "'")):
            return match.group(0)

        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        fixes.append("quoted_mermaid_labels")
        return f'{match.group("node")}{match.group("open")}"{escaped}"{match.group("close")}'

    for line in lines:
        stripped = line.strip().lower()
        if stripped == "```mermaid":
            in_mermaid = True
            output.append(line)
            continue
        if in_mermaid and stripped == "```":
            in_mermaid = False
            output.append(line)
            continue

        output.append(MERMAID_NODE_LABEL_RE.sub(quote_label, line) if in_mermaid else line)

    return "\n".join(output), sorted(set(fixes))

def repair_mermaid_fences(text: str) -> tuple[str, list[str]]:
    """
    Repairs common Mermaid formatting failures in LLM output.
    Returns the repaired text and a list of applied fix labels.
    """
    if not text:
        return "", []

    lines = text.splitlines()
    output = []
    fixes = []
    in_fence = False
    fence_lang = ""
    i = 0

    def next_nonempty_line(start: int) -> str:
        for j in range(start, len(lines)):
            if lines[j].strip():
                return lines[j]
        return ""

    def is_mermaid_continuation(line: str) -> bool:
        stripped_line = line.strip()
        return not stripped_line or bool(MERMAID_CONTINUATION_RE.match(stripped_line))

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fence_match = re.match(r'^```(\w*)\s*$', stripped)

        if fence_match:
            in_fence = not in_fence
            fence_lang = fence_match.group(1).lower() if in_fence else ""
            output.append(line)
            i += 1
            continue

        if not in_fence and stripped.lower() == "mermaid" and MERMAID_START_RE.match(next_nonempty_line(i + 1)):
            fixes.append("wrapped_bare_mermaid")
            output.append("```mermaid")
            i += 1

            while i < len(lines):
                current = lines[i]
                current_stripped = current.strip()

                if current_stripped == "```":
                    i += 1
                    break

                if current_stripped and MARKDOWN_BOUNDARY_RE.match(current) and output[-1].strip():
                    break

                following = next_nonempty_line(i + 1)
                if not current_stripped and following and (
                    MARKDOWN_BOUNDARY_RE.match(following) or not is_mermaid_continuation(following)
                ):
                    i += 1
                    break

                if current_stripped and not is_mermaid_continuation(current):
                    break

                output.append(current)
                i += 1

            while output and not output[-1].strip():
                output.pop()
            output.append("```")
            continue

        output.append(line)
        i += 1

    if in_fence and fence_lang == "mermaid":
        fixes.append("closed_unterminated_mermaid")
        output.append("```")

    return "\n".join(output), fixes

def strip_body_frontmatter(text: str) -> tuple[str, list[str]]:
    """
    Removes accidental YAML frontmatter from an LLM body.
    The real file-level frontmatter is added separately by dump_markdown_with_metadata.
    """
    if not text:
        return "", []

    cleaned = re.sub(r'^\s*---\s*\n.*?\n---\s*\n?', '', text.strip(), count=1, flags=re.DOTALL)
    if cleaned != text.strip():
        return cleaned.strip(), ["removed_body_frontmatter"]
    return text, []

def run_markdown_quality_checks(text: str, strip_frontmatter: bool = False) -> tuple[str, list[str]]:
    """
    Applies deterministic, low-risk cleanup to generated Markdown.
    This intentionally avoids semantic rewrites; LLM retry can be layered later.
    """
    fixes = []
    cleaned = text or ""

    if strip_frontmatter:
        cleaned, applied = strip_body_frontmatter(cleaned)
        fixes.extend(applied)

    cleaned, applied = repair_latex_carriage_returns(cleaned)
    fixes.extend(applied)

    cleaned, applied = repair_mermaid_fences(cleaned)
    fixes.extend(applied)

    cleaned, applied = repair_mermaid_label_quotes(cleaned)
    fixes.extend(applied)

    return cleaned.strip(), fixes

def clean_llm_response(text: str) -> str:
    """
    Safely unwraps the LLM response if it's wrapped in a response container (like ```markdown).
    Preserves functional code blocks (like ```mermaid, ```python).
    """
    if not text:
        return ""
    
    text = text.strip()
    
    # Match an outer code block
    pattern = r'^```(\w*)\n(.*?)\n```$'
    match = re.match(pattern, text, re.DOTALL | re.IGNORECASE)
    
    if match:
        lang = match.group(1).lower()
        content = match.group(2).strip()
        
        # Whitelist of languages that are likely used as "response containers"
        # If the block uses one of these (or no language), we unwrap it.
        # If it's anything else (mermaid, python, etc.), it's likely intended functional code.
        container_langs = ['', 'markdown', 'md', 'txt', 'text', 'markdown-math']
        
        if lang in container_langs:
            return content
        else:
            # It's a functional block (e.g. ```mermaid), keep the wrapper
            return text
            
    return text
