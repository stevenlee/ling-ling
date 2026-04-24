import logging
import json
import random
import glob
import re
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from services.rag_manager import RAGManager
from core.config import SKILLS_DIR

class InsightAgent:
    def __init__(self, project_root: Path, llm, rag_manager):
        self.project_root = project_root
        self.llm = llm
        self.rag = rag_manager
        self.insights_dir = self.project_root / "lings-desktop" / "Insights"
        self.insights_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = SKILLS_DIR
        self.strategies = self._load_strategies()
        
    def _load_strategies(self) -> dict:
        strategies = {}
        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            return {}
            
        for filepath in self.skills_dir.glob("*.md"):
            try:
                content = filepath.read_text(encoding='utf-8')
                match = re.search(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
                if match:
                    yaml_data = yaml.safe_load(match.group(1))
                    body_content = content[match.end():].strip()
                    if yaml_data and 'name' in yaml_data:
                        skill_id = yaml_data['name']
                        strategies[skill_id] = yaml_data
                        strategies[skill_id]['system_prompt'] = body_content
                        logging.info(f"InsightAgent: Loaded Skill '{skill_id}' from {filepath.name}")
            except Exception as e:
                logging.error(f"Failed to load skill {filepath.name}: {e}")
        return strategies

    def generate_insight(self, strategy_id: str = "recency", user_directive: str = "") -> str:
        if strategy_id not in self.strategies:
            available = list(self.strategies.keys())
            if not available: return "❌ Error: No strategies found."
            strategy_id = random.choice(available)
        
        config = self.strategies[strategy_id]
        logging.info(f"InsightAgent: Executing '{config['name']}'...")
        
        selection = config.get('selection', {})
        input_schema = config.get('input_schema', {})
        properties = input_schema.get('properties', {})
        method = config.get('method') or properties.get('method') or selection.get('method', 'random')
        limit = config.get('limit') or properties.get('limit') or selection.get('limit', 10)
        
        context = self._get_context_by_method(method, limit, user_directive)
        base_system_prompt = config.get('system_prompt', "Analyze this.")
        
        # Manually load template for decoupling
        template_path = self.project_root / "System_Engine" / "core" / "Templates" / "insight-rpt.md"
        template_text = template_path.read_text(encoding='utf-8') if template_path.exists() else ""
        
        custom_task = (
            f"{template_text}\n\n"
            f"## 分析指令\n{base_system_prompt}\n\n"
            f"## 知識背景\n{context}"
        )
        
        report_content = self.llm.answer_query(
            query_content=f"根據設定的策略進行深度分析。\n使用者額外補充：{user_directive if user_directive else '無'}",
            wiki_context="",
            custom_instruction=custom_task
        )
        
        # --- SMART YAML MERGE ---
        # Parse the YAML from report_content and inject our exercise metadata
        import yaml
        
        final_markdown = report_content
        match = re.search(r'^---\s*\n(.*?)\n---\s*\n', report_content, re.DOTALL)
        if match:
            try:
                llm_yaml = yaml.safe_load(match.group(1))
                if isinstance(llm_yaml, dict):
                    # Inject our metadata
                    llm_yaml['exercise_strategy'] = strategy_id
                    llm_yaml['exercise_name'] = config['name']
                    llm_yaml['exercise_description'] = config['description']
                    llm_yaml['date_created'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    new_yaml_str = yaml.dump(llm_yaml, allow_unicode=True, default_flow_style=False).strip()
                    final_markdown = f"---\n{new_yaml_str}\n---\n\n{report_content[match.end():].strip()}"
            except Exception as e:
                logging.error(f"InsightAgent: Failed to merge YAML: {e}")
        
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"🎐insight-{timestamp}.md"
        (self.insights_dir / filename).write_text(final_markdown, 'utf-8')
        return final_markdown

    def generate_full_insight(self, user_directive: str = "") -> str:
        logging.info("InsightAgent: Generating FULL Report...")
        all_results = []
        # Manually load template for decoupling
        template_path = self.project_root / "System_Engine" / "core" / "Templates" / "insight-rpt.md"
        template_text = template_path.read_text(encoding='utf-8') if template_path.exists() else ""

        for strategy_id, config in self.strategies.items():
            context = self._get_context_by_method(config.get('method', 'random'), 10, user_directive)
            
            custom_task = (
                f"{template_text}\n\n"
                f"## 分析指令\n執行策略：{config['name']}\n分析目標：{config['description']}\n\n"
                f"## 知識背景\n{context}"
            )
            
            section_content = self.llm.answer_query(
                query_content=f"執行策略：{config['name']}",
                wiki_context="",
                custom_instruction=custom_task
            )
            all_results.append(f"## 📌 分析維度：{config['name']}\n\n{section_content}")

        sections_joined = "\n\n---\n\n".join(all_results)
        
        # For FULL report, we create a SINGLE YAML at the top
        final_markdown = f"""---
title: "Ling Ling 的練習本 - {datetime.now().strftime('%Y-%m-%d')}"
type: insight_report
date_created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
---

# 🎀 Ling Ling 的練習本 (Full Report)

{sections_joined}

---
*此報告由 Insight Agent 聚合所有策略自動生成。*
"""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"🎐full-insight-{timestamp}.md"
        (self.insights_dir / filename).write_text(final_markdown, 'utf-8')
        return final_markdown

    def _get_context_by_method(self, method: str, limit: int, user_directive: str = "") -> str:
        target_file = None
        target_tag = None
        file_matches = re.findall(r'\[\[(.*?)\]\]', user_directive)
        if file_matches:
            target_file = file_matches[0].split('|')[0].strip()
            if '/' in target_file: target_file = target_file.split('/')[-1]
            if target_file.lower().endswith('.md'): target_file = target_file[:-3]
        tag_matches = re.findall(r'#([^\s#]+)', user_directive)
        if tag_matches: target_tag = tag_matches[0]

        if method == "recency": return self._get_recent_context(limit)
        elif method == "tags": return self._get_tag_cluster_context(limit, target_tag)
        elif method == "islands": return self._get_island_context(limit, target_file)
        else: return self._get_random_sample_context(limit, target_file)

    def _get_recent_context(self, limit: int) -> str:
        try:
            results = self.rag.collection.get(include=['metadatas', 'documents'])
            if not results['documents']: return "No documents found."
            docs_with_meta = list(zip(results['documents'], results['metadatas']))
            docs_with_meta.sort(key=lambda x: x[1].get('timestamp', ''), reverse=True)
            pool_size = min(len(docs_with_meta), limit * 3)
            recent_pool = docs_with_meta[:pool_size]
            selection = random.sample(recent_pool, min(len(recent_pool), limit))
            return "\n---\n".join([x[0] for x in selection])
        except: return "No recent data found."

    def _get_tag_cluster_context(self, limit: int, target_tag: str = None) -> str:
        try:
            results = self.rag.collection.get(include=['metadatas', 'documents'])
            if not results['metadatas']: return self._get_random_sample_context(limit)
            if not target_tag:
                tag_counts = {}
                for meta in results['metadatas']:
                    tags = [t for t in meta.get('tags', '').split(',') if t]
                    for t in tags: tag_counts[t] = tag_counts.get(t, 0) + 1
                if not tag_counts: return self._get_random_sample_context(limit)
                interesting_tags = [t for t, count in tag_counts.items() if count >= 2]
                target_tag = random.choice(interesting_tags if interesting_tags else list(tag_counts.keys()))
            
            cluster_docs = [doc for doc, meta in zip(results['documents'], results['metadatas']) if f",{target_tag}," in meta.get('tags', '')]
            if not cluster_docs: return self._get_random_sample_context(limit)
            selection = random.sample(cluster_docs, min(len(cluster_docs), limit))
            return f"Focusing on Cluster: #{target_tag}\n\n" + "\n---\n".join(selection)
        except: return self._get_random_sample_context(limit)

    def _get_island_context(self, limit: int, target_island: str = None) -> str:
        from maintenance.wiki_linter import WikiLinter
        if not target_island:
            linter = WikiLinter(self.project_root)
            orphans = linter.scan_graph().get('orphans', [])
            if not orphans: return self._get_random_sample_context(limit)
            target_island = random.choice(orphans)
        results = self.rag.collection.get(where={"title": target_island}, limit=limit)
        docs = results.get('documents', [])
        return f"Analysis target (Knowledge Island): [[{target_island}]]\n\n" + "\n---\n".join(docs) if docs else self._get_random_sample_context(limit)

    def _get_random_sample_context(self, limit: int, target_file: str = None) -> str:
        try:
            if target_file:
                results = self.rag.collection.get(where={"title": target_file})
                docs = results.get('documents', [])
                if docs: return f"Analysis target: [[{target_file}]]\n\n" + "\n---\n".join(docs)
            results = self.rag.collection.get()
            docs = results.get('documents', [])
            if not docs: return "Empty KB."
            selection = random.sample(docs, min(len(docs), limit))
            return "\n---\n".join(selection)
        except: return "Error."
