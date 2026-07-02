import logging
import re
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET


from core.json_extract import salvage_json_array
from services.http_client import PoliteHttpClient
from services.llm.prompt_composer import lang_hint


class PatentFetchError(Exception):
    """FPO could not be fetched/parsed (network, rate-limit, structure change).

    Distinct from "genuinely zero matching patents" so the caller can tell the
    user the truth instead of blaming their keywords."""


class ResearchPipeline:
    def __init__(self, llm_client):
        self.llm = llm_client
        self._cache = {}
        self.http = PoliteHttpClient()

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
            response = self.http.get(url, source="arxiv", retries=1)
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
                    results.append(
                        {"title": title, "summary": summary, "url": link, "source": "arXiv"}
                    )
            return results
        except Exception as e:
            logging.error(f"ArXiv search failed for '{keyword}': {e}")
            return []

    def search_wikipedia(self, keyword: str, limit: int = 3) -> list[dict]:
        try:
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(keyword)}&utf8=&format=json&srlimit={limit}"
            response = self.http.get(search_url, source="wikipedia")
            data = response.json()
            search_results = data.get("query", {}).get("search", [])

            results = []
            for item in search_results:
                title = item["title"]
                page_url = f"https://en.wikipedia.org/w/api.php?format=json&action=query&prop=extracts&exintro&explaintext&redirects=1&titles={urllib.parse.quote(title)}"

                # space every wiki call, not just extracts
                page_resp = self.http.get(page_url, source="wikipedia")
                page_data = page_resp.json()
                pages = page_data.get("query", {}).get("pages", {})
                extract = ""
                for page_id, page_info in pages.items():
                    extract = page_info.get("extract", "")
                    break
                results.append(
                    {
                        "title": title,
                        "summary": extract[:1000] + ("..." if len(extract) > 1000 else ""),
                        "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                        "source": "Wikipedia",
                    }
                )
            return results
        except Exception as e:
            logging.error(f"Wikipedia search failed for '{keyword}': {e}")
            return []

    def search_patents(self, keyword: str, limit: int = 30) -> list[dict]:
        """Search patents via FreePatentsOnline (FPO) scraping.

        Returns a (possibly empty) list on a successful fetch — an empty list
        means FPO genuinely returned no matching patents. Raises
        PatentFetchError when the page can't be fetched or parsed (network,
        rate-limit, HTML change) so the caller doesn't misreport a transient
        failure as "no patents found".
        """
        from bs4 import BeautifulSoup
        import html

        url = f"https://www.freepatentsonline.com/result.html?sort=relevance&srch=top&query_txt={urllib.parse.quote(keyword)}&submit=&patents_us=on"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        }

        try:
            response = self.http.get(url, source="fpo", headers=headers)
        except Exception as e:
            logging.error(f"FPO fetch failed for '{keyword}': {e}")
            raise PatentFetchError(f"fetch failed: {e}") from e

        try:
            # FPO HTML is slightly malformed, lxml parses it robustly
            soup = BeautifulSoup(response.text, "lxml")
            tables = soup.find_all("table", class_="listing_table")

            results = []
            if not tables:
                # HTTP 200 with no listing_table: either genuinely zero hits or
                # FPO changed its HTML. Leave a breadcrumb — this used to return
                # [] silently, which made structure changes look like no-results.
                logging.warning(
                    f"FPO returned HTTP 200 for '{keyword}' but no listing_table was "
                    f"found — zero results, or the page structure changed."
                )
            if tables:
                rows = tables[0].find_all("tr")
                # Skip header row and limit results
                for row in rows[1 : limit + 1]:
                    tds = row.find_all("td")
                    if len(tds) >= 3:
                        p_id = tds[1].text.strip()
                        title_node = tds[2].find("a")
                        title = title_node.text.strip() if title_node else "Unknown Title"
                        link = (
                            "https://www.freepatentsonline.com" + title_node["href"]
                            if title_node
                            else ""
                        )

                        br = tds[2].find("br")
                        abstract = ""
                        if br and br.next_sibling:
                            abstract = br.next_sibling.text.strip()

                        title = html.unescape(re.sub(r"<[^>]+>", "", title))
                        abstract = html.unescape(re.sub(r"<[^>]+>", "", abstract))

                        results.append(
                            {
                                "id": p_id,
                                "title": title,
                                "summary": abstract or "No abstract provided.",
                                "url": link,
                                "source": "FreePatentsOnline",
                            }
                        )
            return results
        except Exception as e:
            logging.error(f"FPO parse failed for '{keyword}': {e}")
            raise PatentFetchError(f"parse failed: {e}") from e

    # ── Research rendering (moved from LLMClient in P2b: patent/digest
    #    markdown is research domain knowledge, not LLM plumbing) ──────

    def generate_research_keywords(self, content: str, instruction: str) -> list[str]:
        prompt = f"""
請根據以下內容與使用者的指示，生成 3 到 5 個適合用於學術與專利搜尋引擎（如 arXiv, Wikipedia, EuropePMC）的英文搜尋關鍵字。
**重要：關鍵字必須簡短（1 到 3 個單字為佳），並包含廣泛的上位概念（例如 "Artificial intelligence", "Machine learning", "Language model"），以便能在專利資料庫中找到結果。請勿使用過長或過於具體的長句。**
直接以 JSON 陣列格式輸出，例如 ["keyword1", "keyword2"]，不要有任何其他文字。

[User Instruction]
{instruction}

[Content]
{content}
"""
        try:
            res = self.llm.complete(
                "You are a research assistant. Output only a JSON array of strings.",
                prompt,
                temperature=0.3,
                stage="research_keywords",
            )
            import json
            import re

            match = re.search(r"\[.*\]", res, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    keywords = [
                        str(x).strip() for x in parsed if isinstance(x, str) and str(x).strip()
                    ]
                    if keywords:
                        return keywords
            return ["General Topic"]
        except Exception as e:
            logging.error(f"Failed to generate keywords: {e}")
            return ["General Topic"]

    @staticmethod
    def _md_cell(text) -> str:
        """Sanitise a value for use inside a single Markdown table cell."""
        s = str(text if text is not None else "").replace("\n", " ").replace("|", "\\|").strip()
        return s or "—"

    def generate_elite_digest(
        self, arxiv_wiki_results: list[dict], source_name: str, topic: str = ""
    ) -> str:
        # The LLM only selects and translates; URLs are rendered from our own data
        # to avoid the model corrupting links by copying them verbatim.
        import json

        indexed = [
            {
                "idx": i,
                "title": r.get("title", ""),
                "summary": r.get("summary", ""),
                "source": r.get("source", ""),
            }
            for i, r in enumerate(arxiv_wiki_results)
        ]
        data_str = json.dumps(indexed, ensure_ascii=False, indent=2)
        prompt = f"""請針對主題「{topic}」，從以下來自 {source_name} 的搜尋結果中，精選 3 到 5 篇最重要的文獻。
對每一筆你選出的文獻，請輸出：
- idx：原始清單中的索引（整數，務必照抄，不要更動）
- zh_title：繁體中文標題（可保留必要的英文專有名詞）
- zh_summary：繁體中文摘要，結構化說明其核心概念與重要洞察

請「只」輸出 JSON 陣列，依重要性由高到低排序，不要有任何其他文字或 Markdown 標記：
[{{"idx": 0, "zh_title": "...", "zh_summary": "..."}}]

[搜尋結果]
{data_str}
"""
        try:
            res = self.llm.complete(
                "You are a knowledgeable research assistant. Output only a JSON array.",
                prompt,
                temperature=0.5,
                stage="elite_digest",
            )
            rows = salvage_json_array(res)
            return self._render_elite_digest(rows, arxiv_wiki_results, topic)
        except Exception as e:
            logging.error(f"Failed to generate elite digest: {e}")
            return "無法生成摘要。"

    def _render_elite_digest(self, rows: list[dict], results: list[dict], topic: str) -> str:
        items = []
        for r in rows:
            try:
                idx = int(r.get("idx"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(results):
                continue
            src = results[idx]
            title = str(r.get("zh_title") or src.get("title", "")).strip()
            summary = str(r.get("zh_summary", "")).strip()
            url = src.get("url", "")
            source_line = f"\n    * **來源**：[{url}]({url})" if url else ""
            items.append(f"* **{title}**\n    * **摘要**：{summary}{source_line}")
        if not items:
            return "無法生成摘要。"
        header = (
            f"### 📚 Academic & Concept Elite Digest\n"
            f"以下是為您精選的 {len(items)} 篇關於「{topic}」的重要研究與文獻摘要：\n\n"
        )
        return header + "\n\n".join(items)

    _PATENT_TABLE_HEADER = (
        "| 專利編號 | 關聯性 | 主旨 | 摘要 | 全文連結 |\n| :--- | :---: | :--- | :--- | :--- |\n"
    )

    def generate_patent_table(self, patent_results: list[dict], topic: str = "") -> str:
        """Render fetched patents as a Markdown table. The LLM only filters/
        ranks/translates; patent number + URL come from our own data so the
        model can't corrupt them. subject/summary are written in the configured
        OUTPUT_LANGUAGE (not a hardcoded language). Three tiers keep patents —
        and their translation — from being lost to a flaky ranking step:
        rank+translate → translate-only → raw source text (last resort)."""
        import json

        if not patent_results:
            return "> 查無符合的專利（此主題可能較少出現在專利，或關鍵字過於限縮）。"

        lang = lang_hint()
        indexed = [
            {
                "idx": i,
                "id": p.get("id", ""),
                "title": p.get("title", ""),
                "summary": p.get("summary", ""),
            }
            for i, p in enumerate(patent_results)
        ]
        data_str = json.dumps(indexed, ensure_ascii=False, indent=2)

        # Tier 1: filter + rank + translate.
        rank_prompt = f"""請針對主題「{topic}」，從以下專利清單中篩選出與主題相關的專利，並依關聯性由高到低排序。
對每一筆你選出的專利，請輸出：
- idx：原始清單中的索引（整數，務必照抄，不要更動）
- relevance：關聯性，只能是「高」「中」「低」三者之一
- subject：「主旨」，一句話點出技術核心，用 {lang} 書寫
- summary：摘要（1~2 句），用 {lang} 書寫

請「只」輸出 JSON 陣列，不要有任何其他文字或 Markdown 標記：
[{{"idx": 0, "relevance": "高", "subject": "...", "summary": "..."}}]

[專利清單]
{data_str}
"""
        lines = self._patent_rows_to_lines(self._safe_json_rows(rank_prompt), patent_results)
        if lines:
            return self._PATENT_TABLE_HEADER + "\n".join(lines)

        # Tier 2: ranking failed — translate every patent (no ranking). A
        # simpler task than rank+translate, so its JSON is more likely to parse.
        translate_prompt = f"""請把以下每一筆專利的「主旨」與「摘要」翻譯／改寫成 {lang}。不要篩選、不要排序，全部保留。
對每一筆輸出：
- idx：原始索引（整數，照抄）
- subject：主旨（一句話點出技術核心），用 {lang}
- summary：摘要（1~2 句），用 {lang}

請「只」輸出 JSON 陣列：
[{{"idx": 0, "subject": "...", "summary": "..."}}]

[專利清單]
{data_str}
"""
        lines = self._patent_rows_to_lines(
            self._safe_json_rows(translate_prompt), patent_results, default_relevance="—"
        )
        if lines:
            note = "> ⚠️ 關聯性排序這次無法產生，以下為已翻譯但未排序的專利：\n\n"
            return note + self._PATENT_TABLE_HEADER + "\n".join(lines)

        # Tier 3: even translation failed — raw source text, clearly labelled.
        raw = [
            f"| {self._md_cell(p.get('id', ''))} | — | {self._md_cell(p.get('title', ''))} | "
            f"{self._md_cell((p.get('summary', '') or '')[:160])} | "
            f"{('[連結](' + p.get('url', '') + ')') if p.get('url') else '—'} |"
            for p in patent_results
        ]
        note = (
            "> ⚠️ 排序與翻譯這次都無法產生（LLM 格式化失敗），"
            f"以下為抓到的 {len(raw)} 筆原始專利（未排序、原文）：\n\n"
        )
        return note + self._PATENT_TABLE_HEADER + "\n".join(raw)

    def _safe_json_rows(self, prompt: str) -> list:
        """One JSON-array LLM call, salvage-parsed; [] on any failure."""
        try:
            res = self.llm.complete(
                "You are a knowledgeable research assistant. Output only a JSON array.",
                prompt,
                temperature=0.3,
                stage="patent_table",
            )
            return salvage_json_array(res)
        except Exception as e:
            logging.error(f"Patent JSON step failed: {e}")
            return []

    def _patent_rows_to_lines(
        self, rows: list[dict], patent_results: list[dict], default_relevance: str = ""
    ) -> list[str]:
        """Map LLM rows (idx/relevance/subject/summary) back onto our patent
        data, skipping any row with a bad/out-of-range idx."""
        lines = []
        for r in rows:
            try:
                idx = int(r.get("idx"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(patent_results):
                continue
            src = patent_results[idx]
            pid = self._md_cell(src.get("id", ""))
            url = src.get("url", "")
            relevance = self._md_cell(r.get("relevance", default_relevance) or default_relevance)
            subject = self._md_cell(r.get("subject", ""))
            summary = self._md_cell(r.get("summary", ""))
            link = f"[連結]({url})" if url else "—"
            lines.append(f"| {pid} | {relevance} | {subject} | {summary} | {link} |")
        return lines

    def prepare_and_run(self, instruction: str, base_content: str) -> str:
        """Unifies context preparation for both vault inline tags and prompt files."""
        # 1. Extract wikilinks and load content (Do this first before keywords truncate instruction)
        wiki_matches = re.findall(r"\[\[(.*?)\]\]", instruction)
        linked_texts = []
        if wiki_matches:
            from core.vault_utils import get_note_content

            for match in wiki_matches:
                doc_title = match.split("|")[0].strip()
                linked_content = get_note_content(doc_title)
                if linked_content:
                    linked_texts.append(f"## Source: {doc_title}\n\n{linked_content}")
                    logging.info(f"Using content from linked note: {doc_title}")

        # 2. Extract keywords
        user_keywords = []
        kw_match = re.search(r"(?:keywords:|kw:)\s*(.*)", instruction, flags=re.IGNORECASE)
        if kw_match:
            kw_str = kw_match.group(1)
            user_keywords = [k.strip() for k in kw_str.split(",") if k.strip()]
            instruction = instruction[: kw_match.start()].strip()

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
            llm_keywords = self.generate_research_keywords(content, instruction)
            for kw in llm_keywords:
                if kw not in keywords:
                    keywords.append(kw)
                if len(keywords) >= 5:
                    break

        logging.info(f"Research Keywords (User provided: {len(user_keywords)}): {keywords}")

        # 2. Fetch Data
        arxiv_wiki_results = []
        patent_results = []
        patent_fetch_failed = False

        for kw in keywords:
            ui.info(f"🔎 正在搜尋關鍵字: {kw}")
            arxiv_wiki_results.extend(self.search_arxiv(kw, limit=2))
            arxiv_wiki_results.extend(self.search_wikipedia(kw, limit=2))
            try:
                patent_results.extend(self.search_patents(kw, limit=10))
            except PatentFetchError as e:
                # A transient/structural FPO failure — record it so we don't
                # later blame the user's keywords for an empty patent section.
                patent_fetch_failed = True
                logging.warning(f"Patent fetch skipped for '{kw}': {e}")

        # Dedupe across keywords: literature by URL, patents by title
        # (collapses granted + application variants of the same invention).
        arxiv_wiki_results = self._dedupe(
            arxiv_wiki_results, key=lambda r: r.get("url") or r.get("title")
        )
        patent_results = self._dedupe(patent_results, key=lambda p: p.get("title"))

        # 3. Generate Markdown Blocks
        elite_digest_md = ""
        if arxiv_wiki_results:
            elite_digest_md = self.generate_elite_digest(
                arxiv_wiki_results, "arXiv & Wikipedia", topic=instruction
            )

        patent_table_md = ""
        if patent_results:
            patent_table_md = self.generate_patent_table(patent_results, topic=instruction)
        elif patent_fetch_failed:
            # Honest distinction: the fetch failed, so we CAN'T say there are no
            # patents — only that we couldn't reach FPO this time.
            patent_table_md = (
                "> ⚠️ 專利來源（FreePatentsOnline）這次無法連線或被限流，暫時略過。"
                "稍後重跑 `@ling-research` 再試一次。"
            )
        else:
            patent_table_md = "> 查無符合的專利（此主題可能較少出現在專利，或關鍵字過於限縮）。"

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
            logging.info(
                f"ResearchPipeline started for {filepath.name} with instruction: {instruction}"
            )
            final_block = self.prepare_and_run(instruction, content)

            # 5. Write back to file
            # Re-read to ensure we don't overwrite user changes made during API calls
            current_content = filepath.read_text(encoding="utf-8")
            new_content = current_content.replace(
                raw_trigger, f"@ling-done-research {instruction}".strip()
            )
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
                new_content = current_content.replace(
                    raw_trigger, f"@ling-failed-research {instruction}".strip()
                )
                new_content += f"\n\n> ⚠️ Ling-Ling 檢索失敗，請稍後再試或調整關鍵字。（{e}）\n"
                filepath.write_text(new_content, encoding="utf-8")
            except Exception:
                logging.exception(f"Failed to write research failure marker for {filepath.name}")
