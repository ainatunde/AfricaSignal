"""The real boundary data: 37 states and 774 LGAs. Needs the network (about 12 MB) or a local
cache in ``data/places``; set RUN_NETWORK_TESTS=1 to run it."""

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from africasignal.places.load import (
    boundary_version,
    download_boundaries,
    load_aliases,
    load_boundaries,
)
from africasignal.places.resolve import resolve_place, resolve_point

CACHE = Path(__file__).resolve().parents[2] / "data" / "places"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RUN_NETWORK_TESTS") or (CACHE / "ADM2.geojson").exists()),
    reason="needs the boundary files: set RUN_NETWORK_TESTS=1 or populate data/places",
)


def test_real_dataset_loads_37_states_774_lgas_and_resolves(engine: Engine) -> None:
    files = download_boundaries(CACHE)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        counts = load_boundaries(s, dict(files), boundary_version(files))
        load_aliases(s)
        s.commit()
        assert counts == {"country": 1, "state": 37, "lga": 774}
        assert (
            s.execute(
                text("SELECT count(*) FROM place WHERE kind='lga' AND parent_id IS NULL")
            ).scalar_one()
            == 0
        )
        lagos_lgas = s.execute(
            text(
                "SELECT count(*) FROM place l JOIN place s ON s.id=l.parent_id WHERE s.code='NG-LA' AND l.kind='lga'"
            )
        ).scalar_one()
        assert lagos_lgas == 20

        lagos = resolve_place(s, "Lagos")
        assert (lagos.precision, lagos.place.code) == ("state", "NG-LA")  # type: ignore[union-attr]
        assert resolve_place(s, "Surulere").status == "ambiguous"
        ikeja = resolve_point(s, 6.6018, 3.3515)
        assert ikeja is not None and ikeja.lga.name == "Ikeja" and ikeja.state.name == "Lagos"
