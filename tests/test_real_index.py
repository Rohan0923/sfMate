"""The committed search index with the real embedding model (slow: loads the model). Skipped if either is missing."""
import pytest

from app import config, rag

pytestmark = pytest.mark.skipif(not (config.DATA_DIR / "embeddings.npy").exists(), reason="no built index")


@pytest.fixture(scope="module")
def real_index():
    pytest.importorskip("sentence_transformers")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(config.EMBED_MODEL_ID, device="cpu")
    yield model


@pytest.mark.parametrize("question,expected", [
    ("Can I have my phone out at a school dance?", "Handbook 2026-27 §4.4 Dances"),
    ("What's the dress code?", "§4.5 Dress Code"),
    ("What do I need to take AP Chemistry?", "Advanced Placement Chemistry"),
    ("How many years of science do I need to graduate?", "Science - 6 Semesters Required"),
    ("Is there a woodworking class?", "Introduction to Woodworking"),
])
def test_questions_find_the_right_source(real_index, question, expected):
    rag.set_embedder(real_index)
    rag.load_index()
    titles = [c["title"] for c, _ in rag.retrieve(question, k=3)]
    assert any(expected in t for t in titles), titles
