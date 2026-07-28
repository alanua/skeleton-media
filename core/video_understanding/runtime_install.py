from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.video_understanding.live_runtime import (
    build_live_runtime,
    doctor_live_runtime,
    resolve_existing_private_memory_root,
    synthetic_memory_roundtrip,
)
from core.video_understanding.models import VideoUnderstandingError


YT_DLP_VERSION = "2026.06.09"
YT_DLP_URL = (
    "https://github.com/yt-dlp/yt-dlp/releases/download/2026.06.09/yt-dlp"
)
YT_DLP_SHA256 = "e5d57466682cfa9d61e9cf7c8a4f09b00f4a62af37d3bbdc4bcffdf63615feac"
VIBE_VERSION = "3.0.19"
VIBE_URL = (
    "https://github.com/thewh1teagle/vibe/releases/download/v3.0.19/"
    "vibe_3.0.19_amd64.deb"
)
VIBE_SHA256 = "f09780b705f594708b99a661fb2b64c2e7eb94c80b775f9906eb359ddc3a52a9"
PILLOW_VERSION = "12.2.0"
WORKER_UNIT = "skeleton-video-understanding-worker.service"
SONA_UNIT = "skeleton-video-understanding-sona.service"
_SAFE_SHA = frozenset("0123456789abcdef")
_MANDATORY_PROVIDER_MISSING_REASONS = {
    "ffmpeg": "FFMPEG_PROVIDER_MISSING",
    "ffprobe": "FFPROBE_PROVIDER_MISSING",
    "tesseract": "OCR_PROVIDER_MISSING",
}


@dataclass(frozen=True)
class RuntimeLayout:
    base: Path
    releases: Path
    current: Path
    venv: Path
    bin: Path
    opt: Path
    state: Path
    config_dir: Path
    config_file: Path
    systemd_dir: Path


@dataclass(frozen=True)
class InstallResult:
    source_sha: str
    runtime_config_status: str
    provider_ready_count: int
    provider_required_count: int
    ollama_status: str
    sona_status: str
    artifact_store_status: str
    queue_recovery_status: str
    memory_gateway_status: str
    memory_roundtrip_status: str
    service_install_status: str
    service_active: bool
    worker_count: int
    rollback_ready: bool
    stable_reason_codes: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "schema": "skeleton.video_understanding.install_receipt.v1",
            "source_merge_sha": self.source_sha,
            "runtime_config_status": self.runtime_config_status,
            "provider_ready_count": self.provider_ready_count,
            "provider_required_count": self.provider_required_count,
            "ollama_status": self.ollama_status,
            "sona_status": self.sona_status,
            "artifact_store_status": self.artifact_store_status,
            "queue_recovery_status": self.queue_recovery_status,
            "memory_gateway_status": self.memory_gateway_status,
            "memory_roundtrip_status": self.memory_roundtrip_status,
            "service_install_status": self.service_install_status,
            "service_active": self.service_active,
            "worker_count": self.worker_count,
            "rollback_ready": self.rollback_ready,
            "stable_reason_codes": list(self.stable_reason_codes),
        }


def install_runtime(
    source_root: Path,
    *,
    expected_sha: str,
    enable: bool,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> InstallResult:
    values = dict(os.environ if env is None else env)
    source_root = source_root.expanduser().resolve(strict=True)
    source_sha = _verify_source_sha(source_root, expected_sha)
    private_memory_root = resolve_existing_private_memory_root(values)
    layout = runtime_layout(home or Path.home())
    _prepare_layout(layout)

    previous_target = _symlink_target(layout.current)
    previous_worker_unit = _read_optional(layout.systemd_dir / WORKER_UNIT)
    previous_sona_unit = _read_optional(layout.systemd_dir / SONA_UNIT)
    release = layout.releases / source_sha
    reasons: list[str] = []
    rollback_ready = previous_target is not None or not layout.current.exists()

    try:
        _install_release(source_root, release)
        _ensure_venv(layout.venv)
        _install_pillow(layout.venv)
        yt_dlp = _install_yt_dlp(layout.bin)
        mandatory = {
            "yt_dlp": str(yt_dlp),
            "ffmpeg": _require_executable("ffmpeg"),
            "ffprobe": _require_executable("ffprobe"),
            "ocr": _require_executable("tesseract"),
        }
        vibe = _install_vibe_optional(layout.opt)
        if vibe is None:
            reasons.append("ASR_FALLBACK_BLOCKED")
        sona_executable = str(vibe or Path("/usr/bin/false"))
        model = _select_ollama_model(values)
        config = _runtime_config_payload(
            layout,
            mandatory=mandatory,
            sona_executable=sona_executable,
            ollama_model=model,
        )
        _atomic_private_json(layout.config_file, config)
        _atomic_symlink(release, layout.current)
        _install_units(
            layout,
            release=release,
            private_memory_root=private_memory_root,
            source_sha=source_sha,
            sona_enabled=vibe is not None,
        )

        doctor_before = doctor_live_runtime(
            layout.config_file,
            processing_revision=source_sha,
            env={**values, "SKELETON_PRIVATE_MEMORY_ROOT": str(private_memory_root)},
        )
        mandatory_ready = doctor_before.get("status") == "READY"
        if not mandatory_ready:
            doctor_reasons = doctor_before.get("stable_reason_codes")
            if isinstance(doctor_reasons, list):
                reasons.extend(str(reason) for reason in doctor_reasons)
            raise VideoUnderstandingError(
                "VIDEO_RUNTIME_DOCTOR_BLOCKED",
                "mandatory video runtime doctor checks failed",
            )

        runtime = build_live_runtime(
            layout.config_file,
            processing_revision=source_sha,
            env={**values, "SKELETON_PRIVATE_MEMORY_ROOT": str(private_memory_root)},
        )
        recovered = runtime.queue.recover_expired()
        memory_receipt = synthetic_memory_roundtrip(
            runtime,
            approval_ref="operator.video.runtime.launch",
        )
        if memory_receipt.get("status") != "DONE":
            raise VideoUnderstandingError(
                "MEMORY_GATEWAY_ROUNDTRIP_BLOCKED",
                "synthetic MemoryGateway roundtrip failed",
            )

        service_active = False
        worker_count = 0
        service_install_status = "INSTALLED_DISABLED"
        if enable:
            _systemctl_user("daemon-reload")
            if vibe is not None:
                _systemctl_user("enable", "--now", SONA_UNIT)
                _wait_unit_state(SONA_UNIT, required=False)
            _systemctl_user("enable", "--now", WORKER_UNIT)
            service_active = _wait_unit_state(WORKER_UNIT, required=True)
            worker_count = 1 if service_active and _unit_main_pid(WORKER_UNIT) > 0 else 0
            if worker_count != 1:
                raise VideoUnderstandingError(
                    "WORKER_COUNT_INVALID",
                    "exactly one active worker is required",
                )
            service_install_status = "ACTIVE"

        doctor_after = doctor_live_runtime(
            layout.config_file,
            processing_revision=source_sha,
            env={**values, "SKELETON_PRIVATE_MEMORY_ROOT": str(private_memory_root)},
        )
        sona_status = str(doctor_after.get("sona_status", "BLOCKED"))
        if sona_status != "READY" and "ASR_FALLBACK_BLOCKED" not in reasons:
            reasons.append("ASR_FALLBACK_BLOCKED")
        return InstallResult(
            source_sha=source_sha,
            runtime_config_status=str(doctor_after.get("runtime_config_status", "BLOCKED")),
            provider_ready_count=int(doctor_after.get("provider_ready_count", 0)),
            provider_required_count=int(doctor_after.get("provider_required_count", 4)),
            ollama_status=str(doctor_after.get("ollama_status", "BLOCKED")),
            sona_status=sona_status,
            artifact_store_status=str(doctor_after.get("artifact_store_status", "BLOCKED")),
            queue_recovery_status="DONE" if recovered >= 0 else "BLOCKED",
            memory_gateway_status=str(doctor_after.get("memory_gateway_status", "BLOCKED")),
            memory_roundtrip_status=str(memory_receipt.get("status", "BLOCKED")),
            service_install_status=service_install_status,
            service_active=service_active,
            worker_count=worker_count,
            rollback_ready=rollback_ready,
            stable_reason_codes=tuple(sorted(set(reasons))),
        )
    except Exception:
        _rollback(
            layout,
            previous_target=previous_target,
            previous_worker_unit=previous_worker_unit,
            previous_sona_unit=previous_sona_unit,
        )
        raise


def runtime_layout(home: Path) -> RuntimeLayout:
    home = home.expanduser().resolve(strict=False)
    base = home / ".local" / "share" / "skeleton" / "video-understanding"
    return RuntimeLayout(
        base=base,
        releases=base / "releases",
        current=base / "current",
        venv=base / "venv",
        bin=base / "bin",
        opt=base / "opt",
        state=home / ".local" / "state" / "skeleton" / "video-understanding",
        config_dir=home / ".config" / "skeleton",
        config_file=home / ".config" / "skeleton" / "video-understanding.json",
        systemd_dir=home / ".config" / "systemd" / "user",
    )


def _prepare_layout(layout: RuntimeLayout) -> None:
    for path in (
        layout.base,
        layout.releases,
        layout.bin,
        layout.opt,
        layout.state,
        layout.config_dir,
        layout.systemd_dir,
        layout.state / "artifacts",
        layout.state / "queue",
        layout.state / "tmp",
        layout.state / "local-media",
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)


def _verify_source_sha(source_root: Path, expected_sha: str) -> str:
    if len(expected_sha) != 40 or any(character not in _SAFE_SHA for character in expected_sha):
        raise VideoUnderstandingError("EXPECTED_SHA_INVALID", "expected source SHA is invalid")
    completed = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    actual = completed.stdout.strip()
    if completed.returncode != 0 or actual != expected_sha:
        raise VideoUnderstandingError("SOURCE_SHA_MISMATCH", "source checkout SHA does not match")
    dirty = subprocess.run(
        ["git", "-C", str(source_root), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    if dirty.returncode != 0 or dirty.stdout.strip():
        raise VideoUnderstandingError("SOURCE_CHECKOUT_DIRTY", "source checkout must be clean")
    return actual


def _install_release(source_root: Path, release: Path) -> None:
    if release.exists():
        marker = release / ".source-ready"
        if marker.is_file():
            return
        shutil.rmtree(release)
    temporary = release.with_name(release.name + ".part")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, mode=0o700)
    for name in ("core", "scripts", "schemas", "docs"):
        source = source_root / name
        if source.is_dir():
            shutil.copytree(
                source,
                temporary / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    for name in ("pyproject.toml",):
        shutil.copy2(source_root / name, temporary / name)
    (temporary / ".source-ready").write_text("ready\n", encoding="utf-8")
    os.replace(temporary, release)


def _ensure_venv(path: Path) -> None:
    python = path / "bin" / "python"
    if python.is_file():
        return
    venv.EnvBuilder(with_pip=True, clear=True).create(path)


def _install_pillow(venv_path: Path) -> None:
    python = venv_path / "bin" / "python"
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            f"pillow=={PILLOW_VERSION}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise VideoUnderstandingError(
            "PILLOW_INSTALL_FAILED",
            "pinned video vision dependency could not be installed",
        )


def _install_yt_dlp(bin_dir: Path) -> Path:
    target = bin_dir / "yt-dlp"
    if target.is_file() and _sha256_file(target) == YT_DLP_SHA256:
        target.chmod(0o700)
        return target
    _download_exact(YT_DLP_URL, target, YT_DLP_SHA256, max_bytes=5_000_000)
    target.chmod(0o700)
    return target


def _install_vibe_optional(opt_dir: Path) -> Path | None:
    install_root = opt_dir / f"vibe-{VIBE_VERSION}"
    candidates = (
        install_root / "usr" / "bin" / "vibe",
        install_root / "usr" / "lib" / "vibe" / "vibe",
    )
    for candidate in candidates:
        if candidate.is_file():
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
            return candidate
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="skeleton-vibe-") as temporary_dir:
            package = Path(temporary_dir) / "vibe.deb"
            _download_exact(VIBE_URL, package, VIBE_SHA256, max_bytes=60_000_000)
            temporary_install = install_root.with_name(install_root.name + ".part")
            shutil.rmtree(temporary_install, ignore_errors=True)
            completed = subprocess.run(
                [dpkg_deb, "-x", str(package), str(temporary_install)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                shutil.rmtree(temporary_install, ignore_errors=True)
                return None
            shutil.rmtree(install_root, ignore_errors=True)
            os.replace(temporary_install, install_root)
    except Exception:
        return None
    for candidate in candidates:
        if candidate.is_file():
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
            return candidate
    matches = tuple(path for path in install_root.rglob("vibe") if path.is_file())
    if len(matches) == 1:
        matches[0].chmod(matches[0].stat().st_mode | stat.S_IXUSR)
        return matches[0]
    return None


def _download_exact(url: str, target: Path, expected_hash: str, *, max_bytes: int) -> None:
    temporary = target.with_name(target.name + ".part")
    temporary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Skeleton-Video-Understanding-Installer/1"},
        )
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as handle:
            total = 0
            while True:
                chunk = response.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise VideoUnderstandingError(
                        "RUNTIME_ASSET_TOO_LARGE",
                        "runtime asset exceeded size limit",
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256_file(temporary) != expected_hash:
            raise VideoUnderstandingError(
                "RUNTIME_ASSET_HASH_MISMATCH",
                "runtime asset hash did not match",
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _require_executable(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise VideoUnderstandingError(
            _MANDATORY_PROVIDER_MISSING_REASONS.get(
                name,
                "MANDATORY_PROVIDER_MISSING",
            ),
            "a mandatory local provider is unavailable",
        )
    return str(Path(value).resolve(strict=True))


def _select_ollama_model(env: Mapping[str, str]) -> str:
    endpoint = "http://127.0.0.1:11434/api/tags"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(1_000_001).decode("utf-8"))
    except Exception as exc:
        raise VideoUnderstandingError("OLLAMA_BLOCKED", "Ollama loopback is unavailable") from exc
    models = payload.get("models") if isinstance(payload, Mapping) else None
    names = [
        str(item.get("name"))
        for item in models or []
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    ]
    requested = str(env.get("SKELETON_VIDEO_OLLAMA_MODEL", "")).strip()
    if requested:
        if requested not in names:
            raise VideoUnderstandingError("OLLAMA_MODEL_MISSING", "configured Ollama model is unavailable")
        return requested
    candidates = [name for name in names if "embed" not in name.casefold()]
    if not candidates:
        raise VideoUnderstandingError("OLLAMA_MODEL_MISSING", "no synthesis Ollama model is available")
    return sorted(candidates)[0]


def _runtime_config_payload(
    layout: RuntimeLayout,
    *,
    mandatory: Mapping[str, str],
    sona_executable: str,
    ollama_model: str,
) -> dict[str, object]:
    return {
        "artifact_root": str(layout.state / "artifacts"),
        "queue_root": str(layout.state / "queue"),
        "temp_root": str(layout.state / "tmp"),
        "approved_local_roots": [str(layout.state / "local-media")],
        "local_media_registry": {},
        "direct_media_allowed_hosts": [],
        "executables": {
            **dict(mandatory),
            "sona": sona_executable,
        },
        "ollama_transport": "loopback",
        "ollama_model": ollama_model,
        "ollama_endpoint": "http://127.0.0.1:11434",
        "sona_endpoint": "http://127.0.0.1:3022",
        "sona_model": "default",
        "sona_start_args": ["--server"],
        "subtitle_languages": ["uk", "de", "en", "ru"],
        "ocr_languages": ["ukr", "deu", "eng", "rus"],
        "limits": {
            "max_duration_seconds": 21600,
            "max_download_bytes": 8589934592,
            "max_transcript_chars": 2000000,
            "max_frames": 80,
            "max_ocr_chars_per_frame": 20000,
            "max_redirects": 5,
            "subprocess_timeout_seconds": 900,
            "subprocess_output_bytes": 4194304,
            "lease_seconds": 900,
            "max_attempts": 4,
        },
    }


def _install_units(
    layout: RuntimeLayout,
    *,
    release: Path,
    private_memory_root: Path,
    source_sha: str,
    sona_enabled: bool,
) -> None:
    python = layout.venv / "bin" / "python"
    worker_unit = f"""[Unit]
Description=Skeleton Video Understanding Worker
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={release}
Environment=PYTHONUNBUFFERED=1
Environment=SKELETON_PRIVATE_MEMORY_ROOT={private_memory_root}
ExecStart={python} {release / 'scripts' / 'video_understanding_worker.py'} --config {layout.config_file} --processing-revision {source_sha} --worker-id hetzner-video-worker-1 --forever
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""
    _atomic_private_text(layout.systemd_dir / WORKER_UNIT, worker_unit)
    sona_unit_path = layout.systemd_dir / SONA_UNIT
    if sona_enabled:
        config = json.loads(layout.config_file.read_text(encoding="utf-8"))
        executable = str(config["executables"]["sona"])
        sona_unit = f"""[Unit]
Description=Skeleton Video Understanding Local Sona API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} --server
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""
        _atomic_private_text(sona_unit_path, sona_unit)
    else:
        sona_unit_path.unlink(missing_ok=True)


def _systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["systemctl", "--user", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise VideoUnderstandingError(
            "USER_SERVICE_CONTROL_FAILED",
            "user service control failed",
        )
    return completed


def _wait_unit_state(unit: str, *, required: bool) -> bool:
    for _ in range(20):
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.stdout.strip() == "active":
            return True
        time.sleep(0.5)
    if required:
        raise VideoUnderstandingError("WORKER_SERVICE_NOT_ACTIVE", "worker service is not active")
    return False


def _unit_main_pid(unit: str) -> int:
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        return int(completed.stdout.strip()) if completed.returncode == 0 else 0
    except ValueError:
        return 0


def _rollback(
    layout: RuntimeLayout,
    *,
    previous_target: Path | None,
    previous_worker_unit: str | None,
    previous_sona_unit: str | None,
) -> None:
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", WORKER_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SONA_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if previous_target is not None:
            _atomic_symlink(previous_target, layout.current)
        elif layout.current.is_symlink():
            layout.current.unlink()
        _restore_optional(layout.systemd_dir / WORKER_UNIT, previous_worker_unit)
        _restore_optional(layout.systemd_dir / SONA_UNIT, previous_sona_unit)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if previous_worker_unit is not None:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", WORKER_UNIT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        if previous_sona_unit is not None:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", SONA_UNIT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
    except Exception:
        pass


def _atomic_private_json(path: Path, payload: object) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    _atomic_private_text(path, rendered)


def _atomic_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".part")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_symlink(target: Path, link: Path) -> None:
    temporary = link.with_name(link.name + ".part")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _symlink_target(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _restore_optional(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_private_text(path, content)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
