import os

# Elasticsearch
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
ES_INDEX = os.getenv("ES_INDEX", "legal_docs")

# Embedding
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-mpnet-base-v2")
EMBEDDING_DIM = 768

# Ingestion
SUPPORTED_EXTENSIONS = {".pdf", ".eml", ".json", ".xlsx", ".csv"}
