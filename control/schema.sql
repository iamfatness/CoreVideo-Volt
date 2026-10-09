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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS span (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  kind TEXT NOT NULL,
  tc_in TEXT NOT NULL,
  tc_out TEXT NOT NULL,
  text TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
  UNIQUE (asset_id, type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS show (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS item (
  id TEXT PRIMARY KEY,
  show_id TEXT NOT NULL REFERENCES show(id),
  position INTEGER NOT NULL,
  slug TEXT NOT NULL,
  script TEXT,
  status TEXT NOT NULL,
  span_id TEXT REFERENCES span(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS version (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  span_id TEXT NOT NULL REFERENCES span(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (asset_id, span_id, kind)
);
