import json
import logging
import base64
import mimetypes
import re
import yaml
from pathlib import Path
from datetime import datetime
from core.config import LLM_PROVIDER, PERSONAS_DIR, TEMPLATES_DIR, GUIDELINES_DIR, PROJECT_ROOT, settings
from core.parser import strip_body_frontmatter, extract_json_object
from core.utils import digest_value_to_text

class LLMClient:
    def __init__(self):
        self.provider = LLM_PROVIDER
        if self.provider == "vllm":
            try: from openai import OpenAI
            except ImportError: raise ImportError("pip install openai")
            import os
            self.client = OpenAI(
                base_url=os.getenv("VLLM_API_BASE", "http://192.168.1.103:9000/v1"),
                api_key=os.getenv("VLLM_API_KEY", "dummy-token"),
                timeout=300.0
            )
            self.model = os.getenv("VLLM_MODEL", "gpt-oss-20b")
        elif self.provider == "gemini":
            try: from google import genai
            except ImportError: raise ImportError("pip install google-genai")
            import os
            self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            self.model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        elif self.provider == "ollama":
            try: from openai import OpenAI
            except ImportError: raise ImportError("pip install openai")
            import os
            self.client = OpenAI(
                base_url=os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1"),
                api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
                timeout=300.0
            )
            self.model = os.getenv("OLLAMA_MODEL", "gemma2:27b")
            
    def _get_lang_hint(self) -> str:
        lang = settings.OUTPUT_LANGUAGE.lower()
        if "chinese" in lang or "中文" in lang: return "Traditional Chinese (繁體中文)"
        if "japanese" in lang or "日本語" in lang: return "Japanese (日本語)"
        return settings.OUTPUT_LANGUAGE

    def _load_localized_content(self, file_path: Path) -> str:
        lang = settings.OUTPUT_LANGUAGE.lower()
        suffix = ".zh" if ("chinese" in lang or "中文" in lang) else ".ja" if ("japanese" in lang or "日本語" in lang) else ""
        if suffix:
            localized_path = file_path.parent / f"{file_path.stem}{suffix}{file_path.suffix}"
            if localized_path.exists(): return localized_path.read_text('utf-8')
        return file_path.read_text('utf-8') if file_path.exists() else ""

    def _build_system_prompt(self, instruction_type: str, forced_template: str = None, default_template: str = None, require_yaml_header: bool = True) -> str:
        role_instructions = self._load_localized_content(PERSONAS_DIR / f"{settings.AGENT_ROLE}.md")
        
        if forced_template == "none":
            template_instructions = ""
        else:
            template_name = (forced_template if forced_template else default_template) or "wiki-note"
            if not template_name.endswith('.md'): template_name += '.md'
            template_instructions = self._load_localized_content(TEMPLATES_DIR / template_name)
        viz_instructions = self._load_localized_content(GUIDELINES_DIR / "Visualization.md")
        
        lang_hint = self._get_lang_hint()
        strict_hint = "\n## STRICT ADHERENCE REQUIRED\nYou MUST follow the provided Markdown template exactly. Do NOT add conversational fillers, greetings, or meta-comments. Focus exclusively on structured content." if settings.STRICT_MODE else ""
        yaml_rule = "Use the standard YAML header (--- title: ... ---) at the beginning of your response." if require_yaml_header else "Do not include YAML frontmatter unless the user explicitly asks for it."
        common_rules = f"\n## Output Language\nPlease output everything in {lang_hint}.{strict_hint}\n\n## Task\n{instruction_type}\n\n{viz_instructions}\n\n{yaml_rule}"
        return f"{role_instructions}\n\n{template_instructions}\n\n{common_rules}"

    def _complete_text(self, system_prompt: str, user_msg: str, temperature: float = None, max_tokens: int = None) -> str:
        temperature = settings.CREATIVITY if temperature is None else temperature
        max_tokens = settings.MAX_OUTPUT if max_tokens is None else max_tokens

        if self.provider == "gemini":
            from google import genai
            response = self.client.models.generate_content(
                model=self.model, contents=[str(user_msg)],
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                    max_output_tokens=max_tokens
                )
            )
            return response.text or ""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"num_ctx": settings.MEMORY_LIMIT} if self.provider == "ollama" else {}
        )
        return response.choices[0].message.content or ""


    def _strip_accidental_frontmatter(self, text: str) -> str:
        if not text:
            return ""

        text = text.strip()
        text = re.sub(r'^```(?:markdown|md)?\s*\n(.*?)\n```$', r'\1', text, flags=re.DOTALL | re.IGNORECASE).strip()
        text, _ = strip_body_frontmatter(text)
        return text.strip()

    def _load_project_identity(self) -> str:
        readme_path = PROJECT_ROOT / "README.md"
        schema_path = PROJECT_ROOT / "SCHEMA.md"
        parts = []
        for path in [readme_path, schema_path]:
            try:
                if path.exists():
                    parts.append(path.read_text(encoding='utf-8')[:4000])
            except Exception:
                continue
        return "\n\n---\n\n".join(parts)

    def generate_entity_page(self, markdown_content: str = None, filename: str = None, index_content: str = "", image_path: Path = None, context_hint: str = None) -> dict:
        instruction_type = "Convert this material into a structured Wiki entity page."
        system_prompt = self._build_system_prompt(instruction_type)

        lang_hint = self._get_lang_hint()
        labels = {
            "Traditional Chinese (繁體中文)": {"file": "檔案名稱", "content": "素材內容"},
            "Japanese (日本語)": {"file": "ファイル名", "content": "素材内容"}
        }.get(lang_hint, {"file": "Filename", "content": "Content"})

        if image_path:
            mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            with open(image_path, "rb") as f:
                raw_bytes = f.read()
                
            if self.provider == "gemini":
                from google import genai
                user_msg = [
                    f"{labels['file']}: {filename}",
                    genai.types.Part.from_bytes(data=raw_bytes, mime_type=mime_type)
                ]
            else:
                encoded_image = base64.b64encode(raw_bytes).decode('utf-8')
                user_msg = [
                    {"type": "text", "text": f"{labels['file']}: {filename}"}, 
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"}}
                ]
        else:
            user_msg = f"{labels['file']}: {filename}\n\n"
            if context_hint:
                user_msg += f"[Context]: {context_hint}\n\n"
            user_msg += f"{labels['content']}:\n{markdown_content}"

        try:
            if image_path:
                # Multimodal path — requires provider-specific payload formatting
                if self.provider == "gemini":
                    from google import genai
                    contents_payload = user_msg if isinstance(user_msg, list) else [str(user_msg)]
                    response = self.client.models.generate_content(
                        model=self.model, contents=contents_payload,
                        config=genai.types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=settings.CREATIVITY,
                            max_output_tokens=settings.MAX_OUTPUT
                        )
                    )
                    return self._hybrid_parse(response.text)
                else:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
                        temperature=settings.CREATIVITY,
                        max_tokens=settings.MAX_OUTPUT,
                        extra_body={"num_ctx": settings.MEMORY_LIMIT} if self.provider == "ollama" else {}
                    )
                    return self._hybrid_parse(response.choices[0].message.content)
            else:
                # Text-only path — delegate to unified _complete_text
                return self._hybrid_parse(self._complete_text(system_prompt, user_msg))
        except Exception as e:
            logging.error(f"LLM Error: {e}")
            return None

    def _hybrid_parse(self, text: str) -> dict:
        """A robust parser that finds the Wiki Note within the model response."""
        result = {"title": "Untitled", "tags": [], "type": "entity", "content": text.strip() if text else ""}
        
        if not text: return result
        
        # 1. Find the first YAML block (support both --- and ```yaml)
        # Using (?:^|\n) to ensure it starts on a new line
        yaml_match = re.search(r'(?:^|\n)(?:---|```yaml)\s*\n(.*?)\n(?:---|```)\s*(?:\n|$)', text, re.DOTALL)
        if yaml_match:
            yaml_str = yaml_match.group(1).strip()
            
            try:
                # Aggressive cleanup: handle markdown symbols at start of line, after brackets, commas, or colons
                clean_yaml_str = re.sub(r'(^|[:\[,\s])[\*\_]{1,2}(.*?)[\*\_]{1,2}(?=[\]\s,:]|$)', r'\1"\2"', yaml_str, flags=re.MULTILINE)
                metadata = yaml.safe_load(clean_yaml_str)
                if isinstance(metadata, dict):
                    if "title" in metadata: result["title"] = str(metadata["title"])
                    if "tags" in metadata: result["tags"] = metadata["tags"]
                    if "type" in metadata: result["type"] = metadata["type"]
                    if "pending_concepts" in metadata: result["pending_concepts"] = metadata["pending_concepts"]
                    
                    # Success! Truncate the content to remove the parsed frontmatter.
                    result["content"] = text[yaml_match.end():].strip()
                    return result
            except Exception as e:
                # If it explicitly started with ```yaml, it was meant to be YAML but had a syntax error.
                if "```yaml" in yaml_match.group(0):
                    logging.warning(f"YAML parse failed: {e}\nOffending string:\n{yaml_str}")
                # Otherwise, it was likely just a markdown horizontal rule (---). We safely ignore it.
                pass
                
        # 2. Fallback: If no YAML (or it failed), try to find the first H1 title
        title_match = re.search(r'^#\s+(.*)', text, re.MULTILINE)
        if title_match:
            result["title"] = title_match.group(1).strip()
            # We don't truncate the H1 title from the content here, as it's useful to keep the header.
        
        return result

    def answer_query(self, query_content: str, wiki_context: str, custom_instruction: str = None, temperature: float = None) -> str:
        if custom_instruction:
            task = custom_instruction
            system_prompt = self._build_system_prompt(task, forced_template="none", require_yaml_header=False)
            user_msg = query_content
        else:
            lang_hint = self._get_lang_hint()
            system_prompt = f"""You are Ling-Ling's question-answering interface.

Answer the user's question directly in {lang_hint}.
Use the provided knowledge context only as reference material.
Do not rewrite, summarize, or continue the context unless the user explicitly asks for that.
If the context is irrelevant or insufficient, say so briefly and answer from the project identity information.
When the user asks what Ling-Ling is, describe Ling-Ling as an Obsidian-vault-based agentic RAG knowledge system driven by Scripture, Skills, and Templates.
Do not include YAML frontmatter.
"""
            user_msg = f"""## User Question
{query_content}

## Project Identity
{self._load_project_identity() or "(No project identity available.)"}

## Retrieved Knowledge Context
{wiki_context if wiki_context.strip() else "(No relevant context retrieved.)"}
"""
        try:
            return self._complete_text(system_prompt, user_msg, temperature=temperature)
        except Exception as e:
            return f"Error: {e}"

    def translate_tags(self, tags: list[str]) -> dict:
        system_prompt = "Return a JSON mapping of {original_tag: english_equivalent} for these tags."
        try:
            if self.provider == "gemini":
                from google import genai
                response = self.client.models.generate_content(
                    model=self.model, contents=[f"Tags: {tags}"],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_prompt, 
                        temperature=0.1,
                        response_mime_type="application/json"
                    )
                )
                return json.loads(response.text)
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Tags: {tags}"}],
                    response_format={"type": "json_object"},
                    temperature=0.1
                )
                return json.loads(response.choices[0].message.content)
        except Exception as e:
            logging.warning(f"Tag translation failed: {e}")
            return {}

    def generate_part_digest(self, title: str, part_number: int, total_parts: int, raw_chunk: str, part_note: str, pending_concepts: str = "") -> dict:
        """Creates a structured map digest for one part of a long document."""
        lang_hint = self._get_lang_hint()
        system_prompt = f"""You create compact, evidence-aware map digests for a later synthesis pass.
Output language: {lang_hint}.
Return JSON only. No Markdown, no YAML, no commentary.
"""
        prompt = f"""Document title: {title}
Part: {part_number}/{total_parts}

Prior unresolved concepts:
{pending_concepts or "(none)"}

Raw source chunk:
{raw_chunk}

Generated part note:
{part_note}

Return one JSON object with this schema:
{{
  "part": {part_number},
  "title": "short part title",
  "thesis": "the central claim or function of this part",
  "key_points": ["3-6 concrete points, preserving names, mechanisms, and distinctions"],
  "evidence": ["2-5 source-grounded details, examples, quotes, terms, or data points"],
  "terms": ["important proper nouns or technical terms"],
  "open_questions": ["ambiguities, missing context, contradictions, or follow-up questions"],
  "handoff": "what the next or final synthesis must remember"
}}

Rules:
- Prefer specific details over generic summary language.
- Do not invent facts that are not supported by the source chunk or generated note.
- Keep each list item concise but information-rich.
"""

        try:
            parsed = extract_json_object(self._complete_text(system_prompt, prompt, temperature=0.2, max_tokens=1800))
            if parsed:
                parsed.setdefault("part", part_number)
                parsed.setdefault("title", f"Part {part_number}")
                parsed.setdefault("thesis", "")
                parsed.setdefault("key_points", [])
                parsed.setdefault("evidence", [])
                parsed.setdefault("terms", [])
                parsed.setdefault("open_questions", [])
                parsed.setdefault("handoff", "")
                return parsed
        except Exception as e:
            logging.error(f"Part digest generation failed for {title} part {part_number}: {e}")

        fallback = self._strip_accidental_frontmatter(part_note).strip().splitlines()
        fallback_lines = [line.strip("#- * \t") for line in fallback if line.strip()][:6]
        return {
            "part": part_number,
            "title": f"Part {part_number}",
            "thesis": fallback_lines[0] if fallback_lines else f"{title} part {part_number}",
            "key_points": fallback_lines[1:5],
            "evidence": [],
            "terms": [],
            "open_questions": [],
            "handoff": pending_concepts or ""
        }

    def _format_part_digest_for_prompt(self, digest) -> str:
        if isinstance(digest, str):
            return digest
        if not isinstance(digest, dict):
            return str(digest or "(empty digest)")

        def as_text(value) -> str:
            return digest_value_to_text(value)

        def bullets(values):
            if not values:
                return "- (none)"
            if isinstance(values, str):
                values = [values]
            return "\n".join(f"- {as_text(value)}" for value in values if as_text(value)) or "- (none)"

        part = digest.get("part", "?")
        title = digest.get("title", f"Part {part}")
        return f"""### Part {part}: {title}
Thesis: {as_text(digest.get('thesis', ''))}

Key points:
{bullets(digest.get('key_points', []))}

Evidence and source-grounded details:
{bullets(digest.get('evidence', []))}

Terms:
{bullets(digest.get('terms', []))}

Open questions:
{bullets(digest.get('open_questions', []))}

Handoff:
{as_text(digest.get('handoff', '')) or '(none)'}
"""

    def generate_synthesis(self, title: str, part_digests: list, final_concepts: str) -> str:
        """Generates a synthesis from structured part digests."""
        lang_hint = self._get_lang_hint()
        digest_text = "\n\n".join(self._format_part_digest_for_prompt(digest) for digest in part_digests)
        prompt = f"""You have processed a long document titled "{title}" using a map-reduce pipeline.

Structured digests from each part:
{digest_text}

Final unresolved concepts or carry-over notes:
{final_concepts or "(none)"}

Task:
Write the final synthesis in {lang_hint}. Do not include YAML frontmatter.

Required Markdown structure:
### 核心命題
State the document's central thesis in 2-4 precise paragraphs.

### 主要發現
Synthesize the important findings across parts. Preserve concrete names, mechanisms, distinctions, and causal links.

### 證據與依據
List the strongest source-grounded details from the part digests. Do not invent unsupported evidence.

### 概念關係
Explain how the major concepts relate to one another. Use a concise Mermaid diagram only if it adds clarity.

### 限制與未解問題
Name ambiguities, contradictions, missing context, or things the source does not establish.

### 可行洞察
Offer practical or strategic takeaways grounded in the source.
"""
        system_prompt = self._build_system_prompt(
            "Create a source-grounded synthesis from structured part digests.",
            forced_template="none",
            require_yaml_header=False
        )

        try:
            return self._strip_accidental_frontmatter(
                self._complete_text(system_prompt, prompt, temperature=0.25, max_tokens=settings.MAX_OUTPUT)
            )
        except Exception as e:
            return f"Synthesis failed: {e}"
