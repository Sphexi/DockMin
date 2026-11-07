"""Every module under app/ stays within the line limit."""

from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
MAX_MODULE_LINES = 2000
SMALL_LIMIT = 2
LINE = "x = 1\n"


def oversized_modules(app: Path, limit: int) -> list[str]:
    lengths = {
        path.relative_to(app.parent).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(app.rglob("*.py"))
    }
    return [f"{name}: {lines} lines" for name, lines in lengths.items() if lines > limit]


def test_no_module_under_app_exceeds_the_line_limit() -> None:
    offenders = oversized_modules(APP, MAX_MODULE_LINES)

    assert offenders == [], f"modules over {MAX_MODULE_LINES} lines:\n" + "\n".join(offenders)


def test_the_size_check_reports_each_module_over_the_limit_with_its_length(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "pkg").mkdir(parents=True)
    (app / "pkg" / "long.py").write_text(LINE * (SMALL_LIMIT + 1), encoding="utf-8")
    (app / "short.py").write_text(LINE, encoding="utf-8")
    (app / "exact.py").write_text(LINE * SMALL_LIMIT, encoding="utf-8")

    offenders = oversized_modules(app, SMALL_LIMIT)

    assert offenders == [f"app/pkg/long.py: {SMALL_LIMIT + 1} lines"]
