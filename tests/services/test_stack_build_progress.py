"""What a stack job page collects from a running image build."""

from app.compose_cli.buildx import LogLine, VertexUpdate
from app.services.stack_jobs import MAX_BUILD_LOG_LINES, StackProgress

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def step(
    digest: str, status: str, *, cached: bool = False, error: str | None = None
) -> VertexUpdate:
    return VertexUpdate(digest, "RUN make", status, cached, error)  # type: ignore[arg-type]


def test_a_build_reports_its_latest_step_states_and_the_tail_of_its_output() -> None:
    progress = StackProgress()
    before = progress.version

    report = progress.build("web", "shop-web")
    report(step(DIGEST, "running"))
    report(step(DIGEST, "done", cached=True))
    report(step(OTHER, "error", error="exit 2"))
    for number in range(MAX_BUILD_LOG_LINES + 5):
        report(LogLine("RUN make", f"line {number}"))
    [view] = progress.snapshot().builds

    assert progress.version > before
    assert (view.service, view.tag) == ("web", "shop-web")
    assert [(s.status, s.cached, s.error) for s in view.steps] == [
        ("done", True, None),
        ("error", False, "exit 2"),
    ]
    assert len(view.log) == MAX_BUILD_LOG_LINES and view.log[-1].endswith(
        str(MAX_BUILD_LOG_LINES + 4)
    )


def test_asking_again_for_a_service_continues_its_build_instead_of_starting_another() -> None:
    progress = StackProgress()

    progress.build("web", "shop-web")(step(DIGEST, "running"))
    progress.build("web", "shop-web")(step(DIGEST, "done"))

    [view] = progress.snapshot().builds
    assert [s.status for s in view.steps] == ["done"]
