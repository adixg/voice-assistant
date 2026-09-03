"""Shared test setup.

Both chat_loop.py and bridge_server.py call voicepipe.stt.ensure_cuda_libs()
at import time, which re-execs the process (os.execv) if it finds CUDA libs
that need adding to LD_LIBRARY_PATH. That's fine for those entrypoints but
would hijack the pytest process itself the moment a test imports
bridge_server. Setting its re-exec guard env var before any test module
imports bridge_server makes it a no-op here.
"""
import os

os.environ["_VOICEPIPE_CUDA_REEXEC"] = "1"
