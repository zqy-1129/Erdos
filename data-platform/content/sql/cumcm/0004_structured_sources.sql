CREATE TABLE IF NOT EXISTS content_de_codex.pages (
 release_id text NOT NULL, source_sha256 text NOT NULL, page integer NOT NULL CHECK(page>0),
 width double precision NOT NULL CHECK(width>0), height double precision NOT NULL CHECK(height>0),
 evidence_asset_id text NOT NULL, evidence_version integer NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,source_sha256,page),
 FOREIGN KEY(release_id,source_sha256) REFERENCES content_de_codex.assets(release_id,sha256),
 FOREIGN KEY(release_id,evidence_asset_id,evidence_version) REFERENCES content_de_codex.assets(release_id,asset_id,version)
);
CREATE TABLE IF NOT EXISTS content_de_codex.blocks (
 release_id text NOT NULL, block_id text NOT NULL, source_sha256 text NOT NULL, page integer NOT NULL,
 reading_order integer NOT NULL CHECK(reading_order>0), text_content text NOT NULL, bbox double precision[] NOT NULL CHECK(cardinality(bbox)=4),
 extraction_method text NOT NULL CHECK(extraction_method IN ('pdf_text','ocr')), payload jsonb NOT NULL,
 PRIMARY KEY(release_id,block_id), FOREIGN KEY(release_id,source_sha256,page) REFERENCES content_de_codex.pages
);
CREATE TABLE IF NOT EXISTS content_de_codex.sections (
 release_id text NOT NULL, section_id text NOT NULL, case_id text NOT NULL, parent_id text,
 level integer NOT NULL CHECK(level>0), title text NOT NULL, start_block text NOT NULL, end_block text NOT NULL,
 recognized_chars integer NOT NULL CHECK(recognized_chars>=0), stage text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,section_id), FOREIGN KEY(release_id,case_id) REFERENCES content_de_codex.papers,
 FOREIGN KEY(release_id,start_block) REFERENCES content_de_codex.blocks(release_id,block_id),
 FOREIGN KEY(release_id,end_block) REFERENCES content_de_codex.blocks(release_id,block_id),
 FOREIGN KEY(release_id,parent_id) REFERENCES content_de_codex.sections(release_id,section_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS content_de_codex.section_blocks (
 release_id text NOT NULL, block_id text NOT NULL, section_id text NOT NULL,
 PRIMARY KEY(release_id,block_id), FOREIGN KEY(release_id,block_id) REFERENCES content_de_codex.blocks,
 FOREIGN KEY(release_id,section_id) REFERENCES content_de_codex.sections
);
CREATE TABLE IF NOT EXISTS content_de_codex.visual_components (
 release_id text NOT NULL, component_id text NOT NULL, kind text NOT NULL CHECK(kind IN ('figure','table','formula')),
 source_sha256 text NOT NULL, page integer NOT NULL, anchor_block_id text NOT NULL,
 evidence_asset_id text NOT NULL, evidence_version integer NOT NULL, semantic_status text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,component_id), FOREIGN KEY(release_id,source_sha256,page) REFERENCES content_de_codex.pages,
 FOREIGN KEY(release_id,anchor_block_id) REFERENCES content_de_codex.blocks(release_id,block_id),
 FOREIGN KEY(release_id,evidence_asset_id,evidence_version) REFERENCES content_de_codex.assets(release_id,asset_id,version)
);
CREATE TABLE IF NOT EXISTS content_de_codex.formula_regions (
 release_id text NOT NULL, formula_region_id text NOT NULL, source_sha256 text NOT NULL, page integer NOT NULL,
 region_type text NOT NULL CHECK(region_type IN ('embedding','isolated')), bbox double precision[] NOT NULL CHECK(cardinality(bbox)=4),
 crop_asset_id text, crop_version integer, latex_candidate text, semantic_status text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(release_id,formula_region_id), FOREIGN KEY(release_id,source_sha256,page) REFERENCES content_de_codex.pages,
 FOREIGN KEY(release_id,crop_asset_id,crop_version) REFERENCES content_de_codex.assets(release_id,asset_id,version),
 CHECK((crop_asset_id IS NULL)=(crop_version IS NULL)),
 CHECK(semantic_status IN ('candidate','source_checked','verified','rejected','abstained'))
);
CREATE TABLE IF NOT EXISTS content_de_codex.structured_import_receipts (
 release_id text PRIMARY KEY REFERENCES content_de_codex.releases,
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 counts jsonb NOT NULL, fingerprints jsonb NOT NULL, verified_at timestamptz NOT NULL DEFAULT now()
);
-- PostgreSQL does not automatically index the referencing side of foreign keys.
-- These are the actual source-page/section access paths used by consumers.
CREATE INDEX IF NOT EXISTS cumcm_blocks_source_page ON content_de_codex.blocks(release_id,source_sha256,page,reading_order);
CREATE INDEX IF NOT EXISTS cumcm_visual_source_page ON content_de_codex.visual_components(release_id,source_sha256,page);
CREATE INDEX IF NOT EXISTS cumcm_formula_source_page ON content_de_codex.formula_regions(release_id,source_sha256,page,formula_region_id);
CREATE INDEX IF NOT EXISTS cumcm_sections_case ON content_de_codex.sections(release_id,case_id,parent_id);
CREATE INDEX IF NOT EXISTS cumcm_section_members ON content_de_codex.section_blocks(release_id,section_id);
