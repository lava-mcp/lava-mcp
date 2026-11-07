from __future__ import annotations

import pytest

from lava_mcp import cmdline


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (["--transport", "streamable-http", "--token", "t"], {}),
        ([], {"LAVA_MCP_TRANSPORT": "streamable-http", "LAVA_TOKEN": "t"}),
    ],
)
def test_http_mode_rejects_server_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env: dict[str, str],
) -> None:
    # A server-side token in HTTP mode would let a client that sends no
    # X-Lava-Token act as the server's LAVA user, so startup must fail.
    for name in ("LAVA_TOKEN", "LAVA_URL", "LAVA_MCP_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cmdline, "build_server", pytest.fail)
    with pytest.raises(SystemExit) as exc:
        cmdline.main(argv)
    assert exc.value.code == 2
    assert "not allowed in HTTP mode" in capsys.readouterr().err
