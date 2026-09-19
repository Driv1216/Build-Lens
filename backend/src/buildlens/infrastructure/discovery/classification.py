"""Name-based, deterministic Stage 2 classification."""

from pathlib import PurePosixPath

_EXACT = {"dockerfile": "Dockerfile", "makefile": "Makefile", "rakefile": "Ruby", "gemfile": "Ruby"}
_SUFFIXES = {
    ".d.ts": "TypeScript",
    ".test.tsx": "TypeScript",
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".md": "Markdown",
    ".rst": "reStructuredText",
}
_BINARY = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".mp3",
    ".mp4",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
    ".sqlite",
    ".db",
    ".exe",
    ".so",
    ".dylib",
    ".class",
}
_MANIFESTS = {
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "poetry.lock",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "requirements.txt",
}


def classify(locator: str) -> tuple[str | None, tuple[str, ...], str]:
    path = PurePosixPath(locator)
    name = path.name.lower()
    suffixes = "".join(path.suffixes).lower()
    language = _EXACT.get(name)
    if language is None:
        for suffix, candidate in sorted(
            _SUFFIXES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if suffixes.endswith(suffix):
                language = candidate
                break
    tags: set[str] = set()
    components = {part.lower() for part in path.parts}
    if language and language not in {"JSON", "YAML", "TOML", "XML", "Markdown", "reStructuredText"}:
        tags.add("source")
    if (
        "test" in components
        or "tests" in components
        or "__tests__" in components
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
        or ".test." in name
        or ".spec." in name
    ):
        tags.update(("source", "test"))
    if (
        name.startswith("readme")
        or "docs" in components
        or language in {"Markdown", "reStructuredText"}
    ):
        tags.add("documentation")
    if name in _MANIFESTS:
        tags.update(("configuration", "manifest"))
    if name.endswith((".lock", ".min.js")) or name == "package-lock.json":
        tags.add("generated")
    if "migration" in components or "migrations" in components:
        tags.add("migration")
    if not tags:
        tags.add("unknown")
    hint = (
        "binary"
        if any(suffixes.endswith(suffix) for suffix in _BINARY)
        else ("text" if language else "unknown")
    )
    return language, tuple(sorted(tags)), hint
