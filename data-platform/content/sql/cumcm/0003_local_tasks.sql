-- User tasks are isolated from the historical editions and template corpus.
CREATE SCHEMA IF NOT EXISTS content_tasks_local;
CREATE TABLE IF NOT EXISTS content_tasks_local.tasks (
 owner_id text NOT NULL, task_id text NOT NULL, problem_sha256 text NOT NULL CHECK(problem_sha256 ~ '^[0-9a-f]{64}$'),
 release_id text NOT NULL REFERENCES content_de_codex.releases, payload jsonb NOT NULL,
 PRIMARY KEY(owner_id,task_id)
);
CREATE TABLE IF NOT EXISTS content_tasks_local.assets (
 owner_id text NOT NULL, task_id text NOT NULL, asset_id text NOT NULL, version integer NOT NULL CHECK(version>0),
 sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'), object_key text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(owner_id,task_id,asset_id,version), FOREIGN KEY(owner_id,task_id) REFERENCES content_tasks_local.tasks
);
CREATE TABLE IF NOT EXISTS content_tasks_local.results (
 owner_id text NOT NULL, task_id text NOT NULL, result_card_id text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(owner_id,task_id,result_card_id), FOREIGN KEY(owner_id,task_id) REFERENCES content_tasks_local.tasks
);
CREATE TABLE IF NOT EXISTS content_tasks_local.nodes (
 owner_id text NOT NULL, task_id text NOT NULL, node_id text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(owner_id,task_id,node_id), FOREIGN KEY(owner_id,task_id) REFERENCES content_tasks_local.tasks
);
