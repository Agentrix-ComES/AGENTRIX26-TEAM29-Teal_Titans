"""Owner: M3. Gemini gemini-embedding-001 (free) → 768-dim vectors. Lazy imports.

text-embedding-004 is no longer served on the free Gemini API (embedContent
returns NotFound); gemini-embedding-001 is the current model. Its native output
is 3072-dim, so we request output_dimensionality=DIM to match the
DocChunk.embedding Vector(768) column.
"""
from app.config import settings

DIM = 768
MODEL = "models/gemini-embedding-001"


def embed(texts: list[str]) -> list[list[float]]:
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    return [
        genai.embed_content(model=MODEL, content=t, output_dimensionality=DIM)["embedding"]
        for t in texts
    ]
    # TODO(M3): batch requests + sentence-transformers fallback when no quota.
