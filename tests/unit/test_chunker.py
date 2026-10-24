from context_memory.memory.chunker import chunk_markdown, split_sections
from context_memory.memory.embedder import HashEmbedder

count = HashEmbedder().count_tokens


def test_heading_paths_nest_and_pop():
    md = "# A\nintro\n## B\nbody b\n### C\nbody c\n## D\nbody d\n"
    paths = [" > ".join(s.path) for s in split_sections(md)]
    assert paths == ["A", "A > B", "A > B > C", "A > D"]


def test_headings_inside_code_fence_are_ignored():
    md = "# Real\n```python\n# not a heading\nx = 1\n```\nafter\n"
    secs = split_sections(md)
    assert len(secs) == 1
    assert "# not a heading" in secs[0].body


def test_tilde_fence_not_closed_by_backticks():
    md = "# H\n~~~\n```\n# still code\n~~~\n## Next\ntext\n"
    paths = [" > ".join(s.path) for s in split_sections(md)]
    assert paths == ["H", "H > Next"]


def test_preamble_without_heading_kept():
    secs = split_sections("loose text\n\n# H\nbody\n")
    assert secs[0].path == [] and secs[0].body == "loose text"


def test_chunks_respect_token_limit():
    para = " ".join(["word"] * 40)
    md = "# H\n\n" + "\n\n".join([para] * 10)
    chunks = chunk_markdown(md, count, max_tokens=120)
    assert len(chunks) > 1
    assert all(c.tokens <= 120 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_oversized_single_line_is_split():
    md = "# H\n" + " ".join(["tok"] * 1000)
    chunks = chunk_markdown(md, count, max_tokens=100)
    assert len(chunks) >= 10
    assert all(c.tokens <= 100 for c in chunks)


def test_code_block_kept_whole_when_it_fits():
    md = "# H\ntext\n\n```\nline1\n\nline2\n```\n"
    chunks = chunk_markdown(md, count, max_tokens=500)
    assert len(chunks) == 1
    assert "line1\n\nline2" in chunks[0].text


def test_empty_and_whitespace_documents():
    assert chunk_markdown("", count) == []
    assert chunk_markdown("\n\n   \n# Only heading\n", count) == []


def test_unclosed_fence_does_not_crash():
    chunks = chunk_markdown("# H\n```\nnever closed\n# hidden", count)
    assert len(chunks) == 1 and "# hidden" in chunks[0].text
