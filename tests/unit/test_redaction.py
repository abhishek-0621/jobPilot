from jobpilot.observability.redaction import redact


def test_redacts_nested_and_patterns():
    out = redact(
        {
            "x": [{"Authorization": "abc"}],
            "note": "used Bearer abc.def ok",
            "jwt": "eyJa.eyJb.sig",
            "hash": "sha256:" + "a" * 64,
        }
    )
    assert out["x"][0]["Authorization"] == "[REDACTED]"
    assert "abc.def" not in out["note"] and out["note"].endswith("ok")
    assert out["jwt"] == "[REDACTED]"
    assert out["hash"].startswith("sha256:a")


def test_sha256_token_survives_inside_string_and_bare_hex_redacted():
    h = "sha256:" + "ab" * 32
    out = redact({"s": f"artifact {h} done", "raw": "key " + "f" * 40})
    assert h in out["s"]
    assert "f" * 40 not in out["raw"]


def test_key_variants_redacted():
    keys = ["password", "DB_PASSWD", "access_token", "client-secret", "Cookie", "api-key",
            "apiKey", "session_id"]
    out = redact({k: "v" for k in keys})
    assert set(out.values()) == {"[REDACTED]"}


def test_no_mutation_and_passthrough():
    src = {"a": [{"token": "t", "n": 1}], "t": (1, "x"), "none": None}
    out = redact(src)
    assert src["a"][0]["token"] == "t"
    assert out["a"][0]["n"] == 1
    assert out["t"] == [1, "x"]
    assert out["none"] is None
