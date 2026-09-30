"""The shipped YAML files are valid and consistent with each other."""

import pytest
import yaml
from pydantic import ValidationError

from africasignal.catalog import Items, PolicySeries, load_items, load_policies
from africasignal.config import config_dir
from africasignal.sources.seed import load_seed


def test_items_load_and_cover_both_topics() -> None:
    items = load_items()
    assert {i.topic for i in items.items} == {"energy", "food"}
    assert {"pms_litre", "lpg_12_5kg", "rice_local_1kg"} <= {i.code for i in items.items}
    assert set(items.keywords) == {"energy", "food"} and all(items.keywords.values())


def test_every_item_has_labels_a_unit_and_default_materiality() -> None:
    for item in load_items().items:
        assert item.nbs_labels and item.unit and item.currency == "NGN"
        assert (item.materiality.mom_pct, item.materiality.yoy_pct) == (5.0, 20.0)


def test_every_factor_has_keywords() -> None:
    for item in load_items().items:
        codes = [f.code for f in item.factors]
        assert len(codes) == len(set(codes))
        assert all(f.keywords for f in item.factors)


def test_item_lookup_by_code() -> None:
    assert load_items().item("pms_litre").topic == "energy"
    with pytest.raises(StopIteration):
        load_items().item("nope")


def test_duplicate_item_codes_are_rejected() -> None:
    item = {"code": "x", "label": "x", "topic": "food", "unit": "u", "nbs_labels": ["x"]}
    with pytest.raises(ValidationError, match="duplicate"):
        Items.model_validate({"keywords": {"energy": [], "food": []}, "items": [item, item]})


def test_unknown_fields_are_rejected() -> None:
    item = {"code": "x", "label": "x", "topic": "food", "unit": "u", "nbs_labels": ["x"], "oops": 1}
    with pytest.raises(ValidationError):
        Items.model_validate({"keywords": {"energy": [], "food": []}, "items": [item]})


def test_thresholds_must_be_positive() -> None:
    item = {
        "code": "x", "label": "x", "topic": "food", "unit": "u", "nbs_labels": ["x"],
        "materiality": {"mom_pct": 0},
    }  # fmt: skip
    with pytest.raises(ValidationError):
        Items.model_validate({"keywords": {"energy": [], "food": []}, "items": [item]})


def test_policy_series_point_at_seeded_sources() -> None:
    slugs = {s.slug for s in load_seed()}
    for series in load_policies().series:
        assert set(series.primary_sources) <= slugs, series.code


def test_policy_scope_must_match_states() -> None:
    base = {"code": "c", "title": "t", "topic": "energy", "unit": "u", "primary_sources": ["nerc"]}
    base["affected_groups"] = "g"
    with pytest.raises(ValidationError, match="state_codes"):
        PolicySeries.model_validate({**base, "scope": "states"})
    with pytest.raises(ValidationError, match="state_codes"):
        PolicySeries.model_validate({**base, "scope": "national", "state_codes": ["NG-LA"]})


def test_config_dir_can_be_overridden(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert config_dir() == tmp_path
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"a": 1}))
