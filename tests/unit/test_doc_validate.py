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
    assert "new_proper_noun:example corp" in validate_rewrite(src, rw, set())
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


def test_word_numbers_rejected() -> None:
    src = "Cut retrieval time by 60% for the search service using Python"
    for rw in ("Doubled throughput for the search service using Python",
               "Cut retrieval time by 60% for three search services using Python",
               "Cut retrieval time by 60% for a million search users using Python",
               "Cut retrieval time by 60% for the search service using Python, halved cost"):
        assert "new_number" in validate_rewrite(src, rw, set()), rw
    # word and digit forms of the same value are equivalent
    assert validate_rewrite("Served 3 services and cut cost by 50 percent",
                            "Served three services and cut cost by fifty percent", set()) == []
    assert validate_rewrite("Doubled throughput on the batch pipeline",
                            "Achieved 2x throughput on the batch pipeline", set()) == []


def test_number_unit_swap_rejected() -> None:
    src = "Cut latency by 60% and improved throughput 10x for the search service"
    swapped = "Cut latency by 10% and improved throughput 60x for the search service"
    assert validate_rewrite(src, swapped, set()) == ["new_number"]
    assert validate_rewrite(src, "Cut latency by 60 and improved throughput 10 for search",
                            set()) == ["new_number"]


def test_single_word_org_rejected() -> None:
    src = "Cut retrieval time by 60% for the search service using Python"
    rw = "Cut retrieval time by 60% for the Google search service using Python"
    out = validate_rewrite(src, rw,
                           set())
    assert out == ["new_proper_noun:google"]
    out = validate_rewrite(src, "Google search: cut retrieval time by 60% using Python", set())
    assert "new_proper_noun:google" in out
    assert validate_rewrite("Built a harness at Example Corporation for ranking",
                            "Built a ranking harness at Example Corp", set()) \
        == ["new_proper_noun:corp", "new_proper_noun:example corp"]


def test_title_case_verb_not_flagged() -> None:
    src = "Cut retrieval time by 60% for the search service using Python"
    rw = "Optimized retrieval time by 60% for the search service using Python"
    assert validate_rewrite(src, rw, set()) == []
    assert validate_rewrite(src, "Streamlined retrieval, cutting time by 60% on the search service",
                            set()) == []


def test_leadership_inflation_rejected() -> None:
    src = "Built a ranking service that cut retrieval time by 60%"
    out = validate_rewrite(src, "Led a team of engineers building a ranking service that cut "
                                "retrieval time by 60%", set())
    assert "new_claim:led" in out and "new_claim:team of" in out
    assert validate_rewrite("Led the ranking service rewrite that cut time by 60%",
                            "Led the ranking service rewrite that cut time by 60%", set()) == []
    assert "new_claim:managed" in validate_rewrite(src, "Managed a ranking service that cut "
                                                        "retrieval time by 60%", set())


def test_alias_and_case_sensitive_terms() -> None:
    src = "Deployed the ranking service and cut retrieval time by half"
    assert "new_tech:k8s" in validate_rewrite(src, "Deployed the ranking service on k8s and "
                                                   "cut retrieval time by half", set())
    assert validate_rewrite(src, "Deployed the ranking service on k8s and cut retrieval time "
                                 "by half", {"K8s"}) == []
    go = "Rewrote the ranking service in Go and cut retrieval time by half"
    assert validate_rewrite(src, go, set()) == ["new_tech:Go"]
    assert validate_rewrite(src, go, {"Go"}) == []
    # lower-case "go" is ordinary English, not the language
    assert validate_rewrite(src, "Deployed the ranking service to go live and cut retrieval "
                                 "time by half", set()) == []


def test_empty_source_length() -> None:
    assert "length" in validate_rewrite("", "Built things", set())
    assert validate_rewrite("", "", set()) == []
