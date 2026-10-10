-- Independent, versioned data-engineering schema; never resets Trae or business tables.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS content_de_codex;
CREATE TABLE IF NOT EXISTS content_de_codex.releases (
 release_id text PRIMARY KEY, manifest_sha256 text NOT NULL CHECK(length(manifest_sha256)=64),
 status text NOT NULL CHECK(status IN ('staging','active','retired')), metadata jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS content_de_codex.entities (
 release_id text NOT NULL REFERENCES content_de_codex.releases, kind text NOT NULL,
 entity_id text NOT NULL, payload jsonb NOT NULL, payload_sha256 text NOT NULL CHECK(length(payload_sha256)=64),
 PRIMARY KEY(release_id,kind,entity_id)
);
CREATE TABLE IF NOT EXISTS content_de_codex.assets (
 release_id text NOT NULL REFERENCES content_de_codex.releases, asset_id text NOT NULL,
 version integer NOT NULL CHECK(version>0), sha256 text NOT NULL CHECK(length(sha256)=64),
 size bigint NOT NULL CHECK(size>=0), object_key text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,asset_id,version), UNIQUE(release_id,sha256)
);
CREATE TABLE IF NOT EXISTS content_de_codex.editions (
 release_id text NOT NULL REFERENCES content_de_codex.releases, year integer NOT NULL CHECK(year BETWEEN 2010 AND 2025),
 competition_id text NOT NULL CHECK(competition_id='cumcm'), PRIMARY KEY(release_id,year)
);
CREATE TABLE IF NOT EXISTS content_de_codex.problems (
 release_id text NOT NULL, problem_id text NOT NULL, year integer NOT NULL, code text NOT NULL CHECK(code IN ('A','B','C','D','E')),
 title text NOT NULL, profile_version integer NOT NULL, problem_types text[] NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,problem_id), FOREIGN KEY(release_id,year) REFERENCES content_de_codex.editions
);
CREATE TABLE IF NOT EXISTS content_de_codex.subproblems (
 release_id text NOT NULL, subproblem_id text NOT NULL, problem_id text NOT NULL, ordinal integer NOT NULL CHECK(ordinal>0),
 goal text NOT NULL, payload jsonb NOT NULL, PRIMARY KEY(release_id,subproblem_id),
 FOREIGN KEY(release_id,problem_id) REFERENCES content_de_codex.problems
);
CREATE TABLE IF NOT EXISTS content_de_codex.papers (
 release_id text NOT NULL, case_id text NOT NULL, year integer NOT NULL, problem_id text, source_sha256 text NOT NULL,
 title text NOT NULL, relation_status text NOT NULL, profile_version integer NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,case_id), FOREIGN KEY(release_id,year) REFERENCES content_de_codex.editions,
 FOREIGN KEY(release_id,problem_id) REFERENCES content_de_codex.problems,
 FOREIGN KEY(release_id,source_sha256) REFERENCES content_de_codex.assets(release_id,sha256)
);
CREATE TABLE IF NOT EXISTS content_de_codex.paper_problem_links (
 release_id text NOT NULL, case_id text NOT NULL, problem_id text, status text NOT NULL, evidence jsonb NOT NULL,
 PRIMARY KEY(release_id,case_id), FOREIGN KEY(release_id,case_id) REFERENCES content_de_codex.papers,
 FOREIGN KEY(release_id,problem_id) REFERENCES content_de_codex.problems
);
CREATE TABLE IF NOT EXISTS content_de_codex.units (
 release_id text NOT NULL REFERENCES content_de_codex.releases, unit_id text NOT NULL,
 competition_id text NOT NULL CHECK(competition_id='cumcm'), year integer CHECK(year BETWEEN 2010 AND 2025),
 kind text NOT NULL, stage text NOT NULL, problem_id text, case_id text, text_content text NOT NULL,
 source_sha256 text, external_consumer_allowed boolean NOT NULL DEFAULT false,
 embedding vector(384) NOT NULL, embedding_model text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,unit_id),
 FOREIGN KEY(release_id,source_sha256) REFERENCES content_de_codex.assets(release_id,sha256),
 FOREIGN KEY(release_id,problem_id) REFERENCES content_de_codex.problems,
 FOREIGN KEY(release_id,case_id) REFERENCES content_de_codex.papers
);
CREATE INDEX IF NOT EXISTS codex_units_filter ON content_de_codex.units(release_id,competition_id,stage,year);
CREATE TABLE IF NOT EXISTS content_de_codex.activation (
 competition_id text PRIMARY KEY CHECK(competition_id='cumcm'), release_id text NOT NULL REFERENCES content_de_codex.releases,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS content_de_codex.release_events (
 event_id bigserial PRIMARY KEY, competition_id text NOT NULL, previous_release text,
 next_release text NOT NULL REFERENCES content_de_codex.releases, event_kind text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
