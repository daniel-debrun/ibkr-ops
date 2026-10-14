"""Crash-safe file output.

* ``atomic_write_*``: write to a temporary file in the target directory, fsync, then ``os.replace``; readers
  observe either the previous or the new file, never a partial one.
* ``append_jsonl``: one ``O_APPEND`` write per record, for append-only ledgers.
* ``latest_by``: the most recent record per key (IB may reuse the id of a rejected order).
* ``run_stamp``: UTC timestamps for immutable per-run file names.
"""
from __future__ import annotations

import csv
import io as _io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def atomic_write_bytes(path: os.PathLike, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def atomic_write_text(path: os.PathLike, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_csv(path: os.PathLike, rows: Iterable[dict], columns: list[str]) -> Path:
    buf = _io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return atomic_write_text(path, buf.getvalue())


def append_jsonl(path: os.PathLike, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: os.PathLike) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                       # a torn last line from a killed writer
    return out


def latest_by(records: Iterable[dict], key: str) -> dict:
    out: dict = {}
    for r in records:
        out[r[key]] = r
    return out


def run_stamp(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")
