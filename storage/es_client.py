import os
from elasticsearch import Elasticsearch

ES_HOST  = os.getenv("ES_HOST", "http://localhost:9200")
INDEX    = "legallens"

def get_client() -> Elasticsearch:
    return Elasticsearch(ES_HOST)
