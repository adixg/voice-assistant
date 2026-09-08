"""CUDA library discovery, kept out of the STT backend because it is a
process-level concern, not a speech one: it has to run before any CUDA
library is dlopen'd, which means before the backend modules are even
constructed.
"""
import os
import sys

_REEXEC_GUARD = "_VOICEPIPE_CUDA_REEXEC"


def ensure_cuda_libs():
    """Put the nvidia-*-cu12 wheel lib dirs on LD_LIBRARY_PATH, re-execing once.

    ctranslate2 (faster-whisper's runtime) dlopen's libcublas/libcudnn from
    those wheels, but only finds them if they were on LD_LIBRARY_PATH at
    process start — setting the variable from inside Python is too late. So
    set it and hand the process over to a fresh interpreter.

    A no-op when the libs aren't installed (CPU-only machine), when we have
    already re-execed, or when argv[0] isn't a real file (`python -c`, a
    REPL, or pytest's runner — where re-execing would hijack the test run).
    """
    if os.environ.get(_REEXEC_GUARD) or not os.path.isfile(sys.argv[0]):
        return

    import importlib.util
    import pathlib

    dirs = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            lib = pathlib.Path(spec.submodule_search_locations[0]) / "lib"
            if lib.is_dir():
                dirs.append(str(lib))
    if not dirs:
        return

    current = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + ([current] if current else []))
    os.environ[_REEXEC_GUARD] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])
