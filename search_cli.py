from retrieval.search import search

def _provenance(r: dict) -> str:
    parts = []
    if r.get("filename"):     parts.append(f"File: {r['filename']}")
    if r.get("page_number"):  parts.append(f"Page: {r['page_number']}")
    if r.get("section_header") and r["section_header"].strip():
        parts.append(f"Section: {r['section_header'].strip()[:60]}")
    if r.get("document_date") or r.get("date"):
        parts.append(f"Date: {r.get('document_date') or r.get('date')}")
    if r.get("sender"):       parts.append(f"From: {r['sender']}")
    if r.get("subject"):      parts.append(f"Subject: {r['subject']}")
    if r.get("source"):       parts.append(f"Source: {r['source']}")
    return " | ".join(parts)

while True:
    print()
    query = input("Search (or 'quit'): ").strip()
    if query.lower() in ("quit", "q", ""):
        break

    results = search(query, top_k=5)

    print(f"\n{len(results)} result(s) for '{query}'")
    print("=" * 70)

    for i, r in enumerate(results, 1):
        confidence = round(r.get("_confidence", 0) * 100)
        print(f"\n[{i}]  {confidence}% confidence  |  rrf={r['_score']}  |  type={r.get('chunk_type','')}")
        print(f"     {_provenance(r)}")
        print(f"\n     {r['text']}")
        print("-" * 70)
