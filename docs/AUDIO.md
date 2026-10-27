# Audio hardware and ALSA guide

How to get sound out of the audio container. Behaviour (music, tempo, channels, formats, assets, RPCs) is specified in
[UC-031](use_cases/UC-031.md) and [services.md section 6, audio](specs/services.md#audio); the `play_audio` and
`menu_voice` flags are in [flags.md](specs/flags.md). The audio service plays through ALSA only, so it needs a sound card
visible under `/dev/snd` on the host.

## Hardware

| Host | Output |
|------|--------|
| Raspberry Pi 5 | No 3.5mm jack: use a USB audio adapter. |
| Raspberry Pi 4 and earlier | The built-in jack or a USB adapter. |
| HDMI | Works, but only if a monitor or TV is attached; not suited to a party setup. |

## How the container gets the card

- Compose passes `/dev/snd` through (`devices: /dev/snd:/dev/snd`) and adds the host `audio` group by number
  (`group_add: "29"` in `docker-compose.yml`). If your host `audio` group has another GID (`getent group audio`), change it.
  `docker-compose.lite.yml` runs the container `privileged` instead.
- `ALSA_CARD` defaults to `auto` (compose). At start-up `services/audio/alsa_config.py` lists the cards, prefers the first one
  whose name contains "USB" and otherwise takes the first card (on a Pi often an HDMI card, so set `ALSA_CARD` when the
  speakers are on the jack). It retries up to 3 times, one second apart, because `/dev/snd` can fill late, and falls
  back to card 0.
- The service then rewrites `/etc/asound.conf` with that card: a `dmix` software mixer (44.1 kHz, 2 channels) as the
  default PCM. The image ships a template with `hw:0,0` that the rewrite replaces; the file is owned by the non-root user
  (uid 65532) so it can be rewritten.
- To force a card: `ALSA_CARD=1` (a card number). To use your own config instead, mount it read-only at
  `/etc/asound.conf`; the rewrite is then skipped with a warning when the file is not writable.
- Music opens the PCM through a fallback chain: `default` (dmix), then `plughw:N,0`, then `hw:N,0`, so a stale dmix after a
  crash or a permission problem does not mute the game.
- Test and CI stacks (`docker-compose.ci.yml`) mount no sound device and run with `play_audio` off.

## Muting

`play_audio` off makes the service accept every request and play nothing. Turning it on later opens the hardware at runtime
(UC-031 A1). It is a flag, not an environment variable; see flags.md.

## Troubleshooting

1. No sound: run `aplay -l` on the host (the runtime image has no shell or ALSA tools), check the adapter is plugged in and
   `/dev/snd` exists, then `docker compose logs audio` for "Detected ALSA cards", "Wrote /etc/asound.conf with card N" and
   "Opened ALSA PCM device".
2. Wrong output (for example the Pi 4 jack while no USB card is present, so an HDMI card was picked): set `ALSA_CARD` to the number from
   `aplay -l` and restart the audio container.
3. "Permission denied" on `/dev/snd/*`: the `group_add` GID does not match the host `audio` group.
4. Cards that appear after the container started (USB hot-plug): restart the audio container so the card is detected again.
5. Check `play_audio` is on and the volume flags are not zero.
6. Choppy music during tempo changes is CPU load from resampling; check `docker stats` on a loaded Pi.
7. Custom music and sounds, formats and limits: see UC-031 BR-003 and services.md.

## Voice lines: catalog and generation

Every voice line is text in one catalog, `services/audio/voice/lines.yaml` (UC-031 BR-007, FR-039, #2107). The
generator renders each line for the generated voice profiles `gen_male` and `gen_female` (one pinned Piper voice each,
run through sherpa-onnx) and commits the files next to the recordings, so nothing synthesises speech on the stage Pi
(C-050). The recordings stay as the profiles `aaron` and `ivy`; the generator never writes them.

| Command | What it does |
|---|---|
| `make voice` | Renders every line whose input changed into `services/audio/assets/<Area>/vox/gen_*/<id>.ogg` and updates `services/audio/assets/voice_manifest.json`. The first run downloads the two voice models (about 65 MB each) to `~/.cache/joustmania/voice-models` and checks their pinned sha256. |
| `make voice-check` | Fails when the catalog, the manifest and the files disagree: a line without a file, a file edited by hand, a catalog edit without `make voice`, a stray file in a `gen_*` directory. No TTS, no network; CI runs it. |
| `make voice-sample` | Renders the catalog's `sample` lines again and compares duration (±10 %) and loudness (±1 LU) with the committed files. Piper output is not bit-reproducible, so this is the reproducibility check. |

To add or change a line: edit `lines.yaml` (`id` = the file stem and the `Sound` value, `text`, optional `say` respelling
when the synthesizer mispronounces a word), run `make voice`, listen to the new files, commit the catalog, the manifest
and the files together. `scripts/tools/gen-voice.sh --only <id> --dry-run` shows what would be rendered.

- Dev machine only: the wrapper needs `uv` and pins its own Python 3.12 and tool packages (sherpa-onnx, numpy, soxr,
  pyloudnorm, imageio-ffmpeg for a pinned ffmpeg); `FFMPEG` overrides the ffmpeg binary.
- Format: 44.1 kHz stereo Ogg Vorbis, silence trimmed, normalised to the median loudness of the `ivy` Menu recordings
  (`loudness.target_lufs`), encoded with ffmpeg `+bitexact` so an unchanged input never changes the committed bytes.
- Licences: the generated files are CC BY-SA 4.0 like the other audio assets (C-017); the voice models and their
  sources are listed in `services/audio/assets/audio_credits.txt`.
- Scope: every voice line the code plays (Menu, Joust, Zombie), the #2106 pad lines and the mode instructions of the
  FR-014 backlog (#2151). Recordings no caller plays are
  not catalogued (`NOT_CATALOGED` in `services/audio/tests/test_voice_catalog.py`); a new caller of one adds its line.
  The recordings no code path played at all (20 lines in `aaron` and `ivy`: unused Menu lines, Commander `red shift`,
  Fight Club `defender_winning`) were deleted (#2166); `DELETED_RECORDINGS` in the same test keeps them gone.
- Playback: `menu_voice` selects any profile of the manifest (`aaron`, `ivy`, `gen_male`, `gen_female`; default `ivy`).
  A line the selected profile lacks plays from its partner (`gen_male` <-> `aaron`, `gen_female` <-> `ivy`), so with
  `ivy` the pad labels play in the `gen_female` voice before Ivy's recorded value (UC-031 BR-007, #2107 part 2).
  Check an id for one voice with `python -m services.audio.sound_catalog --voice gen_female label_sensitivity`.
