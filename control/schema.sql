-- Same objects as the SQLite contract. Production store.

CREATE TABLE IF NOT EXISTS asset (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  retention_class TEXT NOT NULL,
  legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
  cleared BOOLEAN NOT NULL DEFAULT FALSE,
  show_name TEXT,
  room TEXT,
  camera TEXT,
  source_key TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS essence (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  role TEXT NOT NULL,
  location TEXT NOT NULL,
  checksum TEXT,
  codec TEXT,
  frame_rate TEXT,
  timecode_start TEXT,
  audio_layout TEXT,
  open BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  duration_frames INTEGER,
  timecode_source TEXT,
  video_profile TEXT,
  pix_fmt TEXT,
  audio_codec TEXT,
  audio_sample_rate INTEGER
);

CREATE TABLE IF NOT EXISTS span (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  kind TEXT NOT NULL,
  tc_in TEXT NOT NULL,
  tc_out TEXT NOT NULL,
  text TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  label TEXT,
  origin TEXT,
  run_id TEXT,
  frame_in INTEGER,
  frame_out INTEGER
);

CREATE TABLE IF NOT EXISTS job (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_by TEXT,
  lease_expires_at TIMESTAMPTZ,
  run_after TIMESTAMPTZ,
  payload TEXT,
  UNIQUE (asset_id, type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS asr_run (
  job_id TEXT PRIMARY KEY REFERENCES job(id),
  asset_id TEXT NOT NULL REFERENCES asset(id),
  engine TEXT NOT NULL,
  model TEXT,
  device TEXT,
  compute_type TEXT,
  vocabulary_requested TEXT NOT NULL,
  vocabulary_used TEXT NOT NULL,
  corrections_applied TEXT NOT NULL,
  word_count INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  settings TEXT
);

CREATE TABLE IF NOT EXISTS asr_chunk (
  job_id TEXT NOT NULL REFERENCES job(id),
  idx INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  own_start DOUBLE PRECISION NOT NULL,
  own_end DOUBLE PRECISION NOT NULL,
  words TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS shot_run (
  job_id TEXT PRIMARY KEY REFERENCES job(id),
  asset_id TEXT NOT NULL REFERENCES asset(id),
  essence_id TEXT NOT NULL REFERENCES essence(id),
  engine TEXT NOT NULL,
  engine_version TEXT,
  threshold DOUBLE PRECISION,
  min_frames INTEGER,
  shot_count INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  settings TEXT
);

CREATE TABLE IF NOT EXISTS show (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  hold_reason TEXT,
  held_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS item (
  id TEXT PRIMARY KEY,
  show_id TEXT NOT NULL REFERENCES show(id),
  position INTEGER NOT NULL,
  slug TEXT NOT NULL,
  script TEXT,
  status TEXT NOT NULL,
  span_id TEXT REFERENCES span(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  as_run_in TIMESTAMPTZ,
  as_run_out TIMESTAMPTZ,
  scheduled_at TIMESTAMPTZ,
  schedule_state TEXT,
  schedule_note TEXT,
  schedule_claimed_at TIMESTAMPTZ,
  as_run_in_precise TEXT,
  as_run_out_precise TEXT
);

CREATE TABLE IF NOT EXISTS version (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  span_id TEXT NOT NULL REFERENCES span(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  storage_backend TEXT,
  storage_key TEXT,
  storage_sha256 TEXT,
  withdrawn_at TIMESTAMPTZ,
  storage_deleted_at TIMESTAMPTZ,
  UNIQUE (asset_id, span_id, kind)
);

CREATE TABLE IF NOT EXISTS cue (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES item(id),
  position INTEGER NOT NULL,
  room TEXT NOT NULL,
  command TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fail_status TEXT,
  fail_detail TEXT
);

CREATE TABLE IF NOT EXISTS purge_audit (
  id TEXT PRIMARY KEY,
  purged_at TIMESTAMPTZ NOT NULL,
  asset_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT,
  role TEXT,
  location TEXT,
  outcome TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS runner_heartbeat (
  name TEXT PRIMARY KEY,
  pid INTEGER,
  host TEXT,
  started_at TIMESTAMPTZ NOT NULL,
  beat_at TIMESTAMPTZ NOT NULL
);
