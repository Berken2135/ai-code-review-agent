import pytest

from reviewer.agent.files import (
    detect_js_test_framework,
    find_test_files,
    is_test_path,
    is_testable_source,
    review_priority,
    skip_reason,
)
from tests.fakes import changed_file


@pytest.mark.parametrize(
    ("path", "kwargs", "expected"),
    [
        ("src/app.py", {}, None),
        ("docs/guide.md", {}, None),
        ("package-lock.json", {}, "lockfile"),
        ("frontend/yarn.lock", {}, "lockfile"),
        ("poetry.lock", {}, "lockfile"),
        ("go.sum", {}, "lockfile"),
        ("vendor/lib/thing.py", {}, "vendored"),
        ("web/node_modules/pkg/index.js", {}, "vendored"),
        ("dist/bundle.js", {}, "generated"),
        ("static/app.min.js", {}, "generated"),
        ("api/service_pb2.py", {}, "generated"),
        ("src/schema.generated.ts", {}, "generated"),
        ("__snapshots__/a.test.js.snap", {}, "generated"),
        ("assets/logo.png", {}, "binary"),
        ("fonts/a.woff2", {}, "binary"),
        ("src/old.py", {"status": "removed"}, "deleted"),
        ("src/huge.py", {"patch": None}, "no textual diff (binary, too large or empty)"),
    ],
)
def test_skip_reason(path, kwargs, expected):
    assert skip_reason(changed_file(path, **kwargs)) == expected


def test_source_files_are_reviewed_before_tests_config_and_docs():
    paths = ["README.md", "config.yml", "tests/test_app.py", "src/app.py", "docs/a.rst"]

    ordered = sorted(paths, key=lambda p: (review_priority(p), p))

    assert ordered == ["src/app.py", "tests/test_app.py", "config.yml", "README.md", "docs/a.rst"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("src/test_app.py", True),
        ("src/app_test.py", True),
        ("src/app.test.ts", True),
        ("src/app.spec.tsx", True),
        ("src/__tests__/app.ts", True),
        ("src/app.py", False),
        ("src/contest.py", False),
    ],
)
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


def test_is_testable_source_excludes_tests_docs_and_other_languages():
    assert is_testable_source("src/app.py")
    assert is_testable_source("web/app.tsx")
    assert not is_testable_source("tests/test_app.py")
    assert not is_testable_source("README.md")
    assert not is_testable_source("main.go")


TREE = [
    "src/reviewer/api/health.py",
    "tests/unit/test_health.py",
    "tests/unit/api/test_health.py",
    "tests/unit/test_other.py",
    "web/src/button.ts",
    "web/src/button.test.ts",
    "web/src/card.tsx",
    "web/src/__tests__/card.tsx",
    "web/src/list.js",
    "web/src/list.spec.js",
]


def test_python_source_maps_to_test_prefix_and_suffix_conventions():
    tree = ["tests/test_app.py", "x/app_test.py", "tests/test_b.py"]

    assert set(find_test_files("src/app.py", tree)) == {"tests/test_app.py", "x/app_test.py"}


def test_javascript_conventions():
    assert find_test_files("web/src/button.ts", TREE) == ["web/src/button.test.ts"]
    assert find_test_files("web/src/card.tsx", TREE) == ["web/src/__tests__/card.tsx"]
    assert find_test_files("web/src/list.js", TREE) == ["web/src/list.spec.js"]


def test_ambiguous_matches_prefer_the_test_sharing_more_path_components():
    result = find_test_files("src/reviewer/api/health.py", TREE, limit=1)

    assert result == ["tests/unit/api/test_health.py"]


def test_source_itself_and_unrelated_tests_are_not_matched():
    assert find_test_files("src/reviewer/api/health.py", ["src/reviewer/api/health.py"]) == []
    assert find_test_files("src/foo.py", ["tests/test_bar.py"]) == []


def test_unsupported_language_has_no_test_mapping():
    assert find_test_files("main.go", ["main_test.go"]) == []


@pytest.mark.parametrize(
    ("package_json", "expected"),
    [
        ('{"devDependencies": {"vitest": "^1"}}', "vitest"),
        ('{"devDependencies": {"jest": "^29"}}', "jest"),
        ('{"dependencies": {"vitest": "1"}, "devDependencies": {"jest": "29"}}', "vitest"),
        ('{"dependencies": {"react": "18"}}', None),
        ("not json", None),
        ("[]", None),
        (None, None),
    ],
)
def test_detect_js_test_framework(package_json, expected):
    assert detect_js_test_framework(package_json) == expected
