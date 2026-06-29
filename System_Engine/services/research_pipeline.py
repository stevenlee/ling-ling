import threading
import logging
import re
import json
import time
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from core.state import global_busy_state

class ResearchPipeline:
    def __init__(self, llm_client):
        self.llm = llm_client
        self._cache = {}

    def _get_with_retry(self, url: str, headers: dict, timeout: int = 20, retries: int = 3):
        """GET with retry: exponential backoff on HTTP 429, fixed backoff on other transient errors."""
        last_exc = None
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.exceptions.HTTPError as e:
                last_exc = e
                status = e.response.status_code if e.response is not None else None
                if status == 429 and attempt < retries - 1:
                    time.sleep(2 ** attempt + 2)
                    continue
                raise
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                raise
        if last_exc:
            raise last_exc

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    def _dedupe(self, items: list[dict], key) -> list[dict]:
        seen = set()
        out = []
        for it in items:
            k = self._norm(str(key(it)))
            if k and k not in seen:
                seen.add(k)
                out.append(it)
        return out

    def search_arxiv(self, keyword: str, limit: int = 3) -> list[dict]:
        try:
            url = f"http://export.arxiv.org/api/query?search_query=all:%22{urllib.parse.quote(keyword)}%22&start=0&max_results={limit}"
            headers = {"User-Agent": "LingLingResearchBot/1.0 (mailto:admin@example.com)"}
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
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
            headers = {"User-Agent": "LingLingResearchBot/1.0 (mailto:admin@example.com)"}

            response = self._get_with_retry(search_url, headers)
            data = response.json()
            search_results = data.get("query", {}).get("search", [])

            results = []
            for item in search_results:
                title = item["title"]
                page_url = f"https://en.wikipedia.org/w/api.php?format=json&action=query&prop=extracts&exintro&explaintext&redirects=1&titles={urllib.parse.quote(title)}"

                time.sleep(1)  # Be nice to the API
                page_resp = self._get_with_retry(page_url, headers)
                page_data = page_resp.json()
                pages = page_data.get("query", {}).get("pages", {})
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
        Search for patents using FreePatentsOnline (FPO) scraping.
        FPO provides snippets directly in the search results page without API keys.
        """
        try:
            from bs4 import BeautifulSoup
            import html

            url = f"https://www.freepatentsonline.com/result.html?sort=relevance&srch=top&query_txt={urllib.parse.quote(keyword)}&submit=&patents_us=on"
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}

            response = self._get_with_retry(url, headers)

            # FPO HTML is slightly malformed, lxml parses it robustly
            soup = BeautifulSoup(response.text, "lxml")
            tables = soup.find_all("table", class_="listing_table")
            
            results = []
            if tables:
                rows = tables[0].find_all("tr")
                # Skip header row and limit results
                for row in rows[1:limit+1]:
                    tds = row.find_all("td")
                    if len(tds) >= 3:
                        p_id = tds[1].text.strip()
                        title_node = tds[2].find("a")
                        title = title_node.text.strip() if title_node else "Unknown Title"
                        link = "https://www.freepatentsonline.com" + title_node["href"] if title_node else ""
                        
                        br = tds[2].find("br")
                        abstract = ""
                        if br and br.next_sibling:
                            abstract = br.next_sibling.text.strip()
                        
                        title = html.unescape(re.sub(r'<[^>]+>', '', title))
                        abstract = html.unescape(re.sub(r'<[^>]+>', '', abstract))
                        
                        results.append({
                            "id": p_id,
                            "title": title,
                            "summary": abstract or "No abstract provided.",
                            "url": link,
                            "source": "FreePatentsOnline"
                        })
            return results
        except Exception as e:
            logging.error(f"FPO Patent search failed for '{keyword}': {e}")
            return []


    def prepare_and_run(self, instruction: str, base_content: str) -> str:
        """Unifies context preparation for both vault inline tags and prompt files."""
        # 1. Extract wikilinks and load content (Do this first before keywords truncate instruction)
        wiki_matches = re.findall(r"\[\[(.*?)\]\]", instruction)
        linked_texts = []
        if wiki_matches:
            from core.vault_utils import get_note_content
            for match in wiki_matches:
                doc_title = match.split('|')[0].strip()
                linked_content = get_note_content(doc_title)
                if linked_content:
                    linked_texts.append(f"## Source: {doc_title}\n\n{linked_content}")
                    logging.info(f"Using content from linked note: {doc_title}")
                    
        # 2. Extract keywords
        user_keywords = []
        kw_match = re.search(r"(?:keywords:|kw:)\s*(.*)", instruction, flags=re.IGNORECASE)
        if kw_match:
            kw_str = kw_match.group(1)
            user_keywords = [k.strip() for k in kw_str.split(',') if k.strip()]
            instruction = instruction[:kw_match.start()].strip()
            
        # 3. Combine content
        final_content = "\n\n".join(linked_texts) if linked_texts else base_content
            
        return self.run_research(instruction, final_content, user_keywords=user_keywords)

    def run_research(self, instruction: str, content: str, user_keywords: list[str] = None) -> str:
        user_keywords = user_keywords or []

        from core.ui import ui
        ui.info(f"🔍 Researching: {instruction or 'General topic'}...")
        
        # 1. Generate Keywords
        keywords = user_keywords.copy()
        
        if len(keywords) < 5:
            llm_keywords = self.llm.generate_research_keywords(content, instruction)
            for kw in llm_keywords:
                if kw not in keywords:
                    keywords.append(kw)
                if len(keywords) >= 5:
                    break
                    
        logging.info(f"Research Keywords (User provided: {len(user_keywords)}): {keywords}")
        
        # 2. Fetch Data
        arxiv_wiki_results = []
        patent_results = []
        
        for kw in keywords:
            ui.info(f"🔎 正在搜尋關鍵字: {kw}")
            arxiv_wiki_results.extend(self.search_arxiv(kw, limit=2))
            arxiv_wiki_results.extend(self.search_wikipedia(kw, limit=2))
            patent_results.extend(self.search_patents(kw, limit=10))

        # Dedupe across keywords: literature by URL, patents by title
        # (collapses granted + application variants of the same invention).
        arxiv_wiki_results = self._dedupe(arxiv_wiki_results, key=lambda r: r.get("url") or r.get("title"))
        patent_results = self._dedupe(patent_results, key=lambda p: p.get("title"))

        # 3. Generate Markdown Blocks
        elite_digest_md = ""
        if arxiv_wiki_results:
            elite_digest_md = self.llm.generate_elite_digest(arxiv_wiki_results, "arXiv & Wikipedia", topic=instruction)
        
        patent_table_md = ""
        if patent_results:
            patent_table_md = self.llm.generate_patent_table(patent_results, topic=instruction)
        else:
            patent_table_md = "> 找不到相關的專利資料。可能是關鍵字過於限縮或沒有符合的專利。"
        
        # 4. Construct Final Markdown Block
        keywords_str = ", ".join(keywords)
        return (
            f"\n\n---\n"
            f"## 🤖 Ling-Ling Research Digest\n"
            f"**Generated for:** {instruction or 'General topic'}\n"
            f"**Keywords:** {keywords_str}\n\n"
            f"{elite_digest_md}\n\n"
            f"### 💡 專利掃描 (FreePatentsOnline)\n"
            f"{patent_table_md}\n"
        )

    def process_research(self, filepath: Path, content: str, match: re.Match):
        instruction = match.group(1).strip() if match.group(1) else ""
        raw_trigger = match.group(0)
        
        try:
            logging.info(f"ResearchPipeline started for {filepath.name} with instruction: {instruction}")
            final_block = self.prepare_and_run(instruction, content)
            
            # 5. Write back to file
            # Re-read to ensure we don't overwrite user changes made during API calls
            current_content = filepath.read_text(encoding="utf-8")
            new_content = current_content.replace(raw_trigger, f"@ling-done-research {instruction}".strip())
            new_content += final_block
            filepath.write_text(new_content, encoding="utf-8")
            from core.ui import ui
            ui.success(f"✅ Research completed for {filepath.name}")
        except Exception as e:
            logging.exception(f"ResearchPipeline error for {filepath.name}")
            # Mark the trigger as failed so it is not re-fired on every subsequent
            # edit. The marker reorders the tokens (@ling-failed-research) so it no
            # longer contains the "@ling-research" trigger substring at all.
            try:
                current_content = filepath.read_text(encoding="utf-8")
                new_content = current_content.replace(raw_trigger, f"@ling-failed-research {instruction}".strip())
                new_content += f"\n\n> ⚠️ Ling-Ling 檢索失敗，請稍後再試或調整關鍵字。（{e}）\n"
                filepath.write_text(new_content, encoding="utf-8")
            except Exception:
                logging.exception(f"Failed to write research failure marker for {filepath.name}")


