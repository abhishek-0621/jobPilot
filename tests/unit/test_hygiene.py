import pytest

from jobpilot.documents.hygiene import avoid_note, clean_text, stock_phrases, text_issues

CASES = [
    ("a​b", "ab"),
    ("a ​ b", "a b"),
    ("x‌‍y⁠⁤z﻿", "xyz"),
    ("co­operate", "cooperate"),
    ("left‪right‮⁦⁩", "leftright"),
    ("tag\U000e0041\U000e007fged", "tagged"),
    ("vs️\U000e0100x", "vsx"),
    ("“quoted” and ‘single’ it’s", "\"quoted\" and 'single' it's"),
    ("I built it — and it shipped.", "I built it, and it shipped."),
    ("I built it—and it shipped.", "I built it, and it shipped."),
    ("Fast – and cheap", "Fast, and cheap"),
    ("2019–2021", "2019-2021"),
    ("10 — 20 users", "10-20 users"),
    ("Jan 2021 – Present", "Jan 2021 - Present"),
    ("New York–London route", "New York-London route"),
    ("wait―what", "wait, what"),
    ("and then…", "and then..."),
    ("— first item", "- first item"),
    ("ends with dash —", "ends with dash"),
    ("one, — two", "one, two"),
    ("keep nbsp thin", "keep nbsp thin"),
    ("see https://ex.test/a–b?q=“x” now", "see https://ex.test/a–b?q=“x” now"),
    ("mail a.b@ex.test\u200b today", "mail a.b@ex.test today"),
    ("run `a—b` here", "run `a—b` here"),
    ("", ""),
]


@pytest.mark.parametrize(("raw", "want"), CASES)
def test_clean_text(raw: str, want: str) -> None:
    assert clean_text(raw) == want


@pytest.mark.parametrize(("raw", "_"), CASES)
def test_clean_text_idempotent(raw: str, _: str) -> None:
    once = clean_text(raw)
    assert clean_text(once) == once


def test_stock_phrases_word_boundary_and_case() -> None:
    found = stock_phrases("We Delve into a rich TAPESTRY; it is a testament to grit.")
    assert found == ["delve", "tapestry", "testament to"]
    assert stock_phrases("The realms? No: realm.") == ["realm"]
    assert stock_phrases("Leveraging Kafka in today’s fast-paced world") == [
        "in today's fast-paced", "fast-paced world", "leveraging"]
    assert stock_phrases("Underscoring delivery, fostered") == ["underscoring", "fostered"]
    assert stock_phrases("I built a reporting pipeline in Python.") == []
    assert stock_phrases("cutting-edges") == []


def test_text_issues() -> None:
    assert text_issues("**Impact** shipped") == ["markdown"]
    assert text_issues("# Header\nbody") == ["markdown"]
    assert text_issues("Great news!") == []
    assert text_issues("Great news!", no_exclamation=True) == ["exclamation"]
    assert text_issues("a — b") == ["dash"]
    assert text_issues("We delve.") == ["stock_phrase:delve"]


def test_avoid_note() -> None:
    note = avoid_note(["stock_phrase:delve", "markdown", "exclamation", "other"])
    assert "delve" in note and "markdown" in note and "exclamation" in note
    assert avoid_note(["length"]) == ""


def test_phrase_file_is_editable(tmp_path) -> None:
    p = tmp_path / "p.txt"
    p.write_text("# comment\nwidget magic\n", encoding="utf-8")
    assert stock_phrases("pure Widget  Magic here", path=p) == ["widget magic"]
