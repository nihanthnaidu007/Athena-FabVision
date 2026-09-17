"""RAG over user knowledge-base documents: ingestion, retrieval, upload API.

Integration surfaces (imported defensively by consumers, feature-gated
off when missing):

- ``rag.ingestion.ingest_document(document)`` -- extract, chunk, embed.
- ``rag.retrieval.retrieve(user, query, k=5)`` -- cited source items.
- ``POST /kb/documents/`` -- authenticated multipart upload.
"""
