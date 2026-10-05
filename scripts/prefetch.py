"""Run during Render's build: downloads the search model so the server starts fast and never fetches it at runtime."""
from sentence_transformers import SentenceTransformer

from app import config

if __name__ == "__main__":
    SentenceTransformer(config.EMBED_MODEL_ID, device="cpu")
    print(f"cached {config.EMBED_MODEL_ID}")
