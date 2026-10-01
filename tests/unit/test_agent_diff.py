from reviewer.agent.diff import TRUNCATION_MARKER, annotate_patch, pack_chunks, parse_hunks

PATCH = """@@ -1,3 +10,4 @@
 ctx
-old
+new
+new2
 tail
@@ -40,2 +50 @@
-gone
-gone2
+one"""


def test_parse_hunks_returns_new_file_ranges():
    assert parse_hunks(PATCH) == [(10, 13), (50, 50)]


def test_parse_hunks_skips_pure_deletions_and_defaults_count_to_one():
    assert parse_hunks("@@ -5,2 +4,0 @@\n-a\n-b") == []
    assert parse_hunks("@@ -1 +7 @@\n+x") == [(7, 7)]


def test_annotate_patch_numbers_added_and_context_lines_only():
    annotated = annotate_patch(PATCH).splitlines()

    assert annotated[0] == "@@ -1,3 +10,4 @@"
    assert annotated[1] == "   10  ctx"
    assert annotated[2] == "      -old"  # removed lines have no number
    assert annotated[3] == "   11 +new"
    assert annotated[4] == "   12 +new2"
    assert annotated[5] == "   13  tail"
    assert annotated[7] == "      -gone"
    assert annotated[9] == "   50 +one"


def test_annotate_patch_handles_no_newline_marker():
    annotated = annotate_patch("@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b")

    assert annotated.splitlines()[-1] == "    1 +b"


def test_small_sections_share_a_chunk():
    result = pack_chunks([("a.py", "A" * 100), ("b.py", "B" * 100)], budget=1000, max_chunks=4)

    assert len(result.chunks) == 1
    assert result.chunks[0][0] == ["a.py", "b.py"]
    assert not result.truncated_files and not result.omitted_files


def test_sections_overflow_into_new_chunks_in_priority_order():
    sections = [(f"f{i}.py", "X" * 400) for i in range(5)]

    result = pack_chunks(sections, budget=1000, max_chunks=10)

    assert [paths for paths, _ in result.chunks] == [
        ["f0.py", "f1.py"],
        ["f2.py", "f3.py"],
        ["f4.py"],
    ]
    assert all(len(text) <= 1000 for _, text in result.chunks)


def test_oversized_section_is_truncated_at_a_line_boundary_within_budget():
    text = "\n".join(f"line {i}" for i in range(500))

    result = pack_chunks([("big.py", text)], budget=600, max_chunks=4)

    assert result.truncated_files == ["big.py"]
    chunk_text = result.chunks[0][1]
    assert len(chunk_text) <= 600
    assert "more lines not shown" in chunk_text
    assert TRUNCATION_MARKER.split("{")[0] in chunk_text
    assert "line 0" in chunk_text


def test_files_beyond_max_chunks_are_omitted_not_silently_lost():
    sections = [(f"f{i}.py", "X" * 400) for i in range(6)]

    result = pack_chunks(sections, budget=500, max_chunks=2)

    assert [paths for paths, _ in result.chunks] == [["f0.py"], ["f1.py"]]
    assert result.omitted_files == ["f2.py", "f3.py", "f4.py", "f5.py"]


def test_empty_input():
    result = pack_chunks([], budget=1000, max_chunks=4)

    assert result.chunks == [] and result.omitted_files == []
