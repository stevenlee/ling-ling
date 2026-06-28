import threading
import logging
import re
import json
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from core.state import global_busy_state

class ResearchPipeline:
    def __init__(self, llm_client):
        self.llm = llm_client
        self._cache = {}

    def search_arxiv(self, keyword: str, limit: int = 3) -> list[dict]:
        try:
            url = f"http://export.arxiv.org/api/query?search_query=all:{urllib.parse.quote(keyword)}&start=0&max_results={limit}"
            headers = {"User-Agent": "LingLingResearchBot/1.0"}
            response = requests.get(url, headers=headers, timeout=10)
            root = ET.fromstring(response.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            results = []
            for entry in root.findall("atom:entry", ns):
                title_elem = entry.find("atom:title", ns)
                summary_elem = entry.find("atom:summary", ns)
                id_elem = entry.find("atom:id", ns)
                if title_elem is not None and summary_elem is not None:
                    title = title_elem.text.replace("\n", " ").strip()
                    summary = summary_elem.text.replace("\n", " ").strip()
                    link = id_elem.text if id_elem is not None else ""
                    results.append({"title": title, "summary": summary, "url": link, "source": "arXiv"})
            return results
        except Exception as e:
            logging.error(f"ArXiv search failed for '{keyword}': {e}")
            return []

    def search_wikipedia(self, keyword: str, limit: int = 3) -> list[dict]:
        try:
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(keyword)}&utf8=&format=json&srlimit={limit}"
            headers = {"User-Agent": "LingLingResearchBot/1.0"}
            response = requests.get(search_url, headers=headers, timeout=10).json()
            search_results = response.get("query", {}).get("search", [])
            
            results = []
            for item in search_results:
                title = item["title"]
                page_url = f"https://en.wikipedia.org/w/api.php?format=json&action=query&prop=extracts&exintro&explaintext&redirects=1&titles={urllib.parse.quote(title)}"
                page_resp = requests.get(page_url, headers=headers, timeout=10).json()
                pages = page_resp.get("query", {}).get("pages", {})
                extract = ""
                for page_id, page_info in pages.items():
                    extract = page_info.get("extract", "")
                    break
                results.append({
                    "title": title,
                    "summary": extract[:1000] + ("..." if len(extract) > 1000 else ""),
                    "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                    "source": "Wikipedia"
                })
            return results
        except Exception as e:
            logging.error(f"Wikipedia search failed for '{keyword}': {e}")
            return []

    def search_patents(self, keyword: str, limit: int = 30) -> list[dict]:
        """
        Search for patents using the EuropePMC REST API.
        EuropePMC indexes a wide variety of patents (US, EP, WO, etc.) and provides a stable, free API.
        """
        try:
            import urllib.parse
            import requests
            
            query = f'(SRC:PAT) AND ("{keyword}")'
            url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={urllib.parse.quote(query)}&format=json&resultType=core&pageSize={limit}"
            
            headers = {"User-Agent": "LingLingResearchBot/1.0 (mailto:admin@example.com)"}
            response = requests.get(url, headers=headers, timeout=20)
            data = response.json()
            
            results = []
            for r in data.get("resultList", {}).get("result", []):
                p_id = r.get("id", "")
                title = r.get("title", "")
                abstract = r.get("abstractText", "No abstract provided.")
                
                # Clean up abstract if it has HTML tags
                import re, html
                title = html.unescape(re.sub(r'<[^>]+>', '', title))
                abstract = html.unescape(re.sub(r'<[^>]+>', '', abstract))
                
                results.append({
                    "id": p_id,
                    "title": title,
                    "summary": abstract,
                    "url": f"https://europepmc.org/article/PAT/{p_id}",
                    "source": "EuropePMC Patents"
                })
            return results
        except Exception as e:
            logging.error(f"EuropePMC Patent search failed for '{keyword}': {e}")
            return []


    def run_research(self, instruction: str, content: str) -> str:
        from core.ui import ui
        ui.info(f"🔍 Researching: {instruction or 'General topic'}...")
        
        # 1. Generate Keywords
        keywords = self.llm.generate_research_keywords(content, instruction)
        logging.info(f"Research Keywords: {keywords}")
        
        # 2. Fetch Data
        arxiv_wiki_results = []
        patent_results = []
        
        for kw in keywords:
            arxiv_wiki_results.extend(self.search_arxiv(kw, limit=2))
            arxiv_wiki_results.extend(self.search_wikipedia(kw, limit=2))
            patent_results.extend(self.search_patents(kw, limit=10))
        
        # 3. Generate Markdown Blocks
        elite_digest_md = ""
        if arxiv_wiki_results:
            elite_digest_md = self.llm.generate_elite_digest(arxiv_wiki_results, "arXiv & Wikipedia")
        
        patent_table_md = ""
        if patent_results:
            patent_table_md = self.llm.generate_patent_table(patent_results)
        
        # 4. Construct Final Markdown Block
        return (
            f"\n\n---\n"
            f"## 🤖 Ling-Ling Research Digest\n"
            f"**Generated for:** `{instruction or 'General topic'}`\n\n"
            f"### 📚 Academic & Concept Elite Digest\n"
            f"{elite_digest_md}\n\n"
            f"### 💡 USPTO Patent Scan\n"
            f"{patent_table_md}\n"
        )

    def process_research(self, filepath: Path, content: str, match: re.Match):
        instruction = match.group(1).strip() if match.group(1) else ""
        raw_trigger = match.group(0)
        
        # Check if instruction contains a wikilink like [[Some Document]]
        wiki_match = re.search(r"\[\[(.*?)\]\]", instruction)
        if wiki_match:
            doc_title = wiki_match.group(1).split('|')[0].strip()
            from core.vault_utils import get_note_content
            linked_content = get_note_content(doc_title)
            if linked_content:
                content = linked_content
                logging.info(f"Using content from linked note: {doc_title}")
        
        def _task():
            if not global_busy_state.try_set_busy():
                return
            try:
                logging.info(f"ResearchPipeline started for {filepath.name} with instruction: {instruction}")
                final_block = self.run_research(instruction, content)
                
                # 5. Write back to file
                # Re-read to ensure we don't overwrite user changes made during API calls
                current_content = filepath.read_text(encoding="utf-8")
                new_content = current_content.replace(raw_trigger, f"@ling-research-done {instruction}".strip())
                new_content += final_block
                filepath.write_text(new_content, encoding="utf-8")
                ui.success(f"✅ Research completed for {filepath.name}")
            except Exception as e:
                logging.exception(f"ResearchPipeline error for {filepath.name}")
            finally:
                global_busy_state.set_busy(False)
                
        threading.Thread(target=_task, daemon=True).start()


