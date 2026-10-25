from context_memory import llm

HITS = [
    {"where": "Design › Disclosure gate", "snippet": "A deterministic Disclosure Gate withholds contact details, "
     "precise location, financial and credential attributes before anything is sent to a cloud model."},
    {"where": "Retrieval", "snippet": "Hybrid search merges BM25 keyword ranking with vector ranking by "
     "reciprocal rank fusion; recall at five rose from 0.75 to 0.90."},
]


def test_supported_sentences_get_citations_and_inventions_are_dropped():
    answer = ("The **Disclosure Gate** withholds contact details and credential attributes before anything "
              "goes to a cloud model. Hybrid search merges keyword ranking with vector ranking. "
              "Additionally, strict biometric controls guard every terminal session worldwide.")
    out = llm.ground(answer, HITS)
    assert "[1]" in out and "[2]" in out and "**" not in out
    assert "biometric" not in out


def test_nothing_supported_becomes_i_dont_know():
    assert llm.ground("The capital of France is Paris.", HITS) == llm.IDK
    assert llm.ground(llm.IDK, HITS) == llm.IDK
    assert llm.ground("", HITS) == llm.IDK


def test_existing_citations_are_replaced_not_duplicated():
    out = llm.ground("Hybrid search merges keyword ranking with vector ranking [1][2].", HITS)
    assert out.count("[2]") == 1 and out.endswith(".")
