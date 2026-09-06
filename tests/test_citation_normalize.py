from nota.council import Citation, Opinion, normalize_path, validate_citations


def test_normalize_path_bracket_index_and_quotes():
    assert normalize_path("a.data.gainers[0].pct") == "a.data.gainers.0.pct"
    assert normalize_path(" `a.data.x` ") == "a.data.x"
    assert normalize_path("a.data.list[12][3].v") == "a.data.list.12.3.v"


def test_validate_keeps_bracket_citations_when_dotted_path_exists():
    op = Opinion(role="macro", stance="bullish", p_up_7d=0.6, confidence="high", thesis="t", invalidation="i",
                 citations=[Citation(path="m.data.gainers[0].pct", value="6.1"), Citation(path="m.data.nope", value="?")])
    out = validate_citations(op, {"m.data.gainers.0.pct"})
    assert [c.path for c in out.citations] == ["m.data.gainers.0.pct"] and out.dropped_citations == 1 and out.confidence == "high"


def test_citation_value_comes_from_the_evidence_not_the_model():
    op = Opinion(role="macro", stance="bullish", p_up_7d=0.6, confidence="high", thesis="t", invalidation="i",
                 citations=[Citation(path="d.data.rsi_14", value="99 to the moon"), Citation(path="d.data.blank")])
    out = validate_citations(op, {"d.data.rsi_14": 61.3, "d.data.blank": "partial"})
    assert [(c.path, c.value) for c in out.citations] == [("d.data.rsi_14", "61.3"), ("d.data.blank", "partial")]
