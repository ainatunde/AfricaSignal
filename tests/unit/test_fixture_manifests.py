"""The harvested fixtures match their manifests (RSS, GDELT, NERC, NMDPRA, NNPC)."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DIRS = ["rss", "gdelt", "nerc", "nmdpra", "nnpc"]
MAX_BODY = 300


def manifest(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((FIXTURES / name / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", DIRS)
def test_every_file_in_the_directory_is_in_the_manifest_and_vice_versa(name: str) -> None:
    listed = {f["file"] for f in manifest(name)["files"]}
    present = {p.name for p in (FIXTURES / name).iterdir()} - {"manifest.json"}
    assert listed == present


@pytest.mark.parametrize("name", ["gdelt", "nerc", "nmdpra"])
def test_untouched_files_match_their_recorded_checksums(name: str) -> None:
    for entry in manifest(name)["files"]:
        data = (FIXTURES / name / entry["file"]).read_bytes()
        assert len(data) == entry["bytes"], entry["file"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["file"]


@pytest.mark.parametrize("name", DIRS)
def test_every_entry_says_where_and_when_it_was_fetched(name: str) -> None:
    doc = manifest(name)
    assert doc["retrieved_on"] == "2026-09-30"
    for entry in doc["files"]:
        assert entry.get("source_url") or entry.get("feed_url"), entry["file"]
        assert entry["retrieved_on"] == "2026-09-30"


def test_rss_feeds_are_well_formed_rss_with_trimmed_bodies() -> None:
    doc = manifest("rss")
    assert len(doc["files"]) == 8
    assert {o["outlet"] for o in doc["not_available"]} == {"TheCable", "The Guardian Nigeria"}
    for entry in doc["files"]:
        root = ET.parse(FIXTURES / "rss" / entry["file"]).getroot()
        items = root.findall("./channel/item")
        assert len(items) == entry["items"] > 0, entry["file"]
        assert root.tag == "rss"
        for item in items:
            assert item.findtext("title") and item.findtext("link") and item.findtext("pubDate")
            for tag in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"):
                body = item.findtext(tag)
                assert body is None or len(body) <= MAX_BODY, (entry["file"], tag)
        assert entry["terms_note"] and entry["robots_txt"]


def test_gdelt_counts_in_the_manifest_match_the_files() -> None:
    by_file = {f["file"]: f for f in manifest("gdelt")["files"]}
    for name in ("20260930180000.export.CSV.zip", "20260930154500.export.CSV.zip"):
        z = zipfile.ZipFile(FIXTURES / "gdelt" / name)
        rows = [r.split("\t") for r in z.read(z.namelist()[0]).decode("utf-8").splitlines()]
        assert {len(r) for r in rows} == {61}
        entry = by_file[name]
        assert len(rows) == entry["rows"]
        assert sum(r[53] == "NI" for r in rows) == entry["action_geo_country_NI_nigeria"]
        assert sum(r[53] == "NG" for r in rows) == entry["action_geo_country_NG_niger"]


def test_gdelt_has_real_rows_coded_ng_that_are_niger_not_nigeria() -> None:
    """NG is Niger and NI is Nigeria in FIPS 10-4 (plan B6.7): these rows must be rejected by the
    filter ActionGeo_CountryCode == NI or an actor country code of NGA."""
    z = zipfile.ZipFile(FIXTURES / "gdelt" / "20260930154500.export.CSV.zip")
    rows = [r.split("	") for r in z.read(z.namelist()[0]).decode("utf-8").splitlines()]
    niger = [r for r in rows if r[37] == "NG" or r[44] == "NG"]
    assert len(niger) == 7
    assert all(r[53] != "NI" and r[7] != "NGA" and r[17] != "NGA" for r in niger)
    nigeria = [r for r in rows if r[53] == "NI"]
    assert nigeria and all("Nigeria" in r[52] for r in nigeria)


def test_gdelt_mentions_and_gkg_shapes() -> None:
    z = zipfile.ZipFile(FIXTURES / "gdelt" / "20260930180000.mentions.CSV.zip")
    rows = [r.split("\t") for r in z.read(z.namelist()[0]).decode("utf-8").splitlines()]
    assert {len(r) for r in rows} == {16}
    gkg = (FIXTURES / "gdelt" / "20260930180000.gkg.nigeria-rows.tsv").read_text(encoding="utf-8")
    lines = gkg.splitlines()
    assert len(lines) == 12 and {len(line.split("\t")) for line in lines} == {27}
    assert all("#Nigeria#" in line.split("\t")[9] for line in lines)


def test_gdelt_lastupdate_lists_three_files_and_uses_http_urls() -> None:
    lines = (FIXTURES / "gdelt" / "lastupdate.txt").read_text().splitlines()
    names = [line.split()[2].rsplit("/", 1)[1] for line in lines]
    assert [n.split(".")[1] for n in names] == ["export", "mentions", "gkg"]
    assert all(line.split()[2].startswith("http://data.gdeltproject.org/") for line in lines)


def test_nerc_listing_has_publication_entries_with_pdf_links() -> None:
    html = (FIXTURES / "nerc" / "orders.html").read_text(encoding="utf-8")
    titles = re.findall(r'<h6 class="title">([^<]+)</h6>', html)
    pdfs = re.findall(r'href="(https://nerc\.gov\.ng/wp-content/uploads/[^"]+\.pdf)"', html)
    assert "IE MYTO SEPTEMBER 2026" in titles and len(titles) >= 10 and len(pdfs) >= 10


def test_nerc_pdfs_are_pdfs() -> None:
    for entry in manifest("nerc")["files"]:
        if entry["file"].endswith(".pdf"):
            assert (FIXTURES / "nerc" / entry["file"]).read_bytes().startswith(b"%PDF")


def test_nmdpra_page_is_the_blazor_shell_without_content_links() -> None:
    html = (FIXTURES / "nmdpra" / "home.html").read_text(encoding="utf-8")
    assert "blazor.webassembly.js" in html
    assert "press-release" not in re.sub(r"<[^>]+>", " ", html).lower()


def test_nnpc_posts_are_strapi_json_with_trimmed_content() -> None:
    doc = json.loads((FIXTURES / "nnpc" / "posts_newest.json").read_text(encoding="utf-8"))
    assert len(doc["data"]) == 10 and doc["meta"]["pagination"]["total"] > 400
    for post in doc["data"]:
        assert {"id", "title", "content", "post_date", "slug", "publishedAt"} <= set(post)
        assert len(post["content"]) <= MAX_BODY
    assert doc["data"][0]["post_date"] >= doc["data"][-1]["post_date"].replace("-07-", "-06-")
