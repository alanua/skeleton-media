from __future__ import annotations

from pathlib import Path

import pytest

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import RuntimeLimits, VideoRuntimeConfig
from core.video_understanding.subprocess_tools import BoundedCommandRunner, CommandRequest


def config(tmp_path: Path) -> VideoRuntimeConfig:
    local = tmp_path / "local"; local.mkdir()
    source = local / "x"; source.write_bytes(b"x")
    temp = tmp_path / "temp"; temp.mkdir()
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    return VideoRuntimeConfig(
        artifact_root=artifacts,
        queue_root=tmp_path / "queue",
        temp_root=temp,
        approved_local_roots=(local,),
        local_media_registry={"abcdefghijklmnop": source},
        direct_media_allowed_hosts=(),
        executables={key: "/usr/bin/python3" for key in ("yt_dlp", "ffmpeg", "ffprobe", "sona", "ocr")},
        ollama_transport="private_bridge",
        ollama_model="synthetic",
        limits=RuntimeLimits(subprocess_timeout_seconds=2, subprocess_output_bytes=4096, lease_seconds=30),
    )


def test_argv_execution_has_no_shell_and_safe_environment(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    result = BoundedCommandRunner(cfg).require_success(
        CommandRequest("yt_dlp", ("-c", "import os;print(os.environ.get('PYTHONNOUSERSITE'));print('ok')"), cfg.temp_root),
        reason_code="SYNTHETIC_FAILED",
    )
    assert result.stdout_text().splitlines() == ["1", "ok"]
    assert result.argv[0] == "/usr/bin/python3"


def test_output_limit_includes_final_pipe_drain(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    with pytest.raises(VideoUnderstandingError) as exc:
        BoundedCommandRunner(cfg).run(
            CommandRequest("yt_dlp", ("-c", "import sys;sys.stdout.write('x'*3000)"), cfg.temp_root, max_output_bytes=1024)
        )
    assert exc.value.reason_code == "COMMAND_OUTPUT_TOO_LARGE"


def test_timeout_and_cwd_escape_fail_closed(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    with pytest.raises(VideoUnderstandingError) as timeout:
        BoundedCommandRunner(cfg).run(
            CommandRequest("yt_dlp", ("-c", "import time;time.sleep(2)"), cfg.temp_root, timeout_seconds=0.1)
        )
    assert timeout.value.reason_code == "COMMAND_TIMEOUT"
    with pytest.raises(VideoUnderstandingError) as cwd:
        BoundedCommandRunner(cfg).run(CommandRequest("yt_dlp", ("-c", "print(1)"), tmp_path))
    assert cwd.value.reason_code == "COMMAND_CWD_OUTSIDE_RUNTIME"


def test_unknown_executable_and_invalid_args_are_rejected(tmp_path: Path) -> None:
    runner = BoundedCommandRunner(config(tmp_path))
    with pytest.raises(VideoUnderstandingError):
        runner.build_argv("bash", ("-c", "x"))
    with pytest.raises(VideoUnderstandingError):
        runner.build_argv("ffmpeg", ("bad\x00arg",))
