import json
import logging
import base64
import mimetypes
import re
import yaml
from pathlib import Path
from datetime import datetime
from core.config import LLM_PROVIDER, PERSONAS_DIR, TEMPLATES_DIR, GUIDELINES_DIR, PROJECT_ROOT, settings

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

    def _build_system_prompt(self, instruction_type: str, forced_template: str = None, default_template: str = None) -> str:
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
        common_rules = f"\n## Output Language\nPlease output everything in {lang_hint}.{strict_hint}\n\n## Task\n{instruction_type}\n\n{viz_instructions}\n\nUse the standard YAML header (--- title: ... ---) at the beginning of your response."
        return f"{role_instructions}\n\n{template_instructions}\n\n{common_rules}"

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
            if self.provider in ["vllm", "ollama"]:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
                    temperature=settings.CREATIVITY,
                    max_tokens=settings.MAX_OUTPUT,
                    extra_body={"num_ctx": settings.MEMORY_LIMIT} if self.provider == "ollama" else {}
                )
                return self._hybrid_parse(response.choices[0].message.content)
            elif self.provider == "gemini":
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
        except Exception as e:
            logging.error(f"LLM Error: {e}")
            return None

    def _hybrid_parse(self, text: str) -> dict:
        """A robust parser that finds the Wiki Note within the model response."""
        result = {"title": "Untitled", "tags": [], "type": "entity", "content": text.strip() if text else ""}
        
        if not text: return result
        
        # 1. Find the first YAML block (support both --- and ```yaml)
        yaml_match = re.search(r'(?:---|```yaml)\s*\n(.*?)\n(?:---|```)\s*', text, re.DOTALL)
        if yaml_match:
            yaml_str = yaml_match.group(1).strip()
            result["content"] = text[yaml_match.end():].strip()
            
            try:
                # Aggressive cleanup: handle markdown symbols at start of line, after brackets, commas, or colons
                yaml_str = re.sub(r'(^|[:\[,\s])[\*\_]{1,2}(.*?)[\*\_]{1,2}(?=[\]\s,:]|$)', r'\1"\2"', yaml_str, flags=re.MULTILINE)
                metadata = yaml.safe_load(yaml_str)
                if isinstance(metadata, dict):
                    if "title" in metadata: result["title"] = str(metadata["title"])
                    if "tags" in metadata: result["tags"] = metadata["tags"]
                    if "type" in metadata: result["type"] = metadata["type"]
                    if "pending_concepts" in metadata: result["pending_concepts"] = metadata["pending_concepts"]
            except Exception as e:
                logging.warning(f"YAML parse failed: {e}")
                logging.debug(f"Offending YAML string:\n{yaml_str}")
        else:
            # 2. Fallback: If no YAML, try to find the first H1 title
            title_match = re.search(r'^#\s+(.*)', text, re.MULTILINE)
            if title_match:
                result["title"] = title_match.group(1).strip()
                result["content"] = text[title_match.start():].strip()
        
        return result

    def answer_query(self, query_content: str, wiki_context: str, custom_instruction: str = None) -> str:
        if custom_instruction:
            task = custom_instruction
            system_prompt = self._build_system_prompt(task, forced_template="none")
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
            if self.provider == "gemini":
                from google import genai
                response = self.client.models.generate_content(
                    model=self.model, contents=[str(user_msg)],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_prompt, 
                        temperature=settings.CREATIVITY,
                        max_output_tokens=settings.MAX_OUTPUT
                    )
                )
                return response.text
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
                    temperature=settings.CREATIVITY,
                    max_tokens=settings.MAX_OUTPUT,
                    extra_body={"num_ctx": settings.MEMORY_LIMIT} if self.provider == "ollama" else {}
                )
                return response.choices[0].message.content
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
        except: return {}

    def generate_synthesis(self, title: str, part_summaries: list[str], final_concepts: str) -> str:
        """Generates a high-level executive summary for the entire processed document."""
        lang_hint = self._get_lang_hint()
        prompt = f"""You have just finished processing a long document titled "{title}" in parts.
        
Here are the brief summaries of each part:
{chr(10).join(part_summaries)}

Key concepts and remaining thoughts:
{final_concepts}

Task:
Write a professional, high-level Executive Summary for the entire document in {lang_hint}.
Focus on the core thesis, major findings, and strategic value.
Keep it between 300-500 words.
Do not use a YAML header, just the Markdown content.
"""
        system_prompt = self._build_system_prompt("Create an Executive Summary for a multi-part knowledge entity.")
        
        try:
            if self.provider == "gemini":
                from google import genai
                response = self.client.models.generate_content(
                    model=self.model, contents=[prompt],
                    config=genai.types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.3)
                )
                return response.text
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
                    temperature=0.3
                )
                return response.choices[0].message.content
        except Exception as e:
            return f"Synthesis failed: {e}"
