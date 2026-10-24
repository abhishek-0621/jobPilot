from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import BaseModel

from jobpilot.util.hashing import canonical_json, content_hash


class Color(StrEnum):
    RED = "red"


class Item(BaseModel):
    name: str
    when: datetime


def test_hash_key_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_hash_changes_with_value():
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_hash_prefix():
    assert content_hash({}).startswith("sha256:")


def test_canonical_format():
    assert canonical_json({"b": "é", "a": [1]}) == '{"a":[1],"b":"é"}'


def test_datetime_normalized_to_utc():
    tz = timezone(timedelta(hours=2))
    a = canonical_json(datetime(2026, 1, 1, 2, 0, tzinfo=tz))
    b = canonical_json(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert a == b
    assert "2026-01-01T00:00:00" in a


def test_enum_set_path_basemodel():
    assert canonical_json(Color.RED) == '"red"'
    assert canonical_json({3, 1, 2}) == "[1,2,3]"
    assert canonical_json(Path("/x/y")) == '"/x/y"'
    item = Item(name="n", when=datetime(2026, 1, 1, tzinfo=UTC))
    assert content_hash(item) == content_hash({"name": "n", "when": item.when})


def test_unknown_type_raises():
    with pytest.raises(TypeError):
        canonical_json(object())
