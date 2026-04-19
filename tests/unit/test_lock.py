"""Tests for the per-DB advisory file lock."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bread.core.lock import DatabaseLock, DatabaseLockedError


def test_acquire_creates_lock_file_with_pid(tmp_path: Path) -> None:
    db = tmp_path / "bread-paper.db"
    lock = DatabaseLock(db)
    lock.acquire()
    try:
        lock_file = tmp_path / "bread-paper.db.lock"
        assert lock_file.exists()
        assert lock_file.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_release_removes_lock_file(tmp_path: Path) -> None:
    db = tmp_path / "bread-paper.db"
    lock = DatabaseLock(db)
    lock.acquire()
    lock.release()
    assert not (tmp_path / "bread-paper.db.lock").exists()


def test_second_acquire_raises_database_locked(tmp_path: Path) -> None:
    db = tmp_path / "bread-paper.db"
    a = DatabaseLock(db)
    a.acquire()
    try:
        b = DatabaseLock(db)
        with pytest.raises(DatabaseLockedError):
            b.acquire()
    finally:
        a.release()


def test_different_modes_do_not_collide(tmp_path: Path) -> None:
    paper = DatabaseLock(tmp_path / "bread-paper.db")
    live = DatabaseLock(tmp_path / "bread-live.db")
    paper.acquire()
    live.acquire()
    try:
        assert (tmp_path / "bread-paper.db.lock").exists()
        assert (tmp_path / "bread-live.db.lock").exists()
    finally:
        paper.release()
        live.release()


def test_release_then_reacquire_succeeds(tmp_path: Path) -> None:
    db = tmp_path / "bread-paper.db"
    a = DatabaseLock(db)
    a.acquire()
    a.release()
    b = DatabaseLock(db)
    b.acquire()
    try:
        assert (tmp_path / "bread-paper.db.lock").exists()
    finally:
        b.release()
