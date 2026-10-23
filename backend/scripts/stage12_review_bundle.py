"""Build STAGE12_TECHNICAL_REVIEW.zip deterministically from an explicit allow-list.

Usage (from backend/):
    PYTHONPATH=src python scripts/stage12_review_bundle.py \
        --output ../STAGE12_TECHNICAL_REVIEW.zip --runs <run ids> --notes <dir> \
        --verification <dir> --openapi <openapi.json>

* Requires a clean working tree; files come from the committed HEAD tree (git show).
* Never includes .env, keys, persisted indexes/vectors, sandboxes, model caches, venvs,
  node_modules, or the four separately-supplied documents.
* Fixed timestamps (1980-01-01), permissions and compression; built twice and compared.
* Secret-scanned with BuildLens's own screen plus key patterns; unexpected hits abort.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BASE_REF = "stage12-base"
EXCLUDED_DOCS = {"PROJECT_BLUEPRINT.md", "CURRENT_STATE.md", "README.md", "docs/ARCHITECTURE.md"}
FORBIDDEN = re.compile(
    r"(^|/)(\.env($|\.)(?!example)|node_modules/|\.venv/|__pycache__/|indexes/|sandboxes/|workers/|authorizations/)"
)
MAX_FILE = 12 * 1024 * 1024
CONTEXT_FILES = [
    "backend/src/buildlens/application/retrieval.py",
    "backend/src/buildlens/application/context.py",
    "backend/src/buildlens/application/ask.py",
    "backend/src/buildlens/application/embeddings.py",
    "backend/src/buildlens/application/evaluation.py",
    "backend/src/buildlens/application/evaluation_binding.py",
    "backend/src/buildlens/application/evaluation_metrics.py",
    "backend/src/buildlens/application/observation.py",
    "backend/src/buildlens/application/processing.py",
    "backend/src/buildlens/application/source_ports.py",
    "backend/src/buildlens/domain/source.py",
    "backend/src/buildlens/domain/eligibility.py",
    "backend/src/buildlens/domain/processing.py",
    "backend/src/buildlens/domain/retrieval.py",
    "backend/src/buildlens/domain/context.py",
    "backend/src/buildlens/domain/embeddings.py",
    "backend/src/buildlens/domain/evaluation.py",
    "backend/src/buildlens/infrastructure/evaluation/corpus.py",
    "backend/src/buildlens/infrastructure/evaluation/local.py",
    "backend/src/buildlens/infrastructure/evaluation/report.py",
    "backend/src/buildlens/infrastructure/embeddings/supervisor.py",
    "backend/src/buildlens/infrastructure/reranking/supervisor.py",
    "backend/src/buildlens/infrastructure/structural/reader.py",
    "backend/src/buildlens/infrastructure/structural/supervisor.py",
    "backend/src/buildlens/infrastructure/discovery/filesystem.py",
    "backend/src/buildlens/infrastructure/persistence/models.py",
    "backend/src/buildlens/infrastructure/persistence/retrieval.py",
    "backend/src/buildlens/cli/evaluation.py",
    "backend/tests/conftest.py",
    "backend/alembic.ini",
    ".github/workflows/verify.yml",
    ".env.example",
    ".gitignore",
    "compose.yaml",
    "docs/EVALUATION.md",
]
DATASET_FILES = [
    ".buildlens-eval/datasets/buildlens-v4.r1-reviewed.json",
    ".buildlens-eval/datasets/buildlens-v4.r1-revision.json",
    ".buildlens-eval/datasets/corpus-20261002.json",
    ".buildlens-eval/datasets/README.md",
]
PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=]{40,}"),
    "pg_url_with_password": re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s/@'\"]+:[^@\s/'\"]{6,}@"),
}
# (path suffix, pattern) pairs that are reviewed false positives; every use is recorded.
KNOWN_FALSE_POSITIVES = {
    (".env.example", "pg_url_with_password"),  # placeholder password text
}


def git(*args: str, binary: bool = False) -> Any:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode()


def head_file(path: str) -> bytes | None:
    try:
        return git("show", f"HEAD:{path}", binary=True)
    except subprocess.CalledProcessError:
        return None


def category(path: str, changed: set[str]) -> str:
    if path.startswith("review/"):
        return "review"
    if path.startswith("verification/"):
        return "verification"
    if path.startswith(".buildlens-eval/lab/runs/") or path.startswith(".buildlens-eval/runs/"):
        return "result"
    if path.startswith(".buildlens-eval/"):
        return "dataset-or-config"
    if "/tests/" in path or path.startswith("backend/tests") or path.endswith(".test.tsx"):
        return "test"
    if path.endswith(("pyproject.toml", "uv.lock", "package.json", "package-lock.json")):
        return "dependency-manifest"
    if path.startswith("backend/scripts/"):
        return "script"
    return "new-or-modified" if path in changed else "context"


def scan(path: str, data: bytes) -> list[dict[str, str]]:
    sys.path.insert(0, str(REPO / "backend" / "src"))
    from buildlens.domain.source import ContentRejectedError, screen_content

    hits = []
    text = data.decode("utf-8", "ignore")
    try:
        screen_content(text)
    except ContentRejectedError as error:
        hits.append({"path": path, "rule": f"buildlens-screen:{error.code}"})
    for name, pattern in PATTERNS.items():
        if pattern.search(text):
            hits.append({"path": path, "rule": name})
    return hits


def collect(args: argparse.Namespace) -> tuple[dict[str, bytes], dict[str, str], dict[str, Any]]:
    if git("status", "--porcelain").strip():
        raise SystemExit("working tree must be clean (bundle reads committed HEAD)")
    base = git("rev-parse", BASE_REF).strip()
    head = git("rev-parse", "HEAD").strip()
    changed = {p for p in git("diff", "--name-only", f"{BASE_REF}..HEAD").split("\n") if p}
    files: dict[str, bytes] = {}
    notes: dict[str, str] = {}
    for path in sorted(changed | set(CONTEXT_FILES) | set(DATASET_FILES)):
        if path in EXCLUDED_DOCS or FORBIDDEN.search(path):
            continue
        data = head_file(path)
        if data is None:
            notes[path] = "deleted in HEAD (see patch)"
            continue
        files[path] = data
    for test in git("ls-files", "backend/tests").split("\n"):
        if test and test not in files and not FORBIDDEN.search(test):
            data = head_file(test)
            if data is not None:
                files[test] = data
    # Retained (git-ignored) result artifacts: read from disk, never indexes or sandboxes.
    for run in args.runs:
        root = REPO / ".buildlens-eval" / "lab" / "runs" / run
        for file in sorted(root.rglob("*")):
            relative = file.relative_to(REPO).as_posix()
            if file.is_file() and not FORBIDDEN.search(relative):
                files[relative] = file.read_bytes()
    for extra in args.extra:
        root = REPO / extra
        for file in sorted([root] if root.is_file() else root.rglob("*")):
            relative = file.relative_to(REPO).as_posix()
            if file.is_file() and not FORBIDDEN.search(relative):
                files[relative] = file.read_bytes()
    notes_dir = Path(args.notes)
    for file in sorted(notes_dir.rglob("*")):
        if file.is_file():
            files["review/" + file.relative_to(notes_dir).as_posix()] = file.read_bytes()
    verification_dir = Path(args.verification)
    for file in sorted(verification_dir.rglob("*")):
        if file.is_file():
            files["verification/" + file.relative_to(verification_dir).as_posix()] = (
                file.read_bytes()
            )
    # Generated review material.
    pathspec = ["--", ".", *[f":(exclude){p}" for p in sorted(EXCLUDED_DOCS)]]
    files["review/stage12.patch"] = git(
        "diff", "--binary", f"{BASE_REF}..HEAD", *pathspec, binary=True
    )
    series = git("format-patch", "--stdout", f"{BASE_REF}..HEAD", *pathspec, binary=True)
    files["review/patches/stage12-series.mbox"] = series
    stat = git("diff", "--stat=200", f"{BASE_REF}..HEAD", *pathspec)
    numstat = git("diff", "--numstat", f"{BASE_REF}..HEAD", *pathspec)
    files["review/CHANGED_FILES.md"] = (
        "# Changed files (stage12-base..HEAD)\n\nExcluded (supplied separately): "
        + ", ".join(sorted(EXCLUDED_DOCS))
        + "\n\n```\n"
        + stat
        + "```\n\n## numstat (added, removed, path)\n\n```\n"
        + numstat
        + "```\n"
    ).encode()
    tree = git("ls-files")
    files["review/REPO_TREE.txt"] = tree.encode()
    files["review/MIGRATIONS.md"] = (
        "# Database migrations\n\nStage 12 adds **no migrations** and no production tables. "
        "Existing migrations (reference only, unchanged):\n\n"
        + "\n".join(
            f"- `{p}`"
            for p in sorted(git("ls-files", "backend/alembic/versions").split())
            if p.endswith(".py")
        )
        + "\n"
    ).encode()
    for name, value in args.generated.items():
        files[name] = value
    environment = {
        "base_ref": BASE_REF,
        "base_commit": base,
        "head_commit": head,
        "stage11_closed_commit": git("rev-parse", "stage11-closed").strip(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "uv_lock_sha256": hashlib.sha256(files["backend/uv.lock"]).hexdigest(),
        "isolated_lock_sha256": hashlib.sha256(
            files["backend/lab_envs/langchain_postgres/uv.lock"]
        ).hexdigest(),
        "package_lock_sha256": hashlib.sha256(files["frontend/package-lock.json"]).hexdigest(),
    }
    return files, notes, {"environment": environment, "changed": sorted(changed)}


def build_zip(files: dict[str, bytes], manifest: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entries = {
            **files,
            "MANIFEST.json": json.dumps(manifest, indent=2, sort_keys=True).encode(),
        }
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(info, entries[name], compresslevel=9)
    return buffer.getvalue()


VERIFY = '''"""Re-check every file hash listed in MANIFEST.json after extracting the bundle."""
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
manifest = json.loads((root / "MANIFEST.json").read_text())
bad = [
    f["path"]
    for f in manifest["files"]
    if hashlib.sha256((root / f["path"]).read_bytes()).hexdigest() != f["sha256"]
]
print(f"{len(manifest['files'])} files checked, {len(bad)} mismatches")
sys.exit(1 if bad else 0)
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runs", nargs="*", default=[])
    parser.add_argument("--extra", nargs="*", default=[])
    parser.add_argument("--notes", required=True)
    parser.add_argument("--verification", required=True)
    parser.add_argument("--openapi", required=True, type=Path)
    args = parser.parse_args()
    args.generated = {
        "review/openapi.json": args.openapi.read_bytes(),
        "verify_bundle.py": VERIFY.encode(),
    }
    files, notes, info = collect(args)
    hits, substituted = [], {}
    for path in list(files):
        data = files[path]
        if len(data) > MAX_FILE:
            substituted[path] = (
                f"omitted: {len(data)} bytes > {MAX_FILE} cap; "
                f"sha256 {hashlib.sha256(data).hexdigest()}"
            )
            del files[path]
            continue
        for hit in scan(path, data):
            known = any(path.endswith(s) and hit["rule"] == r for s, r in KNOWN_FALSE_POSITIVES)
            hits.append({**hit, "reviewed_false_positive": known})
    unexpected = [h for h in hits if not h["reviewed_false_positive"]]
    files["verification/bundle-secret-scan.json"] = json.dumps(
        {"hits": hits, "unexpected": unexpected}, indent=2
    ).encode()
    if unexpected:
        print(json.dumps(unexpected, indent=2))
        raise SystemExit("secret scan found unexpected hits; bundle not written")
    entries = [
        {
            "path": p,
            "sha256": hashlib.sha256(d).hexdigest(),
            "size": len(d),
            "category": category(p, set(info["changed"])),
        }
        for p, d in sorted(files.items())
    ]
    manifest = {
        "version": "stage12.review-bundle.v1",
        **info["environment"],
        "excluded_documents_supplied_separately": sorted(EXCLUDED_DOCS),
        "exclusions": (
            "secrets, .env, credentials, persisted lab indexes/vectors, sandboxes, "
            "model caches/weights, venvs, node_modules, caches"
        ),
        "omitted_or_substituted": {**notes, **substituted},
        "files": entries,
    }
    first = build_zip(files, manifest)
    second = build_zip(files, manifest)
    if first != second:
        raise SystemExit("non-deterministic archive")
    args.output.write_bytes(first)
    digest = hashlib.sha256(first).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "files": len(entries) + 1,
                "bytes": len(first),
                "sha256": digest,
                "deterministic_rebuild_identical": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
