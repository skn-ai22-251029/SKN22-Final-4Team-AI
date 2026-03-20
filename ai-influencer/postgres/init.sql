-- ─────────────────────────────────────────
-- AI Influencer DB 초기화
-- ─────────────────────────────────────────

-- characters 테이블
CREATE TABLE IF NOT EXISTS characters (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    concept_summary     TEXT,
    comfyui_workflow_id TEXT,
    voice_profile       TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- jobs 테이블
CREATE TABLE IF NOT EXISTS jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             TEXT NOT NULL,
    character_id        TEXT REFERENCES characters(id),
    concept_text        TEXT,
    ref_image_url       TEXT,
    script_json         JSONB,
    preview_url         TEXT,
    final_url           TEXT,
    status              TEXT NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN (
                            'DRAFT','SCRIPTING','GENERATING',
                            'WAITING_APPROVAL','REVISION_REQUESTED',
                            'APPROVED','PUBLISHING','PUBLISHED',
                            'ANALYTICS_COLLECTED','FAILED',
                            'WAITING_VIDEO_APPROVAL'
                        )),
    revision_count      INT DEFAULT 0,
    revision_note       TEXT,
    audio_url           TEXT,
    video_url           TEXT,
    error_message       TEXT,
    messenger_source    TEXT NOT NULL DEFAULT 'discord'
                        CHECK (messenger_source IN ('discord')),
    messenger_user_id   TEXT NOT NULL,
    messenger_channel_id TEXT,
    confirm_message_id  TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- platform_posts 테이블
CREATE TABLE IF NOT EXISTS platform_posts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id              UUID REFERENCES jobs(id) ON DELETE CASCADE,
    platform            TEXT CHECK (platform IN ('youtube','instagram','tiktok')),
    platform_post_id    TEXT,
    published_at        TIMESTAMPTZ,
    metrics_json        JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- job_logs 테이블
CREATE TABLE IF NOT EXISTS job_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      UUID REFERENCES jobs(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- 트리거: jobs.updated_at 자동 갱신
-- ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_jobs_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_jobs_updated_at();

-- ─────────────────────────────────────────
-- 트리거: jobs status 변경 시 job_logs 자동 insert
-- ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION log_job_status_change()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO job_logs (job_id, from_status, to_status)
        VALUES (NEW.id, OLD.status, NEW.status);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_job_status_log
    AFTER UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION log_job_status_change();

-- ─────────────────────────────────────────
-- 기본 데이터
-- ─────────────────────────────────────────
INSERT INTO characters (id, name, concept_summary, voice_profile)
VALUES (
    'default-character',
    '지수',
    'AI 가상 인플루언서, 20대 여성, 금융/라이프스타일 분야',
    'default'
) ON CONFLICT (id) DO NOTHING;
