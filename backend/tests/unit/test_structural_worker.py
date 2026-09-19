from pathlib import Path
from uuid import uuid4

from buildlens.infrastructure.structural.worker import SupervisedPythonAnalyzer


def test_worker_reads_and_parses_without_importing_project_code(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def observed(value: int = 1) -> str:\n    return 'not executed'\n")

    result = SupervisedPythonAnalyzer().analyze(
        uuid4(), uuid4(), uuid4(), str(tmp_path), "sample.py", 1_048_576
    )

    assert result.outcome == "parsed"
    assert [symbol.name for symbol in result.symbols] == ["observed"]
