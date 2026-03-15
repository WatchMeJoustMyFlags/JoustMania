"""
OTEL push metrics for Audio Service.

Tracks system resources, gRPC request performance, and audio playback.
"""

from lib.otel_metrics import Counter, Gauge, Histogram

# System metrics
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")

process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")

process_threads = Gauge("process_threads", "Number of active threads")

# gRPC metrics
grpc_requests_total = Counter("grpc_requests_total", "Total gRPC requests received", ["method", "status"])

grpc_request_duration_seconds = Histogram(
    "grpc_request_duration_seconds",
    "gRPC request duration",
    ["method"],
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0],
)

# Audio metrics
audio_sounds_played_total = Counter(
    "audio_sounds_played_total",
    "Total sound effects played",
    ["result"],  # "success", "no_channel", "file_not_found", "error"
)

audio_playback_errors_total = Counter(
    "audio_playback_errors_total",
    "Total audio playback errors on channels",
)

audio_channels_active = Gauge(
    "audio_channels_active",
    "Number of channels currently playing audio",
)

audio_channel_saturation_total = Counter(
    "audio_channel_saturation_total",
    "Times all channels were busy when a sound was requested",
)

audio_music_playing = Gauge(
    "audio_music_playing",
    "Whether background music is currently playing (1=yes, 0=no)",
)

audio_music_tempo = Gauge(
    "audio_music_tempo",
    "Current music playback tempo (1.0=normal)",
)
