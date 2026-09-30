import pytest

from africasignal.llm.schema import errors

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "kind": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "count", "price", "kind", "tags"],
    "additionalProperties": False,
}
GOOD = {"name": "x", "count": 2, "price": None, "kind": "a", "tags": ["t"]}


def test_conforming_value_has_no_errors() -> None:
    assert errors(GOOD, SCHEMA) == []
    assert errors({**GOOD, "price": 1.5}, SCHEMA) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"name": 3}, "$.name: expected string"),
        ({"count": 2.5}, "$.count: expected integer"),
        ({"count": True}, "$.count: expected integer"),
        ({"price": "1"}, "$.price: matches none"),
        ({"kind": "c"}, "$.kind"),
        ({"tags": ["a", 1]}, "$.tags[1]: expected string"),
        ({"extra": 1}, "$.extra: not allowed"),
    ],
)
def test_each_kind_of_violation_is_reported(change: dict[str, object], fragment: str) -> None:
    assert any(fragment in e for e in errors({**GOOD, **change}, SCHEMA))


def test_missing_required_property() -> None:
    bad = {k: v for k, v in GOOD.items() if k != "kind"}
    assert errors(bad, SCHEMA) == ["$.kind: missing"]


def test_wrong_top_level_type() -> None:
    assert errors([], SCHEMA)[0].startswith("$: expected object")


def test_type_list_and_const() -> None:
    assert errors(None, {"type": ["string", "null"]}) == []
    assert errors("v2", {"const": "v1"}) != []


def test_unsupported_keyword_is_refused_not_ignored() -> None:
    with pytest.raises(ValueError, match="minLength"):
        errors("x", {"type": "string", "minLength": 3})
