-- SPDX-License-Identifier: MIT
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    experiment_id TEXT NOT NULL,
    parent_run_id TEXT,
    uid INTEGER NOT NULL,
    username TEXT NOT NULL,
    resource TEXT NOT NULL,
    description TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    expected_end TEXT,
    ended_at TEXT,
    expected_seconds REAL NOT NULL,
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cwd TEXT NOT NULL,
    command_json TEXT NOT NULL,
    run_dir TEXT NOT NULL,
    unit TEXT NOT NULL,
    exit_code INTEGER,
    service_result TEXT,
    archive_path TEXT,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_resource
ON jobs(resource) WHERE state IN ('running', 'needs_review');
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL
);
                
