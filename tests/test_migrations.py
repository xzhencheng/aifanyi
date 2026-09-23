"""A shared database must not make Alembic propose deleting LangGraph state."""

import os
import sqlite3
import subprocess
import sys


def test_checkpoint_tables_are_separately_managed_and_other_drift_is_detected(tmp_path):
    database = tmp_path / "migrations.db"
    env = {
        **os.environ,
        "AIFANYI_ENVIRONMENT": "test",
        "AIFANYI_DEV_AUTH": "false",
        "AIFANYI_DATABASE_URL": f"sqlite:///{database}",
        "AIFANYI_CHECKPOINT_URL": f"sqlite:///{tmp_path}/checkpoints.db",
    }
    migrated = subprocess.run(
        [sys.executable, "-m", "aifanyi.cli", "migrate"], env=env, capture_output=True, text=True
    )
    assert migrated.returncode == 0, migrated.stderr
    with sqlite3.connect(database) as conn:
        for table in ["checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"]:
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    checked = subprocess.run(
        [sys.executable, "-m", "alembic", "check"], env=env, capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE unexpected_business_table (id TEXT PRIMARY KEY)")
    drift = subprocess.run(
        [sys.executable, "-m", "alembic", "check"], env=env, capture_output=True, text=True
    )
    assert drift.returncode != 0
    assert "unexpected_business_table" in drift.stdout
