"""Dashboard: story bank page, verify/unverify, and story titles on the answers page."""

from pathlib import Path

from jobpilot.orchestrator.engine import TaskKind
from jobpilot.profile.stories import import_stories, stored_bank
from tests.agent_contracts.test_essay_wiring import essay_env, stories
from tests.agent_contracts.test_form_prep import run


async def test_stories_page_renders(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    r = env.client().get("/ui/profile/stories")
    assert r.status_code == 200
    assert "Cutting assistant latency at Example Corp" in r.text
    assert "[ADD] number of daily users" in r.text  # missing notes shown to the user
    assert "Unverify" in r.text and "Verify" in r.text
    assert f"/ui/applications/{env.app_id}/answers" in r.text  # used by
    assert "/ui/profile/stories" in env.client().get("/ui/profile").text


def test_stories_page_without_bank(tmp_path: Path) -> None:
    from tests.agent_contracts.test_form_prep import FakePortal, make_env, snapshot
    env = make_env(tmp_path, FakePortal(snapshot()))
    r = env.client().get("/ui/profile/stories")
    assert r.status_code == 200 and "stories.example.yaml" in r.text


async def test_verify_via_post_with_origin_check(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    c = env.client()
    r = c.post("/ui/profile/stories/missed-deadline/verify", data={"verified": "true"},
               headers={"origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    with env.factory() as s:
        assert not {st.story.story_id: st.verified
                    for st in stored_bank(s).stories}["missed-deadline"]
    r = c.post("/ui/profile/stories/missed-deadline/verify", data={"verified": "true"},
               follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/ui/profile/stories/latency-cut/verify", data={"verified": "false"},
               follow_redirects=False)
    assert r.status_code == 303
    with env.factory() as s:
        state = {st.story.story_id: st.verified for st in stored_bank(s).stories}
    assert state == {"missed-deadline": True, "latency-cut": False}
    assert c.post("/ui/profile/stories/nope/verify", data={"verified": "true"},
                  follow_redirects=False).status_code == 404


async def test_review_page_shows_story_titles(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    await run(env, TaskKind.PREPARE_FORM)
    r = env.client().get(f"/ui/applications/{env.app_id}/answers")
    assert "Drafted from: Cutting assistant latency at Example Corp" in r.text
    assert "needs_story:customer" in r.text
    assert "Save to answer library" in r.text
    r = env.client().post(f"/ui/applications/{env.app_id}/answers/q_proud/library",
                          follow_redirects=False)
    assert r.status_code == 303
    r = env.client().post(f"/ui/applications/{env.app_id}/answers/first_name/library",
                          follow_redirects=False)
    assert r.status_code == 422


async def test_story_text_autoescaped(tmp_path: Path) -> None:
    env = essay_env(tmp_path)
    with env.factory() as s:
        import_stories(s, stories(title="<script>alert(1)</script>",
                                  situation="<img src=x onerror=alert(2)>"))
        s.commit()
    r = env.client().get("/ui/profile/stories")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "<img src=x" not in r.text
