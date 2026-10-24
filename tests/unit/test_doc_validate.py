from jobpilot.documents.validate import TECH_TERMS_PATH, load_tech_terms, validate_rewrite

SRC = "Cut retrieval time by 60% for the search service using Python"


def test_rewrite_with_new_number_rejected() -> None:
    out = validate_rewrite("Cut retrieval time by 60%", "Cut retrieval time by 75%", set())
    assert out == ["new_number"]


def test_rewrite_with_new_tool_rejected() -> None:
    out = validate_rewrite(SRC, "Cut retrieval time by 60% for search using Python and Kubernetes",
                           set())
    assert out == ["new_tech:kubernetes"]


def test_rewrite_allowed_tech_from_verified_skills() -> None:
    rw = "Cut retrieval time by 60% for search using Python and Kubernetes"
    assert validate_rewrite(SRC, rw, {"Kubernetes"}) == []
    assert validate_rewrite(SRC, rw, {"Kubernetes, Docker"}) == []


def test_symbol_tech_allowed_when_in_skills() -> None:
    src = "Wrote low-latency services for the search team"
    rw = "Wrote low-latency C++ services for the search team"
    assert validate_rewrite(src, rw, set()) == ["new_tech:c++"]
    assert validate_rewrite(src, rw, {"C++"}) == []


def test_number_normalization_equivalence() -> None:
    assert validate_rewrite("Improved recall by 60 percent across 1,000+ docs",
                            "Improved recall by 60% across 1000 docs", set()) == []
    assert validate_rewrite("Reached 2.4x throughput on the batch pipeline",
                            "Reached ~2.4x throughput on the batch pipeline", set()) == []
    assert validate_rewrite("Cut cost 60%", "Cut cost 60 percent", set()) == []


def test_ranges_and_years() -> None:
    src = "Handled 5-10 incidents per week since 2022 in the platform team"
    tail = "incidents per week in the platform team"
    assert validate_rewrite(src, f"Handled 5 to 10 {tail} since 2022", set()) == []
    assert validate_rewrite(src, f"Handled 5–10 {tail} since 2023", set()) == ["new_number"]
    assert validate_rewrite(src, f"Handled 5-12 {tail} since 2022", set()) == ["new_number"]


def test_length_bounds() -> None:
    src = "Cut retrieval time by half with caching of results"
    assert validate_rewrite(src, "Cut time", set()) == ["length"]
    assert validate_rewrite(src, src + " " + "and more detail " * 10, set()) == ["length"]
    assert validate_rewrite(src, "Halved retrieval time through result caching", set()) == []


def test_proper_noun_added_rejected() -> None:
    src = "Built an evaluation harness for the ranking team"
    rw = "Built an evaluation harness for the Example Corp ranking team"
    assert validate_rewrite(src, rw, set()) == ["new_proper_noun:example corp"]
    assert validate_rewrite("Built a harness at Example Corp for ranking",
                            "Built a ranking harness at Example Corp", set()) == []


def test_sentence_initial_verb_and_single_caps_not_flagged() -> None:
    src = "Led a team of five to ship the ranking service"
    assert validate_rewrite(src, "Led a team of five engineers shipping the ranking service",
                            set()) == []
    assert validate_rewrite(src, "Partner Led a team of five to ship the ranking service",
                            set()) == []


def test_violations_sorted_and_deduplicated() -> None:
    out = validate_rewrite("Did work", "Did work with Kubernetes and Docker, 5 and 6 times", set())
    assert out == sorted(set(out))
    assert out.count("new_number") == 1
    assert "new_tech:docker" in out and "new_tech:kubernetes" in out


def test_load_tech_terms(tmp_path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("# c\nPython\n\n  C++ \n")
    assert load_tech_terms(p) == {"python", "c++"}
    shipped = load_tech_terms(TECH_TERMS_PATH)
    assert len(shipped) >= 400 and {"model context protocol", "c#", ".net", "node.js"} <= shipped
