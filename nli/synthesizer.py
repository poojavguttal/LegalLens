"""
NLI synthesizer: uses Claude to write a cited answer from search results.
"""
import logging
import os

import anthropic

logger = logging.getLogger("synthesizer")

_client: anthropic.Anthropic | None = None

_SYSTEM_PROMPT = """\
You are a legal research assistant. Answer the user's question using ONLY the
retrieved document excerpts provided. Cite sources as [1], [2], etc.

Rules:
- Base every claim on the excerpts — do not infer beyond the text
- Cite all excerpts that support each claim
- One numbered source may hold several excerpts from the same document,
  separated by [...]; cite the source number, never the excerpt

- If excerpts are insufficient, say so clearly and briefly
- Keep the answer under 200 words
- Use plain, precise legal language
- Do not repeat provenance already shown in citations
"""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _format_context(results: list[dict]) -> str:
    parts = []
    for i, r in enumerate(results, 1):
        prov = []
        if r.get("filename"):
            prov.append(r["filename"])
        # A cited source may cover several excerpts from the same document
        if r.get("pages"):
            prov.append("p." + ", ".join(str(p) for p in r["pages"]))
        elif r.get("page_number"):
            prov.append(f"p.{r['page_number']}")
        if r.get("section_header") and str(r["section_header"]).strip():
            prov.append(r["section_header"].strip()[:50])
        if r.get("sender"):
            prov.append(f"from: {r['sender']}")
        if r.get("document_date") or r.get("date"):
            prov.append(r.get("document_date") or r.get("date"))

        header = f"[{i}] ({' | '.join(prov)})" if prov else f"[{i}]"
        parts.append(f"{header}\n{r['text']}")
    return "\n\n---\n\n".join(parts)


def synthesize_answer(query: str, results: list[dict]) -> str:
    """
    Generate a cited answer from search results.
    Returns a markdown string ready for st.markdown().
    Falls back to a plain summary on error.
    """
    if not results:
        return "No relevant documents were found for this query."

    try:
        client = _get_client()
        context = _format_context(results)

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
            messages=[
                {
                    "role": "user",
                    "content": f"Question: {query}\n\nExcerpts:\n\n{context}",
                }
            ],
        )
        answer = response.content[0].text.strip()
        logger.info(f"Synthesized answer ({len(answer)} chars) for: '{query[:60]}'")
        return answer
    except Exception as e:
        logger.warning(f"Synthesis error: {e}")
        # Minimal fallback: just show result snippets
        lines = [f"**[{i}]** {r['text'][:200]}..." for i, r in enumerate(results, 1)]
        return "\n\n".join(lines)
