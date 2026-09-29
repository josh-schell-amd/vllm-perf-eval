"""Tests for sealing the published payload behind the dashboard login."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from perf_eval import seal

LOGIN = ("viewer", "correct horse battery staple")
PAYLOAD = json.dumps({"models": [{"model": "org/Secret-8B"}]}).encode()


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(seal, "ITERATIONS", 1000)


def test_the_login_opens_what_it_sealed():
    assert seal.unseal(seal.seal(PAYLOAD, *LOGIN), *LOGIN) == PAYLOAD


def test_the_sealed_envelope_holds_nothing_readable():
    text = json.dumps(seal.seal(PAYLOAD, *LOGIN))
    assert "Secret-8B" not in text


@pytest.mark.parametrize("login", [("viewer", "wrong"), ("someone", LOGIN[1])])
def test_a_wrong_username_or_password_does_not_open_it(login):
    with pytest.raises(seal.SealError):
        seal.unseal(seal.seal(PAYLOAD, *LOGIN), *login)


def test_a_tampered_envelope_does_not_open():
    envelope = seal.seal(PAYLOAD, *LOGIN)
    data = bytearray(seal.base64.b64decode(envelope["data"]))
    data[0] ^= 1
    with pytest.raises(seal.SealError):
        seal.unseal({**envelope, "data": seal._b64(bytes(data))}, *LOGIN)


def test_each_seal_uses_a_new_iv_but_the_same_key():
    # The same key across deploys keeps readers signed in; a reused IV with
    # AES-GCM would leak the data.
    first, second = seal.seal(PAYLOAD, *LOGIN), seal.seal(PAYLOAD, *LOGIN)
    assert first["salt"] == second["salt"]
    assert first["iv"] != second["iv"]


@pytest.mark.parametrize("login", [("", "pw"), ("viewer", "")])
def test_missing_credentials_seal_nothing(login):
    with pytest.raises(seal.SealError):
        seal.seal(PAYLOAD, *login)


def test_credentials_must_both_be_in_the_environment(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "viewer")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(seal.SealError):
        seal.credentials_from_env()


def test_the_pages_fixture_is_one_seal_py_opens():
    # tests/js/unlock.test.js opens this file in the page's code, so the two
    # implementations agree on every parameter.
    fixture = Path(__file__).parent / "js" / "fixtures" / "sealed.json"
    envelope = json.loads(fixture.read_text(encoding="utf-8"))
    payload = json.loads(seal.unseal(envelope, "viewer", "fixture-password"))
    assert payload["models"] == [{"model": "org/Fixture-8B"}]


class TestOpenCli:
    def run(self, monkeypatch, tmp_path, envelope_text):
        sealed, out = tmp_path / "live.sealed.json", tmp_path / "live.json"
        if envelope_text is not None:
            sealed.write_text(envelope_text, encoding="utf-8")
        monkeypatch.setenv("DASHBOARD_USERNAME", LOGIN[0])
        monkeypatch.setenv("DASHBOARD_PASSWORD", LOGIN[1])
        monkeypatch.setattr("sys.argv", ["seal.py", "--open", str(sealed), "--output", str(out)])
        assert seal.main() == 0
        return out

    def test_writes_the_live_payload(self, monkeypatch, tmp_path):
        out = self.run(monkeypatch, tmp_path, json.dumps(seal.seal(PAYLOAD, *LOGIN)))
        assert out.read_bytes() == PAYLOAD

    @pytest.mark.parametrize("text", [None, "not json", json.dumps({"v": 1})])
    def test_an_unreadable_live_payload_writes_nothing(self, monkeypatch, tmp_path, text):
        # payload_changed.py then counts it as changed, and the site is redeployed.
        assert not self.run(monkeypatch, tmp_path, text).exists()
