# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for PS Move Pairing Daemon.

Builds a single-file executable that bundles:
- The psmove_pairing Python package
- psmoveapi SWIG bindings (_psmove.so, psmove.py, libpsmoveapi.so)
- OpenTelemetry, gRPC, protobuf, and prometheus-client dependencies

Build inside Docker (scripts/pairing-daemon/Dockerfile.build) to ensure
matching glibc and Python ABI with the psmove-builder artifacts.
"""

import os

# psmove artifacts are staged here by Dockerfile.build
PSMOVE_DIR = os.environ.get("PSMOVE_DIR", "/psmove")

a = Analysis(
    ["psmove_pairing_daemon.py"],
    pathex=["."],
    binaries=[
        (os.path.join(PSMOVE_DIR, "_psmove.so"), "."),
        (os.path.join(PSMOVE_DIR, "libpsmoveapi.so"), "."),
    ],
    datas=[
        (os.path.join(PSMOVE_DIR, "psmove.py"), "."),
        ("psmove_pairing", "psmove_pairing"),
    ],
    hiddenimports=[
        # OpenTelemetry - uses entry_points/dynamic imports extensively
        "opentelemetry",
        "opentelemetry.sdk",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.resources",
        "opentelemetry.exporter.otlp",
        "opentelemetry.exporter.otlp.proto",
        "opentelemetry.exporter.otlp.proto.grpc",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.exporter",
        # gRPC
        "grpc",
        "grpc._cython",
        "grpc._cython.cygrpc",
        "grpc.experimental",
        # Protobuf - needed by OTLP exporter
        "google.protobuf",
        "google.protobuf.descriptor",
        "google.protobuf.descriptor_pool",
        "google.protobuf.runtime_version",
        "google.protobuf.symbol_database",
        "google.protobuf.message",
        "google.protobuf.json_format",
        # OTLP proto generated modules
        "opentelemetry.proto",
        "opentelemetry.proto.common",
        "opentelemetry.proto.common.v1",
        "opentelemetry.proto.common.v1.common_pb2",
        "opentelemetry.proto.resource",
        "opentelemetry.proto.resource.v1",
        "opentelemetry.proto.resource.v1.resource_pb2",
        "opentelemetry.proto.trace",
        "opentelemetry.proto.trace.v1",
        "opentelemetry.proto.trace.v1.trace_pb2",
        "opentelemetry.proto.collector",
        "opentelemetry.proto.collector.trace",
        "opentelemetry.proto.collector.trace.v1",
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2",
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc",
        # Prometheus client
        "prometheus_client",
        "prometheus_client.values",
        "prometheus_client.exposition",
        "prometheus_client.metrics",
        "prometheus_client.metrics_core",
        "prometheus_client.samples",
        "prometheus_client.registry",
        "prometheus_client.platform_collector",
        "prometheus_client.gc_collector",
        "prometheus_client.process_collector",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI / desktop — not needed for a headless daemon
        "tkinter",
        "_tkinter",
        "turtle",
        # Data science — never imported
        "matplotlib",
        "numpy",
        "PIL",
        "scipy",
        "pandas",
        "IPython",
        "jupyter",
        "notebook",
        # Testing / dev tools
        "test",
        "unittest",
        "doctest",
        "pydoc",
        "pdb",
        "profile",
        "cProfile",
        "pstats",
        # Unused stdlib modules — daemon only needs asyncio, os, re, logging, shutil
        "xmlrpc",
        "xml.etree",
        "xml.dom",
        "xml.sax",
        "xml.parsers",
        "sqlite3",
        "dbm",
        "curses",
        "lib2to3",
        "ensurepip",
        "venv",
        "idlelib",
        "turtledemo",
        "distutils",
        "setuptools",
        "pkg_resources",
        "pip",
        "multiprocessing",
        "concurrent",
        "ctypes.test",
        "email.mime",
        "html.parser",
        "http.cookiejar",
        "ftplib",
        "imaplib",
        "smtplib",
        "poplib",
        "nntplib",
        "telnetlib",
        "socketserver",
        "zipapp",
        "compileall",
        "py_compile",
        # OTEL exporters we don't use (only use grpc trace exporter)
        "opentelemetry.exporter.otlp.proto.http",
        "opentelemetry.exporter.otlp.proto.common",
        "opentelemetry.exporter.otlp.proto.grpc._metric_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="psmove-pairing-daemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[
        # Don't compress gRPC native lib — can cause runtime issues
        "cygrpc.cpython*.so",
    ],
    # Use systemd RuntimeDirectory to avoid /tmp issues on locked-down systems
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
)
