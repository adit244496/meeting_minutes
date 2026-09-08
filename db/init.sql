-- pgvector powers two things:
--   1. voiceprint matching (192-dim ECAPA speaker embeddings)
--   2. future semantic search across meeting history (384-dim text embeddings)
CREATE EXTENSION IF NOT EXISTS vector;
