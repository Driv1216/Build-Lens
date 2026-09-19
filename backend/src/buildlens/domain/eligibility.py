"""Small, explicit Stage 2 inventory policy."""

from pathlib import PurePosixPath

POLICY_VERSION = "stage2.v2"

_DIRECTORY_EXCLUSIONS = {
    ".git": "vcs_metadata",
    ".hg": "vcs_metadata",
    ".svn": "vcs_metadata",
    "node_modules": "dependency_directory",
    ".venv": "dependency_directory",
    "venv": "dependency_directory",
    "vendor": "dependency_directory",
    "bower_components": "dependency_directory",
    "__pypackages__": "dependency_directory",
    "build": "build_output",
    "dist": "build_output",
    "target": "build_output",
    "out": "build_output",
    ".next": "build_output",
    ".nuxt": "build_output",
    "coverage": "build_output",
    "htmlcov": "build_output",
    "__pycache__": "cache_or_runtime",
    ".pytest_cache": "cache_or_runtime",
    ".mypy_cache": "cache_or_runtime",
    ".ruff_cache": "cache_or_runtime",
    ".cache": "cache_or_runtime",
    ".tox": "cache_or_runtime",
    ".nox": "cache_or_runtime",
    ".turbo": "cache_or_runtime",
    ".gradle": "cache_or_runtime",
}

_SENSITIVE_NAMES = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".jks", ".keystore")


def exclusion_reason(locator: str, is_directory: bool) -> str | None:
    """Return a policy reason based solely on the observed name/path."""
    parts = PurePosixPath(locator).parts
    # A sensitive/excluded ancestor makes every descendant ineligible.  This
    # matters both while walking and when the secure reader is asked to open a
    # saved locator after a repository changes under it.
    for part in parts:
        name = part.lower()
        if name == ".env" or name.startswith(".env."):
            return "secret_sensitive"
        if (
            name in _SENSITIVE_NAMES
            or name.startswith("credentials.")
            or name.startswith("secrets.")
            or name.endswith(_SENSITIVE_SUFFIXES)
        ):
            return "secret_sensitive"
        if name in _DIRECTORY_EXCLUSIONS:
            return _DIRECTORY_EXCLUSIONS[name]
    name = PurePosixPath(locator).name.lower()
    if name == ".ds_store" or name.endswith((".swp", ".swo", "~")):
        return "cache_or_runtime"
    return None
