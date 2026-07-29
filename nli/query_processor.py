"""
NLI query processor: uses Claude to understand natural language legal queries.
Extracts intent, entities, and search filters before hitting Elasticsearch.
"""
import json
import logging
import os

import anthropic

logger = logging.getLogger("query_processor")

_client: anthropic.Anthropic | None = None

_SYSTEM_PROMPT = """\
You are a legal search query analyzer for a hybrid document retrieval system.
Given a user's natural language question, extract structured search parameters.

The index contains three document types — use these exact values in your JSON:
- "pdf"            → commercial contracts, court opinions, legal briefs
- "email"          → email threads (Enron corpus) with sender/date metadata
- "compliance"     → LEDGAR regulatory provisions from SEC filings

Respond with valid JSON only. No prose, no markdown fences.

Schema:
{
  "intent": "factual" | "conceptual" | "filter",
  "reformulated_query": "<clean retrieval query, no filter conditions>",
  "filters": {
    "doc_type": "<pdf|email|compliance|null>",
    "date_from": "<YYYY-MM-DD or null>",
    "date_to": "<YYYY-MM-DD or null>",
    "party_names": ["<name>", ...]
  },
  "explanation": "<one sentence: what the query is asking for>"
}

Intent:
- factual    → specific fact wanted (ruling, party, date, obligation)
- conceptual → semantically related content (clause types, legal concepts)
- filter     → metadata-driven (emails from X, contracts after Y)

Rules:
- Set doc_type null when no document type is specified or implied
- Set date fields null when no date constraint is mentioned
- Set party_names [] when no named parties appear
- reformulated_query must be a clean search string, NOT a question
- Append party names to reformulated_query so they boost retrieval
"""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def process_query(user_query: str) -> dict:
    """
    Parse a natural language legal query into structured search parameters.

    Returns a dict with keys:
      intent, reformulated_query, filters (doc_type, date_from, date_to, party_names),
      explanation
    Falls back gracefully on any Claude/parse error.
    """
    fallback = {
        "intent": "conceptual",
        "reformulated_query": user_query,
        "filters": {"doc_type": None, "date_from": None, "date_to": None, "party_names": []},
        "explanation": "Using original query (NLI unavailable).",
    }

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": f"Query: {user_query}"}],
        )
        text = response.content[0].text.strip()
        parsed = json.loads(text)
        logger.info(f"NLI → intent={parsed.get('intent')} reformulated='{parsed.get('reformulated_query')}'")
        return parsed
    except json.JSONDecodeError as e:
        logger.warning(f"NLI JSON parse error: {e}")
        return fallback
    except Exception as e:
        logger.warning(f"NLI error: {e}")
        return fallback
