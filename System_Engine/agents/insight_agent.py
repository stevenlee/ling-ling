import logging
import random
import re
import yaml
from datetime import datetime
from pathlib import Path
from agents.base_agent import BaseAgent
from core.config import SKILLS_DIR, PROMPTS_DIR, WIKI_VAULT_DIR

class InsightAgent(BaseAgent):
    def __init__(self, llm, rag_manager):
        super().__init__(llm, rag_manager)
        self.insights_dir = WIKI_VAULT_DIR / "Insights"
        self.insights_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = SKILLS_DIR
        self.strategies = self._load_strategies()
        
    def _load_strategies(self) -> dict:
        strategies = {}
        if not self.skills_dir.exists():
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
            except Exception as e:
                logging.error(f"Failed to load skill {filepath.name}: {e}")
        return strategies

    def execute(self, task_context: dict) -> str:
        strategy_id = task_context.get('strategy_id', "recency")
        user_directive = task_context.get('user_directive', "")
        is_full_report = task_context.get('is_full_report', False)
        
        if is_full_report:
            return self.generate_full_insight(user_directive)
        else:
            return self.generate_insight(strategy_id, user_directive)

    def generate_insight(self, strategy_id: str, user_directive: str = "") -> str:
        if strategy_id not in self.strategies:
            available = list(self.strategies.keys())
            if not available: return "❌ Error: No strategies found."
            strategy_id = random.choice(available)
        
        config = self.strategies[strategy_id]
        selection = config.get('selection', {})
        method = config.get('method') or selection.get('method', 'random')
        limit = config.get('limit') or selection.get('limit', 10)
        
        context = self._get_context_by_method(method, limit, user_directive)
        
        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_insight.md")
        
        custom_task = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"## 分析指令\n{config.get('system_prompt', 'Analyze this.')}\n\n"
            f"## 知識背景\n{context}"
        )
        
        report_content = self.llm.answer_query(
            query_content=f"根據設定的策略進行深度分析。\n使用者額外補充：{user_directive if user_directive else '無'}",
            wiki_context="",
            custom_instruction=custom_task
        )
        
        # Self-correct content (e.g. Mermaid)
        report_content = self._self_correct(report_content)
        
        # Write report via standardized method
        meta = {
            "exercise_strategy": strategy_id,
            "exercise_name": config['name'],
            "exercise_description": config['description']
        }
        
        output_path = self._write_report(f"洞察分析-{config['name']}", report_content, "report_insight", meta)
        
        # Also copy to Insights folder for Obsidian visibility
        insight_file = self.insights_dir / f"🎐insight-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        insight_file.write_text(output_path.read_text(encoding='utf-8'), encoding='utf-8')
        
        return report_content

    def generate_full_insight(self, user_directive: str = "") -> str:
        all_results = []
        for strategy_id, config in self.strategies.items():
            context = self._get_context_by_method(config.get('method', 'random'), 10, user_directive)
            
            system_base = self._load_prompt("system_base.md")
            agent_instruction = self._load_prompt("agent_insight.md")
            
            custom_task = (
                f"{system_base}\n\n{agent_instruction}\n\n"
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
        sections_joined = self._self_correct(sections_joined)
        
        final_markdown = f"# 🎀 Ling Ling 的練習本 (Full Report)\n\n{sections_joined}"
        
        output_path = self._write_report("全方位洞察報告", final_markdown, "report_insight_full")
        
        insight_file = self.insights_dir / f"🎐full-insight-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        insight_file.write_text(output_path.read_text(encoding='utf-8'), encoding='utf-8')
        
        return final_markdown

    def _get_context_by_method(self, method: str, limit: int, user_directive: str = "") -> str:
        target_file = None
        target_tag = None
        file_matches = re.findall(r'\[\[(.*?)\]\]', user_directive)
        if file_matches:
            target_file = file_matches[0].split('|')[0].strip()
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
        except Exception as e:
            logging.debug(f"InsightAgent: recent context retrieval failed: {e}")
            return "No recent data found."

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
        except Exception as e:
            logging.debug(f"InsightAgent: tag cluster retrieval failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_island_context(self, limit: int, target_island: str = None) -> str:
        if not target_island:
            # We would normally import LinterAgent here if needed, but let's keep it simple
            return self._get_random_sample_context(limit)
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
        except Exception as e:
            logging.debug(f"InsightAgent: random sample retrieval failed: {e}")
            return "Error retrieving context."
