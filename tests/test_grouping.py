"""
Grouping tests — chunk-level hits collapsed into one entry per document.

No ES required; group_by_document operates on plain result dicts.
"""
from retrieval.grouping import EXCERPT_SEPARATOR, group_by_document


def _chunk(text, score, **fields) -> dict:
    return {"text": text, "chunk_type": "section", "_score": score, **fields}


class TestGroupByDocument:

    def test_same_file_collapses_to_one_entry(self):
        results = group_by_document([
            _chunk("first",  0.03, filename="a.pdf", page_number=1),
            _chunk("second", 0.02, filename="a.pdf", page_number=3),
        ])
        assert len(results) == 1
        assert results[0]["pages"] == [1, 3]
        assert len(results[0]["chunks"]) == 2

    def test_distinct_files_stay_separate(self):
        results = group_by_document([
            _chunk("x", 0.03, filename="a.pdf", page_number=1),
            _chunk("y", 0.02, filename="b.pdf", page_number=1),
        ])
        assert [r["filename"] for r in results] == ["a.pdf", "b.pdf"]

    def test_relevance_order_preserved(self):
        results = group_by_document([
            _chunk("x", 0.03, filename="a.pdf", page_number=1),
            _chunk("y", 0.02, filename="b.pdf", page_number=1),
            _chunk("z", 0.01, filename="a.pdf", page_number=9),
        ])
        assert [r["filename"] for r in results] == ["a.pdf", "b.pdf"]
        assert results[0]["pages"] == [1, 9]

    def test_group_keeps_best_chunk_metadata_and_score(self):
        """The top-ranked chunk supplies the page the viewer opens at."""
        results = group_by_document([
            _chunk("best",  0.03, filename="a.pdf", page_number=4, section_header="Findings"),
            _chunk("worse", 0.01, filename="a.pdf", page_number=7, section_header="Appendix"),
        ])
        group = results[0]
        assert group["page_number"]    == 4
        assert group["section_header"] == "Findings"
        assert group["_score"]         == 0.03

    def test_all_excerpts_reach_synthesis(self):
        """Collapsing citations must not drop text from the answer's context."""
        results = group_by_document([
            _chunk("first",  0.03, filename="a.pdf", page_number=1),
            _chunk("second", 0.02, filename="a.pdf", page_number=3),
        ])
        assert results[0]["text"] == f"first{EXCERPT_SEPARATOR}second"

    def test_provisions_group_by_source_filing(self):
        results = group_by_document([
            _chunk("p1", 0.03, chunk_type="json_provision", source_documents=["2019/x.htm"]),
            _chunk("p2", 0.02, chunk_type="json_provision", source_documents=["2019/x.htm", "2019/y.htm"]),
            _chunk("p3", 0.01, chunk_type="json_provision", source_documents=["2019/z.htm"]),
        ])
        assert len(results) == 2
        assert len(results[0]["chunks"]) == 2

    def test_chunks_without_document_identity_never_merge(self):
        results = group_by_document([
            _chunk("a", 0.03, chunk_type="json_provision"),
            _chunk("b", 0.02, chunk_type="json_provision"),
        ])
        assert len(results) == 2

    def test_empty_results(self):
        assert group_by_document([]) == []

    def test_single_chunk_is_self_contained(self):
        """Display code reads 'chunks'/'pages' off every group, grouped or not."""
        results = group_by_document([_chunk("only", 0.03, filename="a.pdf", page_number=2)])
        assert results[0]["chunks"] == [{"text": "only", "chunk_type": "section",
                                         "_score": 0.03, "filename": "a.pdf", "page_number": 2}]
        assert results[0]["pages"] == [2]

    def test_input_results_not_mutated(self):
        original = _chunk("first", 0.03, filename="a.pdf", page_number=1)
        group_by_document([original, _chunk("second", 0.02, filename="a.pdf", page_number=3)])
        assert original["text"] == "first"
        assert "chunks" not in original
