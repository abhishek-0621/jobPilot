from jobpilot.documents.diff import bullet_diff
from jobpilot.documents.model import Bullet, ResumeDoc, RoleBlock


def _doc(roles: list[Bullet], projects: list[Bullet], ach: list[Bullet]) -> ResumeDoc:
    def block(bs: list[Bullet]) -> list[RoleBlock]:
        return [RoleBlock(employer="Example Corp", title="Engineer", start="2020", end="2024",
                          location=None, bullets=bs)] if bs else []

    return ResumeDoc(name="Asha Example", headline="Engineer", contact_line="", summary="",
                     skills=[], roles=block(roles), projects=block(projects), education=[],
                     achievements=ach)


def _b(i: str, text: str) -> Bullet:
    return Bullet(fact_id=i, text=text, priority=0)


def test_diff_statuses() -> None:
    base = _doc([_b("f1", "one"), _b("f2", "two"), _b("f3", "three")],
                [_b("f4", "four")], [_b("f5", "five"), _b("f6", "six")])
    tailored = _doc([_b("f1", "one"), _b("f2", "TWO!"), _b("f3", "tri")],
                    [_b("f9", "nine")], [_b("f5", "five")])
    rows = {r.fact_id: r for r in bullet_diff(base, tailored, rejected={"f3"}, dropped={"f6"})}
    assert rows["f1"].status == "kept" and rows["f1"].section == "experience"
    assert rows["f2"].status == "rewritten" and rows["f2"].tailored == "TWO!"
    assert rows["f3"].status == "rewrite_rejected"
    assert rows["f4"].status == "dropped" and rows["f4"].section == "projects"
    assert rows["f5"].status == "kept" and rows["f5"].section == "achievements"
    assert rows["f6"].status == "dropped"
    assert rows["f9"].status == "added_from_bank" and rows["f9"].original == ""
    assert rows["f9"].section == "projects"
