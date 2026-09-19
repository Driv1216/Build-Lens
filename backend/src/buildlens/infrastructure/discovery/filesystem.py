"""Descriptor-relative inventory and bounded reads of ignore-control files only."""

from __future__ import annotations

import os
import stat
import time
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

from buildlens.domain.inspection import ArtifactObservation as ArtifactObservation
from buildlens.domain.inspection import InspectionError as InspectionError
from buildlens.domain.inspection import InspectionResult as InspectionResult
from buildlens.domain.repository_snapshot import (
    ArtifactMetadata,
    ContentHint,
    GitMetadata,
    SnapshotSummary,
    SnapshotWarning,
)
from buildlens.infrastructure.discovery.classification import classify
from buildlens.infrastructure.discovery.git import git_output
from buildlens.infrastructure.discovery.policy import POLICY_VERSION, exclusion_reason
from buildlens.infrastructure.structural.reader import (
    PosixSecureSourceReader,
    SourceReadError,
    directory_at,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _git(root: str, arguments: list[str], timeout: int) -> str | None:
    result = git_output(root, arguments, timeout)
    return result.strip() if result is not None else None


def _git_metadata(root: str, timeout: int) -> tuple[GitMetadata, list[SnapshotWarning]]:
    observed = _now()
    top = _git(root, ["rev-parse", "--show-toplevel"], timeout)
    if top is None:
        return GitMetadata("not_repository", None, None, None, None, None, observed), []
    branch = _git(root, ["symbolic-ref", "--short", "HEAD"], timeout)
    head = _git(root, ["rev-parse", "HEAD"], timeout)
    dirty_output = _git(
        root,
        ["status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=all", "--", "."],
        timeout,
    )
    if dirty_output is None:
        warning = SnapshotWarning("GIT_UNAVAILABLE", "Git status could not be observed.")
        metadata = GitMetadata(
            "unavailable",
            top,
            os.path.relpath(root, top) if root != top else "",
            branch,
            head,
            None,
            observed,
        )
        return metadata, [warning]
    prefix = os.path.relpath(root, top)
    return GitMetadata(
        "available",
        top,
        "" if prefix == "." else prefix.replace(os.sep, "/"),
        branch,
        head,
        bool(dirty_output),
        observed,
    ), []


class LocalRepositoryInspector:
    """Filesystem implementation of the repository-inspection port."""

    def __init__(
        self,
        *,
        max_entries: int = 100_000,
        max_depth: int = 64,
        timeout_seconds: int = 60,
        git_timeout_seconds: int = 10,
    ) -> None:
        self._max_entries = max_entries
        self._max_depth = max_depth
        self._timeout_seconds = timeout_seconds
        self._git_timeout_seconds = git_timeout_seconds

    def inspect(
        self, root_path: str, previous_canonical_root: str | None = None
    ) -> InspectionResult:
        started = _now()
        deadline = time.monotonic() + self._timeout_seconds
        canonical = self._resolve_root(root_path)
        if previous_canonical_root is not None and canonical != previous_canonical_root:
            raise InspectionError(
                "ROOT_TARGET_CHANGED",
                "The registered root now resolves to a different directory.",
                409,
            )
        with ExitStack() as resources:
            root_fd = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            resources.callback(os.close, root_fd)
            root_stat = os.fstat(root_fd)
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
            git_metadata, warnings = _git_metadata(canonical, self._git_timeout_seconds)
            observations: list[ArtifactObservation] = []
            stack: list[tuple[str, str, int, int]] = [(canonical, "", 0, root_stat.st_dev)]
            while stack:
                directory, prefix, depth, root_device = stack.pop()
                if time.monotonic() > deadline:
                    raise InspectionError(
                        "SCAN_TIMEOUT", "Repository inspection exceeded its time limit.", 504
                    )
                with ExitStack() as directory_resources:
                    try:
                        directory_fd = directory_at(root_fd, prefix)
                        directory_resources.callback(os.close, directory_fd)
                        with os.scandir(directory_fd) as iterator:
                            entries: list[os.DirEntry[str]] = []
                            for entry in iterator:
                                if len(entries) + len(observations) >= self._max_entries:
                                    raise InspectionError(
                                        "SCAN_LIMIT_EXCEEDED", "Repository entry limit exceeded."
                                    )
                                entries.append(entry)
                            entries.sort(key=lambda entry: entry.name)
                    except (OSError, SourceReadError) as error:
                        if not prefix:
                            raise InspectionError(
                                "ROOT_UNREADABLE", "The registered root cannot be read."
                            ) from error
                        warnings.append(
                            SnapshotWarning(
                                "DIRECTORY_UNREADABLE", "Directory could not be enumerated.", prefix
                            )
                        )
                        continue
                    for entry in entries:
                        if time.monotonic() > deadline:
                            raise InspectionError(
                                "SCAN_TIMEOUT",
                                "Repository inspection exceeded its time limit.",
                                504,
                            )
                        if len(observations) >= self._max_entries:
                            raise InspectionError(
                                "SCAN_LIMIT_EXCEEDED",
                                "Repository inspection exceeded its entry limit.",
                            )
                        locator = str(PurePosixPath(prefix, entry.name)) if prefix else entry.name
                        if len(locator.encode("utf-8")) > 2048:
                            raise InspectionError(
                                "SCAN_LIMIT_EXCEEDED",
                                "A repository path exceeds the locator limit.",
                            )
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            observations.append(
                                self._excluded(
                                    locator, "unknown", "observation_unavailable", pruned=False
                                )
                            )
                            warnings.append(
                                SnapshotWarning(
                                    "ENTRY_UNAVAILABLE",
                                    "Entry vanished or could not be inspected.",
                                    locator,
                                )
                            )
                            continue
                        kind = self._kind(info.st_mode)
                        is_directory = kind == "directory"
                        reason = exclusion_reason(locator, is_directory)
                        if kind == "symlink":
                            observations.append(
                                self._excluded(locator, kind, "symlink_not_followed", pruned=False)
                            )
                            continue
                        if reason:
                            observations.append(
                                self._excluded(locator, kind, reason, pruned=is_directory)
                            )
                            continue
                        if is_directory:
                            if depth + 1 > self._max_depth:
                                observations.append(
                                    self._included(locator, kind, None, (), "unknown", pruned=True)
                                )
                                warnings.append(
                                    SnapshotWarning(
                                        "DEPTH_LIMIT_REACHED",
                                        "Directory descendants were not enumerated.",
                                        locator,
                                    )
                                )
                            elif info.st_dev != root_device:
                                observations.append(
                                    self._excluded(locator, kind, "mount_boundary", pruned=True)
                                )
                                warnings.append(
                                    SnapshotWarning(
                                        "MOUNT_BOUNDARY",
                                        "Directory descendants were not enumerated across a "
                                        "filesystem boundary.",
                                        locator,
                                    )
                                )
                            else:
                                observations.append(
                                    self._included(locator, kind, None, (), "unknown", pruned=False)
                                )
                                stack.append((locator, locator, depth + 1, root_device))
                        elif kind == "file":
                            language, tags, hint = classify(locator)
                            observations.append(
                                self._included(
                                    locator,
                                    kind,
                                    info.st_size,
                                    tags,
                                    cast(ContentHint, hint),
                                    language,
                                )
                            )
                        else:
                            observations.append(
                                self._included(locator, kind, None, (), "unknown", None)
                            )
            observations, ignore_warnings = self._apply_ignore_rules(
                canonical, observations, git_metadata
            )
            warnings.extend(ignore_warnings)
            # Completion includes eligibility processing.  A snapshot timestamp
            # must not suggest that a later ignore decision was observed earlier.
            completed = _now()
            summary = self._summary(observations, warnings)
            return InspectionResult(
                canonical,
                started,
                completed,
                "partial" if warnings else "complete",
                POLICY_VERSION,
                git_metadata,
                tuple(warnings[:100]),
                summary,
                tuple(sorted(observations, key=lambda item: item.locator)),
                {"device": root_stat.st_dev, "inode": root_stat.st_ino},
            )

    def _resolve_root(self, root_path: str) -> str:
        if root_path == "~" or root_path.startswith("~/"):
            root_path = os.path.expanduser(root_path)
        elif root_path.startswith("~"):
            raise InspectionError(
                "ROOT_PATH_NOT_ABSOLUTE", "Only the current user's home shorthand is supported."
            )
        if not os.path.isabs(root_path):
            raise InspectionError(
                "ROOT_PATH_NOT_ABSOLUTE", "The registered root path must be absolute."
            )
        canonical = os.path.realpath(root_path)
        if not os.path.exists(canonical):
            raise InspectionError("ROOT_NOT_FOUND", "The registered root does not exist.")
        if not os.path.isdir(canonical):
            raise InspectionError("ROOT_NOT_DIRECTORY", "The registered root resolves to a file.")
        if canonical in {os.path.sep, os.path.expanduser("~")}:
            raise InspectionError(
                "ROOT_DISALLOWED", "Filesystem and home-directory roots cannot be inspected."
            )
        if exclusion_reason(canonical, True):
            raise InspectionError(
                "ROOT_DISALLOWED", "The registered root is excluded by the inventory policy."
            )
        return canonical

    @staticmethod
    def _kind(mode: int) -> str:
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISDIR(mode):
            return "directory"
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
            return "special"
        return "unknown"

    @staticmethod
    def _included(
        locator: str,
        kind: str,
        size: int | None,
        tags: tuple[str, ...],
        hint: ContentHint,
        language: str | None = None,
        pruned: bool = False,
    ) -> ArtifactObservation:
        return ArtifactObservation(
            kind,
            locator,
            ArtifactMetadata("included", size, language, tags, hint, "not_ignored", (), pruned),
        )

    @staticmethod
    def _excluded(locator: str, kind: str, reason: str, pruned: bool) -> ArtifactObservation:
        return ArtifactObservation(
            kind,
            locator,
            ArtifactMetadata("excluded", None, None, (), "unknown", "unknown", (reason,), pruned),
        )

    @staticmethod
    def _summary(
        items: list[ArtifactObservation], warnings: list[SnapshotWarning]
    ) -> SnapshotSummary:
        by_kind: dict[str, int] = {}
        languages: dict[str, int] = {}
        tags: dict[str, int] = {}
        reasons: dict[str, int] = {}
        included_files = excluded_files = pruned = included_bytes = 0
        for item in items:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
            metadata = item.metadata
            if metadata.pruned:
                pruned += 1
            for reason in metadata.exclusion_reasons:
                reasons[reason] = reasons.get(reason, 0) + 1
            if item.kind == "file" and metadata.inventory_status == "included":
                included_files += 1
                included_bytes += metadata.size_bytes or 0
                if metadata.language:
                    languages[metadata.language] = languages.get(metadata.language, 0) + 1
                for tag in metadata.tags:
                    tags[tag] = tags.get(tag, 0) + 1
            elif item.kind == "file":
                excluded_files += 1
        return SnapshotSummary(
            by_kind,
            included_files,
            excluded_files,
            pruned,
            included_bytes,
            languages,
            tags,
            reasons,
            len(warnings),
        )

    def _apply_ignore_rules(
        self, root: str, items: list[ArtifactObservation], git: GitMetadata
    ) -> tuple[list[ArtifactObservation], list[SnapshotWarning]]:
        """Apply captured Git-compatible ignore rules.

        Ignore control files are the only project files this adapter may read.
        Exclusions decided before this method always win.
        """
        ignored: set[str] = set()
        warnings: list[SnapshotWarning] = []
        from pathspec import GitIgnoreSpec

        # All control reads share the same pinned-root, no-follow contract.
        controls = {".gitignore"}
        controls.update(
            item.locator for item in items if PurePosixPath(item.locator).name == ".gitignore"
        )
        if git.state == "available":
            if git.repository_root != root:
                warnings.append(
                    SnapshotWarning(
                        "IGNORE_RULES_UNREADABLE",
                        "Ignore rules above the registered root cannot be read safely.",
                    )
                )
            else:
                controls.add(".git/info/exclude")
        rules: list[tuple[str, GitIgnoreSpec]] = []
        remaining = 1024 * 1024
        reader = PosixSecureSourceReader()
        for locator in sorted(controls, key=lambda x: (x != ".git/info/exclude", x.count("/"), x)):
            try:
                data = reader.read(
                    root, locator, min(256 * 1024, remaining), self._root_identity
                ).data
                remaining -= len(data)
                base = "" if locator == ".git/info/exclude" else locator.rpartition("/")[0]
                rules.append(
                    (base, GitIgnoreSpec.from_lines(data.decode("utf-8-sig").splitlines()))
                )
            except SourceReadError as error:
                remaining -= error.bytes_consumed
                if error.code != "SOURCE_NOT_FOUND":
                    warnings.append(
                        SnapshotWarning(
                            "IGNORE_RULES_UNREADABLE", "Ignore control evidence is unavailable."
                        )
                    )
            except (ValueError, UnicodeError):
                warnings.append(
                    SnapshotWarning(
                        "IGNORE_RULES_UNREADABLE", "Ignore control evidence is invalid."
                    )
                )
        for item in items:
            parts = item.locator.split("/")
            for depth in range(1, len(parts) + 1):
                prefix = "/".join(parts[:depth])
                directory = depth < len(parts) or item.kind == "directory"
                decision = False
                for base, spec in rules:
                    if base and not prefix.startswith(base + "/"):
                        continue
                    relative = prefix[len(base) + 1 :] if base else prefix
                    match = spec.check_file(relative + ("/" if directory else ""))
                    if match.include is not None:
                        decision = match.include
                if decision:
                    ignored.add(item.locator)
                    break
        unavailable = (
            any(
                warning.code in {"GIT_IGNORE_UNAVAILABLE", "IGNORE_RULES_UNREADABLE"}
                for warning in warnings
            )
            or git.state == "unavailable"
        )
        if unavailable:
            reason = "ignore_eligibility_unavailable"
            return (
                [
                    item
                    if item.metadata.inventory_status == "excluded"
                    else replace(
                        item,
                        metadata=ArtifactMetadata(
                            "excluded",
                            None,
                            None,
                            (),
                            "unknown",
                            "unknown",
                            (*item.metadata.exclusion_reasons, reason),
                            item.metadata.pruned,
                        ),
                    )
                    for item in items
                ],
                warnings,
            )
        if not ignored:
            return items, warnings
        result: list[ArtifactObservation] = []
        for item in items:
            if item.locator not in ignored or item.metadata.inventory_status == "excluded":
                result.append(item)
                continue
            metadata = item.metadata
            result.append(
                replace(
                    item,
                    metadata=ArtifactMetadata(
                        "excluded",
                        None,
                        None,
                        (),
                        "unknown",
                        "ignored",
                        ("git_ignored",),
                        metadata.pruned,
                    ),
                )
            )
        return result, warnings
