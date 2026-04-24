import os
from huggingface_hub import snapshot_download
from core.config import DATABASE_DIR

def download_models():
    # 指定模型存放路徑
    models_dir = DATABASE_DIR / "models"
    os.makedirs(models_dir, exist_ok=True)
    
    print(f"Starting model download to: {models_dir}")
    print("This may take a while depending on your connection...")
    
    # 下載 MinerU 核心模型 (PDF-Extract-Kit)
    snapshot_download(
        repo_id="OpenDataLab/PDF-Extract-Kit",
        local_dir=models_dir,
        max_workers=4
    )
    
    print("\nDownload complete!")
    print(f"Models are now located in: {models_dir}")
    print("You can now test the PDF ingestion feature.")

if __name__ == "__main__":
    download_models()