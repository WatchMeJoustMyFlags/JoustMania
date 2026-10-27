#!/usr/bin/env python3
"""Generate the voice lines of the catalog (#2107, UC-031 BR-007, C-050).

    scripts/tools/gen-voice.sh                 # make voice: render what changed
    scripts/tools/gen-voice.sh --check         # make voice-check: no TTS, no network
    scripts/tools/gen-voice.sh --sample        # make voice-sample: re-render the sample lines, compare
    scripts/tools/gen-voice.sh --only high --profile gen_female --dry-run

Reads services/audio/voice/lines.yaml and writes, for every generated profile,
services/audio/assets/<Area>/vox/<profile>/<id>.ogg plus
services/audio/assets/voice_manifest.json. A line is rendered again only when
its input hash (spoken text, profile parameters, model sha256, loudness and
output format, generator version) changed, so an unchanged catalog leaves every
committed byte alone. The recorded profiles (`aaron`, `ivy`) are never written.

Piper output is not bit-reproducible (the VITS noise has no seed), so the
manifest pins the inputs, not the PCM; `--sample` compares a fresh render with
the committed file by duration and loudness.

Only `--check` and the fake engine run with the standard library plus PyYAML;
the Piper engine needs the packages gen-voice.sh pins (sherpa-onnx, numpy,
soxr, pyloudnorm, imageio-ffmpeg).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

GENERATOR_VERSION = "1"
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "services" / "audio" / "voice" / "lines.yaml"
ASSETS = REPO_ROOT / "services" / "audio" / "assets"
MANIFEST_NAME = "voice_manifest.json"
MODEL_CACHE = Path(
    os.environ.get("JOUSTMANIA_VOICE_CACHE", "~/.cache/joustmania/voice-models")
).expanduser()

PROFILE_KEYS = {
    "partner",
    "model",
    "model_url",
    "model_sha256",
    "speaker",
    "length_scale",
    "noise_scale",
    "noise_scale_w",
}
LINE_KEYS = {"id", "sound", "text", "say", "recorded"}
TOP_KEYS = {"version", "recorded", "loudness", "audio", "profiles", "sample", "areas"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
# The recordings: never written, whatever the catalog's `recorded` list says (C-050).
PROTECTED_PROFILES = frozenset({"aaron", "ivy"})

# --sample tolerances (Piper output varies run to run).
SAMPLE_DURATION_TOLERANCE = 0.10
SAMPLE_LUFS_TOLERANCE = 1.0


class CatalogError(Exception):
    """The catalog is invalid or a write would leave the generated profiles."""


@dataclass
class Rendered:
    data: bytes
    duration_s: float
    lufs: float


@dataclass
class Result:
    rendered: int = 0
    skipped: int = 0
    pruned: int = 0
    refreshed: int = 0
    paths: list[str] = field(default_factory=list)


# --- catalog ---------------------------------------------------------------


def load_catalog(path: Path = CATALOG) -> dict:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def validate_catalog(cat: dict) -> list[str]:
    """Every problem in the catalog, as text; empty when it is valid."""
    errors: list[str] = []
    if not isinstance(cat, dict):
        return ["catalog: not a mapping"]
    errors += [f"catalog: unknown key {k!r}" for k in sorted(set(cat) - TOP_KEYS)]
    if cat.get("version") != 1:
        errors.append("catalog: version must be 1")
    recorded = cat.get("recorded") or []
    if not isinstance(recorded, list) or not all(
        isinstance(r, str) and SAFE_NAME.match(r) for r in recorded
    ):
        errors.append("catalog: recorded must be a list of profile names")
        recorded = []
    loudness = cat.get("loudness") or {}
    if not all(
        isinstance(loudness.get(k), int | float)
        for k in ("target_lufs", "tolerance_lu")
    ):
        errors.append("catalog: loudness needs target_lufs and tolerance_lu")
    audio = cat.get("audio") or {}
    if not all(
        isinstance(audio.get(k), int)
        for k in ("sample_rate", "channels", "vorbis_quality")
    ):
        errors.append("catalog: audio needs sample_rate, channels and vorbis_quality")
    profiles = cat.get("profiles") or {}
    if not isinstance(profiles, dict) or not profiles:
        errors.append("catalog: profiles must name at least one generated profile")
        profiles = {}
    for name, prof in profiles.items():
        if not SAFE_NAME.match(str(name)):
            errors.append(f"profile {name!r}: invalid name")
        if name in recorded or name in PROTECTED_PROFILES:
            errors.append(
                f"profile {name!r}: is a recorded profile; the generator never writes it"
            )
        if not isinstance(prof, dict):
            errors.append(f"profile {name!r}: not a mapping")
            continue
        errors += [
            f"profile {name!r}: missing {k}" for k in sorted(PROFILE_KEYS - set(prof))
        ]
        errors += [
            f"profile {name!r}: unknown key {k!r}"
            for k in sorted(set(prof) - PROFILE_KEYS)
        ]
        if "partner" in prof and prof["partner"] not in recorded:
            errors.append(
                f"profile {name!r}: partner {prof['partner']!r} is not a recorded profile"
            )
        if "model_sha256" in prof and not re.fullmatch(
            r"[0-9a-f]{64}", str(prof["model_sha256"])
        ):
            errors.append(f"profile {name!r}: model_sha256 is not a sha256")
    areas = cat.get("areas") or {}
    if not isinstance(areas, dict) or not areas:
        errors.append("catalog: areas must hold at least one area")
        areas = {}
    seen: set[str] = set()
    for area, lines in areas.items():
        if not SAFE_NAME.match(str(area)):
            errors.append(f"area {area!r}: invalid name")
        for n, line in enumerate(lines or []):
            where = f"{area}[{n}]"
            if not isinstance(line, dict):
                errors.append(f"{where}: not a mapping")
                continue
            errors += [
                f"{where}: unknown key {k!r}" for k in sorted(set(line) - LINE_KEYS)
            ]
            line_id = line.get("id")
            if (
                not isinstance(line_id, str)
                or not SAFE_ID.match(line_id)
                or line_id.strip() != line_id
            ):
                errors.append(f"{where}: invalid id {line_id!r}")
                continue
            key = f"{area}/{line_id}"
            if key in seen:
                errors.append(f"{key}: duplicate id")
            seen.add(key)
            for k in ("text", "say"):
                if k in line and (not isinstance(line[k], str) or not line[k].strip()):
                    errors.append(f"{key}: empty {k}")
            if "text" not in line:
                errors.append(f"{key}: missing text")
    for ref in cat.get("sample") or []:
        if ref not in seen:
            errors.append(f"sample: unknown line {ref!r}")
    return errors


def _require_valid(cat: dict) -> None:
    errors = validate_catalog(cat)
    if errors:
        raise CatalogError("invalid catalog:\n  " + "\n  ".join(errors))


def iter_lines(cat: dict):
    for area, lines in cat["areas"].items():
        for line in lines:
            yield area, line


def spoken(line: dict) -> str:
    """The text handed to the synthesizer, whitespace folded."""
    return " ".join(str(line.get("say") or line["text"]).split())


def output_path(area: str, profile: str, line_id: str) -> str:
    return f"{area}/vox/{profile}/{line_id}.ogg"


def assert_generated_path(cat: dict, rel: str) -> None:
    """Refuse any path that is not <area>/vox/<generated profile>/<file>."""
    parts = PurePosixPath(rel).parts
    if (
        len(parts) != 4
        or parts[1] != "vox"
        or ".." in parts
        or parts[2] not in cat["profiles"]
        or parts[2] in cat["recorded"]
        or parts[2] in PROTECTED_PROFILES
    ):
        raise CatalogError(f"refusing to write {rel!r}: not under a generated profile")


def text_hash(line: dict) -> str:
    """The displayed text: a text edit must reach the manifest even when `say` is unchanged."""
    return hashlib.sha256(" ".join(str(line["text"]).split()).encode()).hexdigest()


def input_hash(cat: dict, profile: str, line: dict) -> str:
    prof = {k: v for k, v in cat["profiles"][profile].items() if k != "partner"}
    payload = {
        "generator": GENERATOR_VERSION,
        "say": spoken(line),
        "profile": prof,
        "loudness": cat["loudness"],
        "audio": cat["audio"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# --- manifest --------------------------------------------------------------


def load_manifest(assets: Path) -> dict | None:
    path = Path(assets) / MANIFEST_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_head(cat: dict) -> dict:
    return {
        "version": 1,
        "generator": GENERATOR_VERSION,
        "catalog": "services/audio/voice/lines.yaml",
        "recorded": list(cat["recorded"]),
        "profiles": {
            name: {
                "partner": p["partner"],
                "model": p["model"],
                "model_sha256": p["model_sha256"],
            }
            for name, p in sorted(cat["profiles"].items())
        },
    }


def _write_manifest(assets: Path, manifest: dict) -> None:
    manifest = dict(manifest)
    manifest["files"] = dict(sorted(manifest["files"].items()))
    text = json.dumps(manifest, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    (Path(assets) / MANIFEST_NAME).write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _expected(cat: dict) -> dict[str, tuple[str, str, dict]]:
    """rel path -> (profile, area/id, line) for every line of every generated profile."""
    out = {}
    for area, line in iter_lines(cat):
        for profile in cat["profiles"]:
            out[output_path(area, profile, line["id"])] = (
                profile,
                f"{area}/{line['id']}",
                line,
            )
    return out


def _generated_dirs(cat: dict, assets: Path):
    """Every <area>/vox/<generated profile>/ directory: catalog areas and any other area on disk."""
    assets = Path(assets)
    dirs = {
        assets / area / "vox" / profile
        for area in cat["areas"]
        for profile in cat["profiles"]
    }
    for profile in cat["profiles"]:
        dirs.update(d for d in assets.glob(f"*/vox/{profile}") if d.is_dir())
    yield from sorted(dirs)


# --- check (make voice-check) ------------------------------------------------


def check(cat: dict, assets: Path) -> list[str]:
    """Every disagreement between catalog, manifest and files; empty when fresh."""
    problems = validate_catalog(cat)
    if problems:
        return problems
    assets = Path(assets)
    manifest = load_manifest(assets)
    if manifest is None:
        return [f"{MANIFEST_NAME}: missing; run `make voice`"]
    head = _manifest_head(cat)
    for key in ("recorded", "profiles"):
        if manifest.get(key) != head[key]:
            problems.append(
                f"{MANIFEST_NAME}: {key} differ from the catalog; run `make voice`"
            )
    files = manifest.get("files", {})
    expected = _expected(cat)
    for rel, (profile, line_key, line) in expected.items():
        entry = files.get(rel)
        if entry is None:
            problems.append(
                f"{rel}: missing from the manifest ({line_key}); run `make voice`"
            )
            continue
        if entry.get("input_sha256") != input_hash(cat, profile, line):
            problems.append(
                f"{rel}: stale (the catalog line or profile changed); run `make voice`"
            )
        elif entry.get("text_sha256") != text_hash(line):
            problems.append(
                f"{rel}: stale (the catalog text changed); run `make voice`"
            )
        path = assets / rel
        if not path.is_file():
            problems.append(f"{rel}: file missing; run `make voice`")
        elif sha256_file(path) != entry.get("sha256"):
            problems.append(
                f"{rel}: sha256 differs from the manifest (edited by hand?)"
            )
    problems += [
        f"{rel}: in the manifest but not in the catalog"
        for rel in sorted(set(files) - set(expected))
    ]
    for directory in _generated_dirs(cat, assets):
        if not directory.is_dir():
            continue
        for f in sorted(directory.iterdir()):
            rel = f.relative_to(assets).as_posix()
            if rel not in expected:
                problems.append(f"{rel}: stray file in a generated profile")
    return problems


# --- generate (make voice) ---------------------------------------------------


def generate(
    cat: dict,
    assets: Path,
    renderer,
    *,
    only: set[str] | None = None,
    profiles: set[str] | None = None,
    dry_run: bool = False,
    log=print,
) -> Result:
    """Render the lines whose input changed; prune generated files no line owns."""
    _require_valid(cat)
    assets = Path(assets)
    manifest = load_manifest(assets) or {}
    files = dict(manifest.get("files", {}))
    result = Result()
    expected = _expected(cat)
    for rel, (profile, line_key, line) in expected.items():
        if only and line["id"] not in only and line_key not in only:
            continue
        if profiles and profile not in profiles:
            continue
        assert_generated_path(cat, rel)
        digest = input_hash(cat, profile, line)
        entry = files.get(rel)
        path = assets / rel
        fresh = (
            entry is not None
            and entry.get("input_sha256") == digest
            and path.is_file()
            and sha256_file(path) == entry.get("sha256")
        )
        if fresh:
            if entry.get("text_sha256") != text_hash(line):
                # Only the displayed text changed: the audio stays, the manifest follows.
                result.refreshed += 1
                if not dry_run:
                    files[rel] = {
                        **entry,
                        "line": line_key,
                        "text_sha256": text_hash(line),
                    }
            else:
                result.skipped += 1
            continue
        result.rendered += 1
        result.paths.append(rel)
        log(f"render {rel}: {spoken(line)[:60]!r}")
        if dry_run:
            continue
        out = renderer.render(profile, cat["profiles"][profile], spoken(line))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(out.data)
        files[rel] = {
            "line": line_key,
            "input_sha256": digest,
            "text_sha256": text_hash(line),
            "sha256": hashlib.sha256(out.data).hexdigest(),
            "duration_s": round(out.duration_s, 3),
            "lufs": round(out.lufs, 2),
        }
    if not only and not profiles:
        for rel in sorted(set(files) - set(expected)):
            files.pop(rel)
        for directory in _generated_dirs(cat, assets):
            if not directory.is_dir():
                continue
            for f in sorted(directory.iterdir()):
                rel = f.relative_to(assets).as_posix()
                if rel not in expected:
                    result.pruned += 1
                    log(f"prune {rel}")
                    if not dry_run:
                        f.unlink()
    if not dry_run:
        _write_manifest(assets, {**_manifest_head(cat), "files": files})
    return result


# --- engines -----------------------------------------------------------------


class FakeRenderer:
    """Deterministic bytes, no model: for tests and dry plumbing checks."""

    def __init__(self, cat: dict):
        self._target = float(cat["loudness"]["target_lufs"])

    def render(self, profile: str, prof: dict, text: str) -> Rendered:
        seed = json.dumps([profile, prof, text], sort_keys=True).encode()
        data = b"FAKEOGG\0" + hashlib.sha256(seed).digest()
        return Rendered(
            data=data, duration_s=round(0.3 + 0.06 * len(text), 3), lufs=self._target
        )


def find_ffmpeg() -> str:
    """$FFMPEG, the pinned imageio-ffmpeg binary, or ffmpeg on PATH."""
    explicit = os.environ.get("FFMPEG")
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - optional dependency, fall back to PATH
        found = shutil.which("ffmpeg")
        if found:
            return found
    raise SystemExit(
        "gen_voice: ffmpeg not found (run through scripts/tools/gen-voice.sh, or set FFMPEG)"
    )


class PiperRenderer:
    """Piper VITS voices through sherpa-onnx, loudness-normalised, encoded by ffmpeg."""

    def __init__(self, cat: dict, cache: Path = MODEL_CACHE):
        import numpy as np  # noqa: F401 - fail early when the tool deps are missing
        import pyloudnorm  # noqa: F401
        import sherpa_onnx  # noqa: F401
        import soxr  # noqa: F401

        self._cat = cat
        self._cache = Path(cache)
        self._engines: dict[str, object] = {}
        self._ffmpeg = find_ffmpeg()

    # model download, verification and extraction
    def _model_dir(self, prof: dict) -> Path:
        self._cache.mkdir(parents=True, exist_ok=True)
        archive = self._cache / PurePosixPath(prof["model_url"]).name
        if not archive.is_file():
            print(f"download {prof['model_url']}", file=sys.stderr)
            tmp = archive.with_suffix(archive.suffix + ".part")
            with (
                urllib.request.urlopen(prof["model_url"], timeout=120) as resp,
                open(tmp, "wb") as out,
            ):  # noqa: S310
                shutil.copyfileobj(resp, out)
            tmp.rename(archive)
        digest = sha256_file(archive)
        if digest != prof["model_sha256"]:
            raise SystemExit(
                f"gen_voice: {archive} sha256 {digest} != pinned {prof['model_sha256']}"
            )
        target = self._cache / prof["model"]
        marker = target / ".sha256"
        if not marker.is_file() or marker.read_text().strip() != digest:
            if target.exists():
                shutil.rmtree(target)
            with tarfile.open(archive) as tar:
                tar.extractall(self._cache, filter="data")
            marker.write_text(digest + "\n")
        return target

    def _engine(self, profile: str, prof: dict):
        if profile not in self._engines:
            import sherpa_onnx

            d = self._model_dir(prof)
            onnx = next(d.glob("*.onnx"))
            vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(onnx),
                tokens=str(d / "tokens.txt"),
                data_dir=str(d / "espeak-ng-data"),
                length_scale=float(prof["length_scale"]),
                noise_scale=float(prof["noise_scale"]),
                noise_scale_w=float(prof["noise_scale_w"]),
            )
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=vits, num_threads=1, provider="cpu"
                ),
                max_num_sentences=1,
            )
            if not config.validate():
                raise SystemExit(f"gen_voice: invalid sherpa-onnx config for {profile}")
            self._engines[profile] = sherpa_onnx.OfflineTts(config)
        return self._engines[profile]

    @staticmethod
    def _trim(samples, rate: int):
        """Cut leading and trailing silence (40 dB under the loudest 10 ms), keep 60 ms."""
        import numpy as np

        win = max(1, rate // 100)
        n = len(samples) // win
        if n == 0:
            return samples
        rms = np.sqrt(np.mean(samples[: n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
        active = np.nonzero(rms > rms.max() * 10 ** (-40 / 20))[0]
        if active.size == 0:
            return samples
        pad = int(0.06 * rate)
        start = max(0, active[0] * win - pad)
        end = min(len(samples), (active[-1] + 1) * win + pad)
        return samples[start:end]

    @staticmethod
    def _limit(samples, knee: float = 0.7, ceiling: float = 0.97):
        """Soft-limit everything above `knee` so no sample exceeds `ceiling`."""
        import numpy as np

        mag = np.abs(samples)
        over = mag > knee
        span = ceiling - knee
        mag[over] = knee + span * np.tanh((mag[over] - knee) / span)
        return np.sign(samples) * mag

    @staticmethod
    def loudness(samples, rate: int) -> float:
        """Integrated loudness (BS.1770-4); short clips are tiled to 1 s first."""
        import numpy as np
        import pyloudnorm

        x = samples if samples.ndim == 1 else samples.mean(axis=1)
        if len(x) < rate:
            x = np.tile(x, int(np.ceil(rate / max(1, len(x)))))
        return float(pyloudnorm.Meter(rate).integrated_loudness(x))

    def render(self, profile: str, prof: dict, text: str) -> Rendered:
        import numpy as np
        import soxr

        audio = self._engine(profile, prof).generate(
            text, sid=int(prof["speaker"]), speed=1.0
        )
        samples = np.asarray(audio.samples, dtype=np.float64)
        if samples.size == 0:
            raise SystemExit(f"gen_voice: {profile} rendered no audio for {text!r}")
        samples = self._trim(samples, audio.sample_rate)
        out_rate = int(self._cat["audio"]["sample_rate"])
        samples = soxr.resample(samples, audio.sample_rate, out_rate)
        target = float(self._cat["loudness"]["target_lufs"])
        # Gain to the target, then a soft limiter for the peaks; the limiter
        # costs a little loudness, so measure and correct again (converges in 2-3).
        for _ in range(3):
            samples = self._limit(
                samples * 10 ** ((target - self.loudness(samples, out_rate)) / 20)
            )
        channels = int(self._cat["audio"]["channels"])
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
        pcm = np.repeat(pcm[:, None], channels, axis=1)
        with tempfile.TemporaryDirectory() as tmp:
            wav, ogg = Path(tmp) / "in.wav", Path(tmp) / "out.ogg"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(channels)
                w.setsampwidth(2)
                w.setframerate(out_rate)
                w.writeframes(pcm.tobytes())
            self._encode(wav, ogg)
            data = ogg.read_bytes()
            decoded = self._decode(ogg, out_rate, channels)
        return Rendered(
            data=data,
            duration_s=len(decoded) / out_rate,
            lufs=self.loudness(decoded, out_rate),
        )

    def _encode(self, wav: Path, ogg: Path) -> None:
        # bitexact: no encoder version tag and a fixed Ogg stream serial, so an
        # unchanged input encodes to the same bytes (libsndfile picks a random serial).
        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(wav), "-map_metadata", "-1",
            "-c:a", "libvorbis", "-q:a", str(self._cat["audio"]["vorbis_quality"]),
            "-fflags", "+bitexact", "-flags:a", "+bitexact", str(ogg),
        ]  # fmt: skip
        subprocess.run(cmd, check=True)

    def _decode(self, ogg: Path, rate: int, channels: int):
        import numpy as np

        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(ogg),
            "-f", "f32le", "-ac", str(channels), "-ar", str(rate), "-",
        ]  # fmt: skip
        raw = subprocess.run(cmd, check=True, capture_output=True).stdout
        return np.frombuffer(raw, dtype="<f4").astype(np.float64).reshape(-1, channels)


# --- sample (make voice-sample) ------------------------------------------------


def sample(cat: dict, assets: Path, renderer) -> list[str]:
    """Re-render the sample lines; compare duration and loudness with the committed files."""
    _require_valid(cat)
    manifest = load_manifest(assets) or {"files": {}}
    lines = {f"{area}/{line['id']}": (area, line) for area, line in iter_lines(cat)}
    problems = []
    for ref in cat.get("sample") or []:
        area, line = lines[ref]
        for profile, prof in cat["profiles"].items():
            rel = output_path(area, profile, line["id"])
            entry = manifest["files"].get(rel)
            if entry is None:
                problems.append(f"{rel}: not generated")
                continue
            fresh = renderer.render(profile, prof, spoken(line))
            ratio = fresh.duration_s / entry["duration_s"]
            print(f"{rel}: duration {fresh.duration_s:.2f}/{entry['duration_s']:.2f} s, "
                  f"loudness {fresh.lufs:.2f}/{entry['lufs']:.2f} LUFS")  # fmt: skip
            if abs(ratio - 1) > SAMPLE_DURATION_TOLERANCE:
                problems.append(
                    f"{rel}: duration {fresh.duration_s:.2f} s vs {entry['duration_s']:.2f} s"
                )
            if abs(fresh.lufs - entry["lufs"]) > SAMPLE_LUFS_TOLERANCE:
                problems.append(
                    f"{rel}: loudness {fresh.lufs:.2f} vs {entry['lufs']:.2f} LUFS"
                )
    return problems


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify catalog, manifest and files (no TTS)",
    )
    mode.add_argument(
        "--sample", action="store_true", help="re-render the sample lines and compare"
    )
    ap.add_argument(
        "--only", action="append", default=[], help="line id or Area/id (repeatable)"
    )
    ap.add_argument(
        "--profile", action="append", default=[], help="generated profile (repeatable)"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be rendered, write nothing",
    )
    ap.add_argument("--engine", choices=("piper", "fake"), default="piper")
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--assets", type=Path, default=ASSETS)
    args = ap.parse_args(argv)

    cat = load_catalog(args.catalog)
    if args.check:
        problems = check(cat, args.assets)
        for p in problems:
            print(f"voice-check: {p}", file=sys.stderr)
        if not problems:
            print(f"voice-check: OK ({len(_expected(cat))} generated files)")
        return 1 if problems else 0
    try:
        unknown = set(args.profile) - set(cat.get("profiles") or {})
        if unknown:
            raise CatalogError(f"unknown profile(s): {sorted(unknown)}")
        needs_engine = args.sample or not args.dry_run
        renderer = (
            (FakeRenderer(cat) if args.engine == "fake" else PiperRenderer(cat))
            if needs_engine
            else FakeRenderer(cat)
        )
        if args.sample:
            problems = sample(cat, args.assets, renderer)
            for p in problems:
                print(f"voice-sample: {p}", file=sys.stderr)
            return 1 if problems else 0
        result = generate(
            cat,
            args.assets,
            renderer,
            only=set(args.only) or None,
            profiles=set(args.profile) or None,
            dry_run=args.dry_run,
        )
    except CatalogError as exc:
        print(f"gen_voice: {exc}", file=sys.stderr)
        return 2
    print(f"gen_voice: rendered {result.rendered}, unchanged {result.skipped}, text only {result.refreshed}, pruned {result.pruned}"
          + (" (dry run)" if args.dry_run else ""))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
