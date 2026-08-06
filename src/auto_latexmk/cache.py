import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager

from auto_latexmk.runtime import logger

VALID_ENGINES = {"xelatex", "pdflatex", "lualatex"}
CACHE_LOCKS: dict[str, threading.Lock] = {}
CACHE_LOCKS_GUARD = threading.Lock()


def canonical_tex_path(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/")


class CompilerPrefCache:
    def __init__(self, cache_path=None):
        self._path = cache_path or os.path.join(
            os.path.expanduser("~"),
            ".cache",
            "auto-latexmk",
            "compiler-preferences.json",
        )
        self._data: dict = {}
        self._pending: dict = {}
        self._loaded = False

    def _read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as cache_file:
                data = json.load(cache_file)
                return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Failed to read compiler preferences: %s", error)
            return {}

    def _load(self):
        if self._loaded:
            return
        self._data = self._read()
        self._data.update(self._pending)
        self._loaded = True

    def get(self, path: str) -> str | None:
        self._load()
        entry = self._data.get(canonical_tex_path(path))
        if not isinstance(entry, dict):
            return None
        engine = entry.get("engine")
        return engine if engine in VALID_ENGINES else None

    def set(self, path: str, engine: str):
        if engine not in VALID_ENGINES:
            raise ValueError(f"Unsupported engine: {engine}")
        key = canonical_tex_path(path)
        entry = {"engine": engine, "updated_at": time.time()}
        self._pending[key] = entry
        self._data[key] = entry

    def save(self):
        if not self._pending:
            return
        cache_dir = os.path.dirname(os.path.abspath(self._path))
        temp_path = None
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with _exclusive_file_lock(f"{self._path}.lock"):
                merged = self._read()
                merged.update(self._pending)
                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(self._path)}.",
                    suffix=".tmp",
                    dir=cache_dir,
                )
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                    json.dump(merged, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_path, self._path)
                temp_path = None
                self._data = merged
                self._pending.clear()
                self._loaded = True
        except OSError as error:
            logger.warning("Failed to save compiler preferences: %s", error)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass


@contextmanager
def _exclusive_file_lock(lock_path):
    normalized_path = os.path.abspath(lock_path)
    with CACHE_LOCKS_GUARD:
        process_lock = CACHE_LOCKS.setdefault(normalized_path, threading.Lock())

    with process_lock, open(normalized_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            if os.path.getsize(normalized_path) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
