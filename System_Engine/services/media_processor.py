import shutil
import logging
from pathlib import Path

def process_image(filepath: Path, llm_client, index_content: str, assets_dir: Path) -> dict:
    """
    Processes an image by sending it to the Vision LLM and moving it to the assets directory.
    Returns the resulting WikiPage dictionary.
    """
    try:
        logging.info(f"Vision Analysis: Analyzing image {filepath.name}...")
        result = llm_client.generate_entity_page(filename=filepath.name, index_content=index_content, image_path=filepath)
        
        # Copy to Assets for Obsidian preview
        asset_path = assets_dir / filepath.name
        shutil.copy2(filepath, asset_path)
        
        # Add image reference to content
        if result:
            content = result.get('content', '')
            content = f"![[{filepath.name}]]\n\n{content}"
            result['content'] = content
            
        return result
    except Exception as e:
        logging.error(f"Error handling image {filepath.name}: {e}")
        return None
