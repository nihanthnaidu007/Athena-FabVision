"""CI definition checks: the workflow files parse and cover the required
gates. These tests keep the pipeline itself from drifting -- a broken edit
to ci.yml fails here before it fails on GitHub."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / '.github' / 'workflows'

REQUIRED_CI_JOBS = {'lint', 'test', 'pip-audit', 'gitleaks', 'docker'}


def _load_workflow(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f'{name} is missing'
    parsed = yaml.safe_load(path.read_text())
    assert isinstance(parsed, dict), f'{name} did not parse to a mapping'
    return parsed


def _triggers(parsed: dict) -> dict:
    # PyYAML parses an unquoted `on:` key as boolean True.
    return parsed.get('on') or parsed.get(True) or {}


def test_ci_workflow_parses():
    parsed = _load_workflow('ci.yml')
    assert parsed.get('jobs')


def test_ci_workflow_has_required_gates():
    jobs = _load_workflow('ci.yml').get('jobs', {})
    assert REQUIRED_CI_JOBS <= set(jobs)


def test_ci_workflow_runs_on_prs_and_release_branches():
    triggers = _triggers(_load_workflow('ci.yml'))
    assert 'pull_request' in triggers
    branches = triggers['push']['branches']
    assert 'main' in branches
    assert any('release' in str(branch) for branch in branches)


def test_gitleaks_scans_pushes_to_main_and_release():
    triggers = _triggers(_load_workflow('gitleaks.yml'))
    branches = triggers['push']['branches']
    assert 'main' in branches
    assert any('release' in str(branch) for branch in branches)
