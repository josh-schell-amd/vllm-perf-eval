#!/usr/bin/env python3
"""Build the deployable site: copy site/ into a fresh _site/ with the payload
sealed, so the public site holds no readable data (scripts/perf_eval/seal.py).

The data fetch in js/app.js, and every local script and stylesheet the page
loads, get a content-hash query, so after a deploy browsers pick up the new
files together instead of pairing a fresh page with stale cached code. The
plain payload and the events.jsonl store are never published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from perf_eval.seal import credentials_from_env, seal  # noqa: E402

DEFAULT_SOURCE = ROOT / "site"
DEFAULT_PAYLOAD = ROOT / "data" / "perf_eval.json"
DEFAULT_OUTPUT = ROOT / "_site"

SEALED_NAME = "perf_eval.sealed.json"
FETCH_CALL = f"fetch('{SEALED_NAME}')"
# The script that loads the payload, relative to the site root.
APP_SCRIPT = "js/app.js"
# A local script or stylesheet in the page: src="js/app.js", href="app.css".
_ASSET_REF = re.compile(r'(?<![\w-])(?P<attr>src|href)="(?P<path>[^":?#]+\.(?:js|css))"')


def _content_tag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _leak_samples(payload: dict) -> list[str]:
    """Strings from the payload that must never appear in the site in the clear."""
    models = [str(m.get("model") or "") for m in payload.get("models") or []]
    return [s for s in [*models, str(payload.get("generated_at") or "")] if s]


def build(
    source_dir: Path, payload_path: Path, output_dir: Path, credentials: tuple[str, str]
) -> Path:
    """Build output_dir from source_dir and the sealed payload; return output_dir.

    Everything is checked before output_dir is touched, so a failed build
    leaves the previous output in place.
    """
    index = source_dir / "index.html"
    app = source_dir / APP_SCRIPT
    if not source_dir.is_dir():
        raise FileNotFoundError(f"site source directory not found: {source_dir}")
    if not index.is_file():
        raise FileNotFoundError(f"site entrypoint not found: {index}")
    if not app.is_file():
        raise FileNotFoundError(f"site script not found: {app}")
    if not payload_path.is_file():
        raise FileNotFoundError(
            f"published payload not found: {payload_path} (run aggregate.py first)"
        )

    payload_bytes = payload_path.read_bytes()
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{payload_path} is not valid JSON ({exc}); the page could not load it, "
            "so nothing was built"
        ) from exc

    script = app.read_text(encoding="utf-8")
    found = script.count(FETCH_CALL)
    if found != 1:
        raise RuntimeError(
            f"{app} must load its data with exactly one {FETCH_CALL} call, so the "
            f"build can add the cache tag; found {found}"
        )
    sealed = json.dumps(seal(payload_bytes, *credentials)).encode()
    tag = _content_tag(sealed)
    script = script.replace(FETCH_CALL, f"fetch('{SEALED_NAME}?v={tag}')")

    html = index.read_text(encoding="utf-8")
    missing = [
        m["path"] for m in _ASSET_REF.finditer(html) if not (source_dir / m["path"]).is_file()
    ]
    if missing:
        raise FileNotFoundError(f"{index} loads files that do not exist: {missing}")
    # Everything published is the source plus the sealed payload, so checking
    # the source is checking the site: no model or timestamp from the data.
    samples = _leak_samples(payload)
    leaks = sorted(
        {
            str(f.relative_to(source_dir))
            for f in source_dir.rglob("*")
            if f.is_file() and any(s.encode() in f.read_bytes() for s in samples)
        }
    )
    if leaks:
        raise RuntimeError(
            f"site files contain data from the payload, refusing to publish: {leaks}"
        )

    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(source_dir, output_dir)
    (output_dir / APP_SCRIPT).write_text(script, encoding="utf-8")
    (output_dir / SEALED_NAME).write_bytes(sealed)

    # Tagged from the output, so app.js's tag follows the payload it now names.
    def tagged(match: re.Match) -> str:
        path = match["path"]
        return f'{match["attr"]}="{path}?v={_content_tag((output_dir / path).read_bytes())}"'

    (output_dir / "index.html").write_text(_ASSET_REF.sub(tagged, html), encoding="utf-8")

    print(f"Built {output_dir} ({len(sealed)} bytes of sealed data, cache tag {tag})")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Site source (site/)")
    parser.add_argument(
        "--payload", type=Path, default=DEFAULT_PAYLOAD, help="Path to perf_eval.json"
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="Where to build the site (_site/)"
    )
    args = parser.parse_args()

    build(args.source, args.payload, args.output, credentials_from_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())
