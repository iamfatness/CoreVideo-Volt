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
  deleted_at TIMESTAMPTZ,
  archived_at TIMESTAMPTZ
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
  audio_sample_rate INTEGER,
  tier TEXT,
  archive_key TEXT,
  archive_sha256 TEXT
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
  held_at TIMESTAMPTZ,
  slot_seconds INTEGER,
  recurrence TEXT,
  recurrence_of TEXT,
  occurrence_on TEXT,
  read_wpm INTEGER
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
  as_run_out_precise TEXT,
  schedule_mode TEXT,
  schedule_offset_seconds INTEGER,
  story_status TEXT,
  script_rev INTEGER,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  approved_rev INTEGER,
  floated BOOLEAN,
  source_wire_id TEXT
);

CREATE TABLE IF NOT EXISTS story_rev (
  item_id TEXT NOT NULL,
  rev INTEGER NOT NULL,
  slug TEXT NOT NULL,
  script TEXT,
  actor TEXT,
  at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (item_id, rev)
);

CREATE TABLE IF NOT EXISTS story_event (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  at TIMESTAMPTZ NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  from_value TEXT,
  to_value TEXT,
  rev INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS wire_story (
  id TEXT PRIMARY KEY,
  feed TEXT NOT NULL,
  external_id TEXT NOT NULL,
  headline TEXT NOT NULL,
  body TEXT,
  byline TEXT,
  published_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL,
  item_id TEXT,
  UNIQUE (feed, external_id)
);

CREATE TABLE IF NOT EXISTS story_template (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  body TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS story_archive (
  id TEXT PRIMARY KEY,
  original_item_id TEXT,
  show_title TEXT,
  slug TEXT NOT NULL,
  script TEXT,
  story_status TEXT,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  aired_at TIMESTAMPTZ,
  archived_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS mos_sync (
  device TEXT NOT NULL,
  show_id TEXT NOT NULL,
  state TEXT NOT NULL,
  acked_hash TEXT,
  error TEXT,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (device, show_id)
);

CREATE TABLE IF NOT EXISTS mos_counter (
  device TEXT PRIMARY KEY,
  next_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mos_device (
  device TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  detail TEXT,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS device_status (
  device TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  observed TEXT,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS device_event (
  id TEXT PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL,
  device TEXT NOT NULL,
  command TEXT NOT NULL,
  params TEXT,
  outcome TEXT NOT NULL,
  detail TEXT,
  cue_id TEXT,
  actor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_safety (
  id INTEGER PRIMARY KEY,
  safe_mode INTEGER NOT NULL,
  reason TEXT,
  set_by TEXT,
  set_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS macro (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  version INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_step (
  macro_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  position INTEGER NOT NULL,
  room TEXT NOT NULL,
  command TEXT NOT NULL,
  payload TEXT NOT NULL,
  offset_ms INTEGER NOT NULL,
  group_key TEXT,
  PRIMARY KEY (macro_id, version, position)
);

CREATE TABLE IF NOT EXISTS device_confirm (
  id TEXT PRIMARY KEY,
  cue_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL
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
  fail_detail TEXT,
  offset_ms INTEGER,
  preroll_ms INTEGER,
  group_key TEXT,
  due_at TIMESTAMPTZ,
  claimed_at TIMESTAMPTZ,
  preload_status TEXT,
  preload_detail TEXT,
  preloaded_at TIMESTAMPTZ
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

CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS volt_user (
  name TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL,
  disabled_at TIMESTAMPTZ,
  email TEXT,
  display_name TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS volt_group (
  name TEXT PRIMARY KEY,
  role TEXT,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS group_member (
  group_name TEXT NOT NULL,
  user_name TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (group_name, user_name)
);

CREATE TABLE IF NOT EXISTS session (
  id_hash TEXT PRIMARY KEY,
  user_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  csrf TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS session_by_user ON session (user_name);
