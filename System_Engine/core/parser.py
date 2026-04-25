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
