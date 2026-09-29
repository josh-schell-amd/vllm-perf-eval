"""Tests for assembling the deployable site."""

from __future__ import annotations

import json

import pytest

import build_site
from perf_eval import seal

CREDS = ("viewer", "correct horse battery staple")


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    # The real iteration count is for the public file; tests only need the format.
    monkeypatch.setattr(seal, "ITERATIONS", 1000)


def opened(out):
    """The payload a built site publishes, decrypted with the test login."""
    envelope = json.loads((out / "perf_eval.sealed.json").read_text(encoding="utf-8"))
    return json.loads(seal.unseal(envelope, *CREDS))


@pytest.fixture
def site(tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text(
        '<html><link rel="stylesheet" href="extra.css" /><script src="js/app.js"></script></html>',
        encoding="utf-8",
    )
    (site_dir / "js").mkdir()
    (site_dir / "js" / "app.js").write_text(
        "fetch('perf_eval.sealed.json').then(r=>r.json());", encoding="utf-8"
    )
    (site_dir / "extra.css").write_text("body{}", encoding="utf-8")
    return site_dir


@pytest.fixture
def payload(tmp_path):
    path = tmp_path / "data" / "perf_eval.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"models": [], "summary": {}}), encoding="utf-8")
    return path


class TestBuild:
    def test_copies_the_site_and_the_payload(self, site, payload, tmp_path):
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        assert (out / "index.html").is_file()
        assert (out / "extra.css").is_file()
        assert opened(out) == {"models": [], "summary": {}}

    def test_only_the_sealed_payload_is_published(self, site, tmp_path):
        payload = tmp_path / "perf_eval.json"
        payload.write_text(
            json.dumps(
                {"generated_at": "2026-09-29T15:35:35Z", "models": [{"model": "org/Secret-8B"}]}
            ),
            encoding="utf-8",
        )
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        assert not (out / "perf_eval.json").exists()
        published = b"".join(f.read_bytes() for f in out.rglob("*") if f.is_file())
        assert b"Secret-8B" not in published
        assert b"2026-09-29T15:35:35Z" not in published
        assert opened(out)["models"] == [{"model": "org/Secret-8B"}]

    def test_a_site_file_holding_payload_data_fails_the_build(self, site, tmp_path):
        payload = tmp_path / "perf_eval.json"
        payload.write_text(json.dumps({"models": [{"model": "org/Secret-8B"}]}), encoding="utf-8")
        (site / "notes.txt").write_text("results for org/Secret-8B", encoding="utf-8")
        with pytest.raises(RuntimeError, match="notes.txt"):
            build_site.build(site, payload, tmp_path / "_site", CREDS)
        assert not (tmp_path / "_site").exists()

    def test_missing_credentials_build_nothing(self, site, payload, tmp_path):
        with pytest.raises(seal.SealError):
            build_site.build(site, payload, tmp_path / "_site", ("viewer", ""))
        assert not (tmp_path / "_site").exists()

    def test_cache_busts_the_fetch(self, site, payload, tmp_path):
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        script = (out / "js" / "app.js").read_text(encoding="utf-8")
        assert "fetch('perf_eval.sealed.json?v=" in script

    def test_the_cache_tag_tracks_the_payload_contents(self, site, payload, tmp_path):
        def tags(out):
            script = (out / "js" / "app.js").read_text(encoding="utf-8")
            html = (out / "index.html").read_text(encoding="utf-8")
            return script.split("?v=")[1][:12], html.split("js/app.js?v=")[1][:12]

        first = tags(build_site.build(site, payload, tmp_path / "a", CREDS))
        payload.write_text(json.dumps({"models": [{"model": "org/Tag-Test"}]}), encoding="utf-8")
        second = tags(build_site.build(site, payload, tmp_path / "b", CREDS))
        # The fetch names the new payload, so the script that holds it is new too.
        assert first[0] != second[0]
        assert first[1] != second[1]

    def test_local_scripts_and_styles_get_content_tags(self, site, payload, tmp_path):
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        html = (out / "index.html").read_text(encoding="utf-8")
        assert 'href="extra.css?v=' in html
        assert 'src="js/app.js?v=' in html

    def test_only_src_and_href_attributes_are_tagged(self, site, payload, tmp_path):
        index = site / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8") + '<img data-src="extra.css">', encoding="utf-8"
        )
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        assert 'data-src="extra.css"' in (out / "index.html").read_text(encoding="utf-8")

    def test_a_page_loading_a_missing_file_fails_the_build(self, site, payload, tmp_path):
        index = site / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8") + '<script src="js/gone.js"></script>',
            encoding="utf-8",
        )
        with pytest.raises(FileNotFoundError, match="gone.js"):
            build_site.build(site, payload, tmp_path / "_site", CREDS)

    def test_rebuilding_is_idempotent(self, site, payload, tmp_path):
        out = tmp_path / "_site"
        build_site.build(site, payload, out, CREDS)
        stale = out / "stale.txt"
        stale.write_text("old", encoding="utf-8")
        build_site.build(site, payload, out, CREDS)
        assert not stale.exists()

    def test_the_event_store_is_never_published(self, site, payload, tmp_path):
        (payload.parent / "events.jsonl").write_text('{"event":"build"}\n', encoding="utf-8")
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        assert not (out / "events.jsonl").exists()
        assert [p.name for p in out.iterdir() if p.name.endswith(".jsonl")] == []


class TestFailureModes:
    def test_missing_payload_raises(self, site, tmp_path):
        with pytest.raises(FileNotFoundError, match="published payload not found"):
            build_site.build(site, tmp_path / "absent.json", tmp_path / "_site", CREDS)

    def test_missing_site_raises(self, payload, tmp_path):
        with pytest.raises(FileNotFoundError, match="site source directory not found"):
            build_site.build(tmp_path / "absent", payload, tmp_path / "_site", CREDS)

    def test_missing_index_raises(self, payload, tmp_path):
        empty = tmp_path / "empty-site"
        empty.mkdir()
        with pytest.raises(FileNotFoundError, match="site entrypoint not found"):
            build_site.build(empty, payload, tmp_path / "_site", CREDS)

    def test_invalid_payload_json_raises_before_publishing(self, site, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        out = tmp_path / "_site"
        with pytest.raises(ValueError, match="not valid JSON"):
            build_site.build(site, broken, out, CREDS)
        assert not out.exists()

    def test_a_failed_build_leaves_the_previous_output(self, site, payload, tmp_path):
        out = build_site.build(site, payload, tmp_path / "_site", CREDS)
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            build_site.build(site, broken, out, CREDS)
        assert (out / "perf_eval.sealed.json").is_file()

    @pytest.mark.parametrize(
        "html",
        [
            "<html>no data load</html>",
            "<script>fetch('perf_eval.sealed.json');fetch('perf_eval.sealed.json');</script>",
        ],
    )
    def test_the_page_must_fetch_the_payload_exactly_once(self, payload, tmp_path, html):
        site_dir = tmp_path / "site"
        (site_dir / "js").mkdir(parents=True)
        (site_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        (site_dir / "js" / "app.js").write_text(html, encoding="utf-8")
        with pytest.raises(RuntimeError, match="exactly one"):
            build_site.build(site_dir, payload, tmp_path / "_site", CREDS)


class TestRealSite:
    def test_the_checked_in_dashboard_builds(self, tmp_path):
        payload = tmp_path / "perf_eval.json"
        payload.write_text(
            json.dumps({"generated_at": "2026-01-01T00:00:00Z", "models": [], "summary": {}}),
            encoding="utf-8",
        )
        out = build_site.build(build_site.DEFAULT_SOURCE, payload, tmp_path / "_site", CREDS)
        assert "fetch('perf_eval.sealed.json?v=" in (out / "js" / "app.js").read_text(
            encoding="utf-8"
        )
        html = (out / "index.html").read_text(encoding="utf-8")
        for asset in ("app.css", "js/theme.js", "js/analysis.js", "js/app.js"):
            assert f"{asset}?v=" in html, asset

    def test_the_page_runs_no_inline_script(self):
        # The Content-Security-Policy allows scripts only from the site itself.
        html = (build_site.DEFAULT_SOURCE / "index.html").read_text(encoding="utf-8")
        assert "script-src 'self'" in html
        assert "<script>" not in html
