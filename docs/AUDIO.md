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
