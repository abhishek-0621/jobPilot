from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jobpilot.config import load_config
from jobpilot.contracts.enums import FieldDecision
from jobpilot.forms.mapping import (
    apply_suggestions,
    map_fields,
    normalize_label,
    suggest_keys,
)
from jobpilot.forms.model import AliasConfig, MappingContext, load_aliases
from jobpilot.llm.ollama_client import GenerationMeta
from jobpilot.portals.base import FormField, FormSnapshot
from jobpilot.profile.schema import load_profile_file

ROOT = Path(__file__).resolve().parents[2]
PROFILE = load_profile_file(ROOT / "tests" / "fixtures" / "profile_example.yaml")
ALIASES = load_aliases(ROOT / "config" / "field_aliases.yaml")


def make_ctx(**over: Any) -> MappingContext:
    base: dict[str, Any] = {
        "identity": PROFILE.identity,
        "links": PROFILE.links,
        "current_employment": PROFILE.current_employment,
        "education": PROFILE.education,
        "languages": PROFILE.languages,
        "library": {"notice_period": ("30 days", FieldDecision.FILL_AND_REVIEW)},
        "artifacts": {"resume_path": Path("/tmp/r.pdf"), "cover_letter_path": None,
                      "cover_letter_text": "I like building RAG systems."},
        "aliases": ALIASES,
        "years_experience": 3.5,
    }
    base.update(over)
    return MappingContext(**base)


def field(fid: str, label: str, ftype: str = "text", required: bool = False,
          options: list[str] | None = None) -> FormField:
    return FormField(field_id=fid, label=label, field_type=ftype, required=required,
                     options=options or [])


def run(*fields: FormField, ctx: MappingContext | None = None) -> dict[str, Any]:
    answers, unmatched = map_fields(FormSnapshot(url="http://127.0.0.1/f", fields=list(fields)),
                                    ctx or make_ctx())
    out: dict[str, Any] = {a.field_id: a for a in answers}
    out["_unmatched"] = [f.field_id for f in unmatched]
    return out


def test_normalize_label() -> None:
    assert normalize_label("  E-mail Address * (required)") == "e mail address"
    assert normalize_label("Phone (optional)") == "phone"


def test_exact_alias_autofill() -> None:
    a = run(field("f1", "Email Address*", "email", True))["f1"]
    assert a.proposed_value == "asha@example.com"
    assert a.decision == FieldDecision.AUTO_FILL
    assert a.confidence == 1.0
    assert a.source_fact_id == "profile:identity.email"
    assert len(a.rationale) <= 200


def test_phone_format_by_country_select() -> None:
    cc = field("cc", "Country code", "select", options=["+1", "India (+91)"])
    with_cc = run(cc, field("p", "Mobile number", "tel"))
    assert with_cc["p"].proposed_value == "9000000000"
    assert with_cc["cc"].proposed_value == "India (+91)"
    alone = run(field("p", "Mobile number", "tel"))
    assert alone["p"].proposed_value == "+919000000000"


def test_select_option_matching() -> None:
    out = run(field("c", "Country", "select", options=["United States", "IND"]),
              field("d", "Highest degree", "select", options=["B.Tech", "Bachelors"]))
    assert out["c"].proposed_value == "IND"
    assert out["c"].decision == FieldDecision.AUTO_FILL
    assert out["d"].proposed_value is None
    assert out["d"].decision == FieldDecision.ASK_USER


def test_unknown_required_ask_user() -> None:
    out = run(field("x", "Favourite colour", required=True), field("y", "Spirit animal"))
    assert out["x"].decision == FieldDecision.ASK_USER
    assert out["y"].decision == FieldDecision.NOT_APPLICABLE
    assert out["_unmatched"] == ["x", "y"]


def test_compensation_from_library_fill_and_review() -> None:
    lib = {"expected_ctc_inr_lpa": (30, FieldDecision.FILL_AND_REVIEW)}
    out = run(field("e", "Expected CTC", required=True), field("c", "Current CTC", required=True),
              ctx=make_ctx(library=lib))
    assert out["e"].proposed_value == "30"
    assert out["e"].decision == FieldDecision.FILL_AND_REVIEW
    assert out["e"].source_fact_id == "library:expected_ctc_inr_lpa"
    assert out["c"].decision == FieldDecision.ASK_USER
    assert out["c"].proposed_value is None


def test_sensitive_user_only_even_with_value() -> None:
    lib = {"work_authorization_india": ("Yes", FieldDecision.FILL_AND_REVIEW)}
    out = run(field("w", "Are you authorized to work in India?", "radio", True, ["Yes", "No"]),
              field("g", "Gender", "select", options=["Male", "Female"]),
              ctx=make_ctx(library=lib))
    assert out["w"].decision == FieldDecision.USER_ONLY
    assert out["w"].proposed_value is None
    assert out["g"].decision == FieldDecision.USER_ONLY


def test_token_subset_tie_is_ask_user() -> None:
    out = run(field("t", "Company name", required=True))
    assert out["t"].decision == FieldDecision.ASK_USER
    assert out["t"].proposed_value is None
    tok = run(field("m", "Your mobile number please"))["m"]
    assert tok.confidence == 0.8
    assert tok.proposed_value == "+919000000000"
    assert tok.decision == FieldDecision.FILL_AND_REVIEW


def test_tie_routes_to_unmatched_and_optional_is_na() -> None:
    out = run(field("t", "Company name", required=True), field("o", "Company name"))
    assert out["o"].decision == FieldDecision.NOT_APPLICABLE
    assert out["_unmatched"] == ["t", "o"]


def test_token_match_never_auto_fills() -> None:
    a = run(field("e", "Your personal email please"))["e"]
    assert a.proposed_value == "asha@example.com"
    assert a.confidence == 0.8
    assert a.decision == FieldDecision.FILL_AND_REVIEW


@pytest.mark.parametrize("label", ["Referrer email", "Manager's email address",
                                   "Emergency contact phone number", "Recruiter name",
                                   "Reference name", "Hiring manager"])
async def test_third_party_fields_ask_user(label: str) -> None:
    out = run(field("r", label, required=True), field("o", label))
    assert out["r"].decision == FieldDecision.ASK_USER
    assert out["r"].proposed_value is None
    assert out["o"].decision == FieldDecision.NOT_APPLICABLE
    assert out["_unmatched"] == []
    gw = FakeGateway({"suggestions": [{"field_id": "r", "key": "email"}]})
    assert await suggest_keys([field("r", label)], gw, ALIASES) == {}
    assert gw.calls == []
    assert apply_suggestions(FormSnapshot(url="u", fields=[field("r", label)]),
                             [field("r", label)], {"r": "email"}, make_ctx()) == []


def test_help_text_reaches_classifier() -> None:
    f = FormField(field_id="q", label="Question 3", field_type="text", required=True,
                  help_text="Are you authorized to work in India?")
    assert run(f)["q"].decision == FieldDecision.USER_ONLY


def test_e164_fallback_adds_plus_and_number_rounds() -> None:
    ident = PROFILE.identity.model_copy(update={"phone_e164": None, "phone_country_code": "91"})
    assert run(field("p", "Phone"), ctx=make_ctx(identity=ident))["p"].proposed_value == \
        "+919000000000"
    out = run(field("n", "Years of experience", "number"), ctx=make_ctx(years_experience=3.6))
    assert out["n"].proposed_value == "4"


def test_duplicate_alias_rejected() -> None:
    with pytest.raises(ValueError, match="'a' and 'b'"):
        AliasConfig.model_validate({"fields": {"a": {"aliases": ["x"]},
                                               "b": {"aliases": ["X"]}}})


def test_config_exposes_aliases_path(tmp_path: Path) -> None:
    (tmp_path / "model_routes.yaml").write_text("routes: {}\n")
    assert load_config(tmp_path).field_aliases_path == tmp_path / "field_aliases.yaml"


def test_list_profile_join() -> None:
    out = run(field("o", "Other links"), field("l", "Languages known"))
    assert out["o"].proposed_value == "https://asha.example.com, https://github.com/asha-example"
    assert out["l"].proposed_value == "English, Hindi"


def test_file_input_routing() -> None:
    out = run(field("r", "Upload your CV", "file", True),
              field("c", "Cover letter", "file"),
              field("x", "Transcript", "file", True))
    assert out["r"].proposed_value == "/tmp/r.pdf"
    assert out["r"].decision == FieldDecision.AUTO_FILL
    assert out["r"].source_fact_id == "artifact:resume"
    assert out["c"].decision == FieldDecision.NOT_APPLICABLE
    assert out["x"].decision == FieldDecision.ASK_USER


def test_experience_select_band() -> None:
    opts = ["0-2 years", "3-5 years", "5+ years"]
    out = run(field("s", "Total experience", "select", options=opts),
              field("n", "Years of experience", "number"),
              field("t", "Total years of experience"))
    assert out["s"].proposed_value == "3-5 years"
    assert out["n"].proposed_value == "4"
    assert out["t"].proposed_value == "3.5"
    high = run(field("s", "Total experience", "select", options=["1 to 3", "5+"]),
               ctx=make_ctx(years_experience=7.0))
    assert high["s"].proposed_value == "5+"


def test_cover_letter_text_for_why_question() -> None:
    a = run(field("w", "Why do you want to work here?", "textarea", True))["w"]
    assert a.proposed_value == "I like building RAG systems."
    assert a.decision == FieldDecision.FILL_AND_REVIEW
    assert a.source_fact_id == "artifact:cover_letter_text"


class FakeGateway:
    def __init__(self, canned: dict[str, Any]) -> None:
        self.canned = canned
        self.calls: list[dict[str, Any]] = []

    async def generate(self, route: str, messages: list[dict], schema: type[BaseModel],
                       prompt_version: str) -> tuple[Any, GenerationMeta]:
        self.calls.append({"route": route, "messages": messages,
                           "prompt_version": prompt_version})
        meta = GenerationMeta(model_tag="fake", prompt_version=prompt_version,
                              latency_ms=1, attempts=1, eval_count=None)
        return schema.model_validate(self.canned), meta


async def test_llm_suggests_key_not_value() -> None:
    f = field("h", "Contact handle", required=True)
    gw = FakeGateway({"suggestions": [{"field_id": "h", "key": "email"},
                                      {"field_id": "ghost", "key": "email"}]})
    ctx = make_ctx()
    _, unmatched = map_fields(FormSnapshot(url="u", fields=[f]), ctx)
    sugg = await suggest_keys(unmatched, gw, ALIASES)
    assert sugg == {"h": "email"}
    none = apply_suggestions(FormSnapshot(url="u", fields=[f]), unmatched, {"h": "middle_name"},
                             ctx)
    assert none == []  # null-profile key is not suggestable
    gh = apply_suggestions(FormSnapshot(url="u", fields=[f]), unmatched, {"h": "github"},
                           make_ctx(links={}))
    assert [x.decision for x in gh] == [FieldDecision.ASK_USER]
    assert gw.calls[0]["route"] == "form_mapping"
    assert gw.calls[0]["prompt_version"] == "field_map_v1"
    assert "Contact handle" in gw.calls[0]["messages"][1]["content"].split("<untrusted_content>")[1]
    [a] = apply_suggestions(FormSnapshot(url="u", fields=[f]), unmatched, sugg, ctx)
    assert a.proposed_value == "asha@example.com"
    assert a.decision == FieldDecision.FILL_AND_REVIEW
    assert a.confidence == 0.5
    assert a.rationale == "Model-suggested mapping"


async def test_unknown_key_dropped() -> None:
    gw = FakeGateway({"suggestions": [{"field_id": "h", "key": "password"}]})
    assert await suggest_keys([field("h", "Contact handle")], gw, ALIASES) == {}


@pytest.mark.parametrize("key", ["email", "work_authorization", "demographics"])
async def test_llm_cannot_unlock_sensitive(key: str) -> None:
    sens = field("s", "Do you require visa sponsorship?", required=True)
    gw = FakeGateway({"suggestions": [{"field_id": "s", "key": key}]})
    assert await suggest_keys([sens], gw, ALIASES) == {}
    assert gw.calls == []
    answers = apply_suggestions(FormSnapshot(url="u", fields=[sens]), [sens], {"s": key},
                                make_ctx())
    assert answers == []
    plain = field("p", "Your handle")
    gw2 = FakeGateway({"suggestions": [{"field_id": "p", "key": key}]})
    sugg = await suggest_keys([plain], gw2, ALIASES)
    assert "work_authorization" not in sugg.values()
    assert "demographics" not in sugg.values()


@pytest.mark.parametrize("label,key,files", [
    ("_systemfield_name", "full_name", False),
    ("_systemfield_email", "email", False),
    ("_systemfield_resume", "resume", True),
])
def test_ashby_systemfield_aliases(label: str, key: str, files: bool) -> None:
    from jobpilot.forms.mapping import _match_key
    assert _match_key(label, ALIASES, files=files)[0] == key
