"""The image starts the launcher, whose settings bound what a client can make the server buffer."""

from pathlib import Path

from app.serving.launcher import WS_MAX_SIZE

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
KIB = 1024


def test_the_image_command_starts_the_launcher_and_no_server_flags_of_its_own() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert 'CMD ["python", "-m", "app.serving"]' in text
    assert "uvicorn" not in text.replace("uvicorn[standard]", "")


def test_the_launcher_caps_the_size_of_a_websocket_message_at_64_kib() -> None:
    assert WS_MAX_SIZE == 64 * KIB
