"""File classification: what to review, in which order, and which tests belong to which source."""

import json
from pathlib import PurePosixPath

from reviewer.schemas import ChangedFile

LOCKFILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "bun.lockb",
        "poetry.lock",
        "Pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
        "mix.lock",
        "pubspec.lock",
    }
)
# ponytail: directory names are a heuristic; a repo that keeps real code in build/ loses it.
VENDORED_DIRS = frozenset(
    {"vendor", "node_modules", "third_party", "third-party", "bower_components", ".venv", "venv"}
)
GENERATED_DIRS = frozenset({"dist", "build", "generated", "__generated__", ".next", "coverage"})
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", "_pb2.py", "_pb2_grpc.py", ".pb.go", ".snap")
BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svgz", ".pdf", ".zip",
        ".gz", ".tar", ".tgz", ".7z", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".exe",
        ".dll", ".so", ".dylib", ".jar", ".class", ".pyc", ".mp3", ".mp4", ".mov", ".wasm",
    }
)  # fmt: skip
DOC_EXTENSIONS = frozenset({".md", ".rst", ".txt", ".adoc"})
CONFIG_EXTENSIONS = frozenset({".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".xml", ".lock"})
JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec"})


def skip_reason(file: ChangedFile) -> str | None:
    """Why this file is not sent to the reviewer, or None if it should be reviewed."""
    path = PurePosixPath(file.filename)
    name = path.name
    if file.status == "removed":
        return "deleted"
    if VENDORED_DIRS & set(path.parts[:-1]):
        return "vendored"
    if name in LOCKFILES:
        return "lockfile"
    if GENERATED_DIRS & set(path.parts[:-1]) or name.endswith(GENERATED_SUFFIXES):
        return "generated"
    if ".generated." in name:
        return "generated"
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return "binary"
    if not file.patch:
        return "no textual diff (binary, too large or empty)"
    return None


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return (
        name.startswith("test_")
        or p.stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
        or bool(TEST_DIRS & set(p.parts[:-1]))
    )


def is_testable_source(path: str) -> bool:
    suffix = PurePosixPath(path).suffix
    return not is_test_path(path) and (suffix == ".py" or suffix in JS_EXTENSIONS)


def review_priority(path: str) -> int:
    """Lower is reviewed first: source, then tests, then config, then docs."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in DOC_EXTENSIONS or PurePosixPath(path).parts[0] == "docs":
        return 3
    if suffix in CONFIG_EXTENSIONS:
        return 2
    if is_test_path(path):
        return 1
    return 0


def find_test_files(source_path: str, all_paths: list[str], limit: int = 2) -> list[str]:
    """Existing test files for a source file, by naming convention.

    foo.py <-> test_foo.py / foo_test.py; foo.ts <-> foo.test.ts / foo.spec.ts / __tests__/foo.ts.
    When several match (e.g. two `test_utils.py`), the one sharing the most path components wins.
    """
    source = PurePosixPath(source_path)
    stem, suffix = source.stem, source.suffix
    if suffix == ".py":
        names = {f"test_{stem}.py", f"{stem}_test.py"}
    elif suffix in JS_EXTENSIONS:
        names = {f"{stem}.{kind}{ext}" for kind in ("test", "spec") for ext in JS_EXTENSIONS}
    else:
        return []

    source_dirs = set(source.parts[:-1]) - {"src", "lib", "app"}
    matches: list[tuple[int, int, str]] = []
    for candidate in all_paths:
        c = PurePosixPath(candidate)
        in_tests_dir = c.parent.name == "__tests__" and c.stem == stem and c.suffix in JS_EXTENSIONS
        if candidate != source_path and (c.name in names or in_tests_dir):
            shared = len(source_dirs & set(c.parts[:-1]))
            matches.append((-shared, len(candidate), candidate))
    return [path for _, _, path in sorted(matches)[:limit]]


def detect_js_test_framework(package_json: str | None) -> str | None:
    """'vitest' or 'jest' from package.json dependencies, else None."""
    if not package_json:
        return None
    try:
        data = json.loads(package_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps or "@jest/globals" in deps:
        return "jest"
    return None
