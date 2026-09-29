"""Guards keeping the Buildkite token scoped and out of the repository.

The token is a repo secret injected as step-scoped env on the single step that
needs it. These tests fail if a change would widen where it can be used or let
one land in the tree.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

import perf_eval

ROOT = Path(perf_eval.__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"

TOKEN_NAMES = ("BUILDKITE_TOKEN", "BUILDKITE_API_TOKEN")
# The only modules permitted to read the Buildkite token. Adding one widens
# where a credential can reach, so it should be a deliberate decision.
TOKEN_ENTRYPOINTS = {"perf_eval/collect_artifacts.py"}
# Read-only by construction: these must never issue a write.
READ_ONLY_MODULES = ("perf_eval/collect_artifacts.py",)


class TestOrgIsPinned:
    def test_buildkite_org_is_vllm(self):
        assert perf_eval.BUILDKITE_ORG == "vllm"

    def test_pipeline_slug_is_perf_eval(self):
        assert perf_eval.BUILDKITE_PIPELINE_SLUG == "perf-eval"

    def test_every_buildkite_url_interpolates_the_pinned_org(self):
        # A pinned org is what keeps the token from being pointed at an
        # unrelated Buildkite organization.
        pattern = re.compile(r"organizations/\{(\w+)\}")
        for path in SCRIPTS.rglob("*.py"):
            for name in pattern.findall(path.read_text(encoding="utf-8")):
                assert name == "BUILDKITE_ORG", f"{path}: org comes from {name}"


class TestTokenReach:
    def test_only_the_artifact_collector_reads_the_token(self):
        offenders = set()
        for path in SCRIPTS.rglob("*.py"):
            rel = path.relative_to(SCRIPTS).as_posix()
            source = path.read_text(encoding="utf-8")
            if any(name in source for name in TOKEN_NAMES) and rel not in TOKEN_ENTRYPOINTS:
                offenders.add(rel)
        assert offenders == set(), f"unexpected Buildkite token references: {offenders}"

    @pytest.mark.parametrize("module", READ_ONLY_MODULES)
    def test_token_holders_only_issue_reads(self, module):
        source = (SCRIPTS / module).read_text(encoding="utf-8")
        for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
            assert verb not in source, f"{module}: {verb} would need a write-scoped token"

    @pytest.mark.parametrize("module", READ_ONLY_MODULES)
    def test_token_holders_fail_closed_without_a_token(self, module):
        source = (SCRIPTS / module).read_text(encoding="utf-8")
        assert 'os.getenv("BUILDKITE_TOKEN")' in source, module
        assert "BUILDKITE_TOKEN not set" in source, module

    @pytest.mark.parametrize("module", READ_ONLY_MODULES)
    def test_token_holders_never_disable_tls_verification(self, module):
        """A TLS-inspecting proxy is fixed by trusting the OS store.

        Never by turning verification off in a process holding a credential.
        Checked against the parsed syntax tree rather than the text, so a
        comment or docstring *naming* the anti-pattern does not trip it.
        """
        tree = ast.parse((SCRIPTS / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                disabled = (
                    keyword.arg == "verify"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                )
                assert not disabled, f"{module} disables TLS verification"

    @pytest.mark.parametrize("module", READ_ONLY_MODULES)
    def test_token_holders_trust_the_os_certificate_store(self, module):
        source = (SCRIPTS / module).read_text(encoding="utf-8")
        assert "use_system_certificates()" in source, module

    @pytest.mark.parametrize("module", ["aggregate.py", "merge_events.py", "normalize.py"])
    def test_offline_modules_cannot_reach_the_network(self, module):
        source = (SCRIPTS / "perf_eval" / module).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import requests|from requests)", source, re.M), module
        assert not re.search(r"^\s*(?:import urllib|from urllib)", source, re.M), module
        assert "api.buildkite.com" not in source, module


@pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="workflows not present")
class TestWorkflowTokenHandling:
    def _workflow_text(self, name):
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_no_workflow_contains_a_literal_token(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for name in TOKEN_NAMES:
                for line in text.splitlines():
                    if f"{name}:" not in line:
                        continue
                    # Every assignment must reference the secrets context.
                    assert "secrets." in line, f"{path.name}: {line.strip()}"

    def test_the_token_is_scoped_to_the_ingest_step_only(self):
        text = self._workflow_text("collect-and-deploy.yml")
        # Exactly one step may receive it, and nothing above `steps:` may,
        # since a workflow- or job-level env block reaches every step.
        assert text.count("BUILDKITE_TOKEN: ${{ secrets.BUILDKITE_TOKEN }}") == 1
        preamble = text.split("steps:")[0]
        assert "BUILDKITE_TOKEN" not in preamble

    def test_ci_workflow_never_receives_the_token(self):
        for name in ("lint-and-test.yml", "secrets-scan.yml"):
            text = self._workflow_text(name)
            for token in TOKEN_NAMES:
                assert token not in text, f"{name} must not receive {token}"

    def test_no_checkout_persists_credentials(self):
        # A persisted token sits in .git/config where every later step can
        # read it; the one step that pushes supplies it for that push only.
        for path in WORKFLOWS.glob("*.yml"):
            for job in yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"].values():
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        with_ = step.get("with") or {}
                        assert with_.get("persist-credentials") is False, (
                            f"{path.name}: {step.get('name')}"
                        )

    def test_the_dashboard_login_reaches_only_the_steps_that_seal_or_open(self):
        workflow = yaml.safe_load(self._workflow_text("collect-and-deploy.yml"))
        text = self._workflow_text("collect-and-deploy.yml")
        assert "DASHBOARD_PASSWORD" not in text.split("steps:")[0]
        steps = workflow["jobs"]["collect"]["steps"]
        holders = [s["name"] for s in steps if "DASHBOARD_PASSWORD" in (s.get("env") or {})]
        assert holders == ["Fetch the published payload", "Build the site"]

    def test_the_store_token_reaches_only_the_store(self):
        workflow = yaml.safe_load(self._workflow_text("collect-and-deploy.yml"))
        steps = workflow["jobs"]["collect"]["steps"]
        holders = [s["name"] for s in steps if "STATE_REPO_TOKEN" in str(s)]
        assert holders == ["Check out the event store", "Persist the event store"]

    def test_ci_never_receives_the_login_or_the_store_token(self):
        for name in ("lint-and-test.yml", "secrets-scan.yml"):
            text = self._workflow_text(name)
            for secret in ("DASHBOARD_PASSWORD", "DASHBOARD_USERNAME", "STATE_REPO_TOKEN"):
                assert secret not in text, f"{name} must not receive {secret}"

    def test_only_the_persist_step_pushes_with_the_token(self):
        workflow = yaml.safe_load(self._workflow_text("collect-and-deploy.yml"))
        steps = workflow["jobs"]["collect"]["steps"]
        pushers = [step["name"] for step in steps if "extraheader" in step.get("run", "")]
        assert pushers == ["Persist the event store"]


@pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="workflows not present")
def test_only_main_saves_the_store_or_deploys():
    # A manual run can pick any branch; unmerged code must not write the
    # shared store or the live site.
    text = (WORKFLOWS / "collect-and-deploy.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    for name in ("Persist the event store", "Upload the site"):
        (step,) = [s for s in jobs["collect"]["steps"] if s.get("name") == name]
        assert "github.ref == 'refs/heads/main'" in str(step.get("if")), name
    assert "github.ref == 'refs/heads/main'" in str(jobs["deploy"].get("if"))


@pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="workflows not present")
def test_only_the_deploy_job_can_publish():
    # Pages and OIDC permissions reach no step that runs collected code.
    text = (WORKFLOWS / "collect-and-deploy.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    assert jobs["collect"]["permissions"] == {"contents": "read"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
