-- 国赛 2010—2025 数据交付 v1：content_de 命名空间（独立，不改既有业务表）
-- PostgreSQL 16 + pgvector. 主体身份与内容版本分离；release_items 引用确定版本。

BEGIN;

CREATE SCHEMA IF NOT EXISTS content_de;

CREATE EXTENSION IF NOT EXISTS vector;

-- 届次
CREATE TABLE content_de.editions (
    edition_id       TEXT PRIMARY KEY,
    competition_id   TEXT NOT NULL,
    year             INT  NOT NULL CHECK (year >= 1900 AND year <= 2100),
    track            TEXT,
    official_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT ed_comp_year UNIQUE (competition_id, year, track)
);

-- 赛题
CREATE TABLE content_de.problems (
    problem_id     TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL,
    edition_id     TEXT REFERENCES content_de.editions(edition_id),
    code           TEXT,
    title          TEXT,
    identity_status TEXT NOT NULL DEFAULT 'pending_review',
    review_status  TEXT NOT NULL DEFAULT 'pending_review',
    source_asset_ref JSONB,
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 小问
CREATE TABLE content_de.subproblems (
    subproblem_id  TEXT PRIMARY KEY,
    problem_id     TEXT NOT NULL REFERENCES content_de.problems(problem_id) DEFERRABLE,
    requirement    TEXT,
    deliverables   JSONB NOT NULL DEFAULT '[]'::jsonb,
    constraints    JSONB NOT NULL DEFAULT '[]'::jsonb,
    dependencies   JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence       JSONB,
    schema_version INT NOT NULL DEFAULT 2
);

-- 论文
CREATE TABLE content_de.papers (
    case_id        TEXT PRIMARY KEY,
    problem_id     TEXT REFERENCES content_de.problems(problem_id) DEFERRABLE,
    title          TEXT,
    award_evidence JSONB,
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
    identity_status TEXT NOT NULL DEFAULT 'pending_review',
    version        INT NOT NULL DEFAULT 1,
    source_asset_ref JSONB
);

-- 论文-父题关联
CREATE TABLE content_de.paper_problem_links (
    link_id       TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES content_de.papers(case_id),
    problem_id    TEXT NOT NULL REFERENCES content_de.problems(problem_id),
    relation_status TEXT NOT NULL CHECK (relation_status IN ('candidate','confirmed','rejected')),
    evidence      JSONB
);

-- 资产
CREATE TABLE content_de.assets (
    asset_id     TEXT NOT NULL,
    version      INT  NOT NULL CHECK (version >= 1),
    role         TEXT NOT NULL,
    competition_id TEXT,
    object_key   TEXT NOT NULL,
    sha256       TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes   BIGINT NOT NULL CHECK (size_bytes >= 0),
    media_type   TEXT,
    license      JSONB NOT NULL DEFAULT '{"internal_analysis":false,"distribute_original":false,"distribute_profile":false,"send_to_third_party_model":false}'::jsonb,
    PRIMARY KEY (asset_id, version),
    UNIQUE (asset_id, version, sha256)
);

-- 解析运行
CREATE TABLE content_de.parse_runs (
    parse_run_id  TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL,
    source_version INT NOT NULL,
    parser_tool   TEXT,
    parser_version TEXT,
    model_id      TEXT,
    config_fingerprint TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    FOREIGN KEY (source_asset_id, source_version) REFERENCES content_de.assets(asset_id, version)
);

-- 结构块
CREATE TABLE content_de.blocks (
    block_id      TEXT PRIMARY KEY,
    parse_run_id  TEXT NOT NULL REFERENCES content_de.parse_runs(parse_run_id),
    parent_section_id TEXT,
    level         INT CHECK (level IS NULL OR level >= 0),
    ord           INT NOT NULL CHECK (ord >= 1),
    block_type    TEXT NOT NULL,
    page          INT NOT NULL CHECK (page >= 1),
    printed_page  INT,
    text          TEXT,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    locations     JSONB NOT NULL DEFAULT '[]'::jsonb,
    review_status TEXT NOT NULL DEFAULT 'pending_review'
);
CREATE INDEX idx_blocks_parse ON content_de.blocks(parse_run_id);

-- 块-小问多对多
CREATE TABLE content_de.block_subproblem_links (
    link_id       TEXT PRIMARY KEY,
    block_id      TEXT NOT NULL REFERENCES content_de.blocks(block_id),
    subproblem_id TEXT NOT NULL REFERENCES content_de.subproblems(subproblem_id),
    role          TEXT,
    relation_status TEXT NOT NULL CHECK (relation_status IN ('candidate','confirmed','rejected')),
    evidence      JSONB
);

-- 检索单元
CREATE TABLE content_de.retrieval_units (
    unit_id   TEXT NOT NULL,
    version   INT  NOT NULL CHECK (version >= 1),
    case_id   TEXT NOT NULL REFERENCES content_de.papers(case_id),
    problem_id TEXT,
    stage     TEXT,
    role      TEXT,
    retrieval_text TEXT NOT NULL,
    quality_status TEXT NOT NULL DEFAULT 'pending_review',
    usage_status TEXT NOT NULL DEFAULT 'blocked',
    PRIMARY KEY (unit_id, version)
);
CREATE INDEX idx_units_case ON content_de.retrieval_units(case_id, stage);

CREATE TABLE content_de.unit_members (
    unit_id  TEXT NOT NULL,
    unit_version INT NOT NULL,
    block_id TEXT NOT NULL REFERENCES content_de.blocks(block_id),
    ord      INT NOT NULL CHECK (ord >= 1),
    PRIMARY KEY (unit_id, unit_version, block_id),
    FOREIGN KEY (unit_id, unit_version) REFERENCES content_de.retrieval_units(unit_id, version)
);

-- embedding
CREATE TABLE content_de.embedding_profiles (
    profile_id    TEXT PRIMARY KEY,
    model_id      TEXT NOT NULL,
    dimension     INT  NOT NULL CHECK (dimension > 0),
    preprocess_version TEXT,
    unit_version  INT,
    UNIQUE (model_id, dimension)
);

CREATE TABLE content_de.embeddings (
    unit_id    TEXT NOT NULL,
    unit_version INT NOT NULL,
    profile_id TEXT NOT NULL REFERENCES content_de.embedding_profiles(profile_id),
    embedding  vector(1536),
    pre_text_hash TEXT,
    PRIMARY KEY (unit_id, unit_version, profile_id),
    FOREIGN KEY (unit_id, unit_version) REFERENCES content_de.retrieval_units(unit_id, version)
);

-- 发布
CREATE TABLE content_de.releases (
    release_id   TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL,
    version      INT NOT NULL,
    published_status TEXT NOT NULL DEFAULT 'staging',
    note         TEXT,
    UNIQUE (release_id, version)
);

CREATE TABLE content_de.release_items (
    release_id TEXT NOT NULL,
    asset_id   TEXT NOT NULL,
    version    INT  NOT NULL,
    role       TEXT,
    change     TEXT NOT NULL CHECK (change IN ('add','update','keep','remove')),
    PRIMARY KEY (release_id, asset_id, version),
    FOREIGN KEY (asset_id, version) REFERENCES content_de.assets(asset_id, version)
);

-- 审核证据
CREATE TABLE content_de.reviews (
    review_id     TEXT PRIMARY KEY,
    object_id     TEXT NOT NULL,
    object_version INT,
    property_paths JSONB,
    source_asset_ref JSONB,
    candidate     JSONB,
    conclusion    TEXT,
    reviewer_type TEXT NOT NULL CHECK (reviewer_type IN ('human','agent_visual','rule','content_match','unknown')),
    review_status TEXT NOT NULL DEFAULT 'pending_review',
    reviewed_at   TEXT
);

COMMIT;
