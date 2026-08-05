from faraday_review.deterministic.protected_paths import PathClassifier

CLASSES = {
    "red": [".github/**", "tools/guards/**", "review-policy/**"],
    "yellow": ["src/**", "package.json", "uv.lock"],
    "green": ["docs/**", "*.md", "**/*.md", "tests/**"],
}


def _classifier() -> PathClassifier:
    return PathClassifier(classes=CLASSES, default_class="yellow")


def test_docs_green():
    assert _classifier().classify_path("docs/RUNBOOK.md")[0] == "green"
    assert _classifier().classify_path("README.md")[0] == "green"


def test_workflow_red_beats_green_md():
    # .github/workflows/README.md matches both red and green: max wins.
    assert _classifier().classify_path(".github/workflows/README.md")[0] == "red"


def test_unmatched_defaults_yellow_not_green():
    assert _classifier().classify_path("Makefile")[0] == "yellow"


def test_double_star_crosses_directories_single_star_does_not():
    assert _classifier().classify_path("src/a/b/c.py")[0] == "yellow"
    cls = PathClassifier(classes={"red": ["a/*.py"]}, default_class="yellow")
    assert cls.classify_path("a/b/c.py")[0] == "yellow"
    assert cls.classify_path("a/c.py")[0] == "red"


def test_overall_is_max_and_findings_sorted():
    overall, findings = _classifier().classify(
        ["docs/x.md", "src/app.py", ".github/workflows/ci.yml"]
    )
    assert overall == "red"
    paths = [f.path for f in findings]
    assert paths == sorted(paths)
    assert all(f.check == "protected_paths" for f in findings)
