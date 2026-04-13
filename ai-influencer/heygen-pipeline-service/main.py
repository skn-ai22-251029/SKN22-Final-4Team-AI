import logging
import os
from contextlib import asynccontextmanager

import psycopg2
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class ContentMetadata(BaseModel):
    title: str = Field(description="Short Korean title under 50 chars")
    summary: str = Field(description="1-2 sentence Korean summary")
    tags: list[str] = Field(description="3-5 English tech tags")


class GenerateRequest(BaseModel):
    job_id: str
    audio_url: str
    avatar_id: str = ""


class RegisterContentRequest(BaseModel):
    job_id: str = ""
    script_text: str
    content_url: str = ""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _openai_client_for_metadata() -> OpenAI:
    api_key = (
        os.environ.get("OPENAI_API_KEY_CONTENT_METADATA", "").strip()
        or os.environ.get("OPENAI_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY_CONTENT_METADATA or OPENAI_FALLBACK_API_KEY "
            "(or legacy OPENAI_API_KEY) is required"
        )
    return OpenAI(api_key=api_key)


def _openai_client_for_embedding() -> OpenAI:
    api_key = (
        os.environ.get("OPENAI_API_KEY_CONTENT_EMBEDDING", "").strip()
        or os.environ.get("OPENAI_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY_CONTENT_EMBEDDING or OPENAI_FALLBACK_API_KEY "
            "(or legacy OPENAI_API_KEY) is required"
        )
    return OpenAI(api_key=api_key)


def _generate_metadata(script_text: str) -> dict:
    client = _openai_client_for_metadata()
    response = client.beta.chat.completions.parse(
        model="gpt-5.4-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate metadata for a Korean tech news short-form video. "
                    "Given script_text, produce title, summary, and tags. "
                    "Keep title concise and summary factual."
                ),
            },
            {"role": "user", "content": script_text},
        ],
        response_format=ContentMetadata,
    )
    parsed = response.choices[0].message.parsed
    return {
        "title": parsed.title.strip(),
        "summary": parsed.summary.strip(),
        "tags": [str(tag).strip() for tag in parsed.tags if str(tag).strip()],
    }


def _build_content_vector(summary: str, script_text: str) -> str:
    client = _openai_client_for_embedding()
    # Keep existing intent: vectorize summary + script_text together.
    embed_input = f"{summary}\n{script_text}"
    embed_resp = client.embeddings.create(model="text-embedding-3-small", input=embed_input)
    vector = embed_resp.data[0].embedding
    return "[" + ",".join(str(v) for v in vector) + "]"


def _register_content_sync(script_text: str, content_url: str) -> str:
    metadata = _generate_metadata(script_text)
    vector_str = _build_content_vector(metadata["summary"], script_text)

    conn = psycopg2.connect(
        host=_require_env("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        database=_require_env("DB_NAME"),
        user=_require_env("DB_USER"),
        password=_require_env("DB_PASSWORD"),
        sslmode="require",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_contents
                    (title, platform, script_text, summary, tags, content_url, is_published, content_vector, uploaded_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s::vector, CURRENT_TIMESTAMP)
                RETURNING content_id
                """,
                [
                    metadata["title"] or "AI 영상 콘텐츠",
                    "youtube_shorts",
                    script_text,
                    metadata["summary"],
                    metadata["tags"],
                    content_url,
                    True,
                    vector_str,
                ],
            )
            content_id = cur.fetchone()[0]
        conn.commit()
        return str(content_id)
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("heygen-pipeline-service started (register-content mode)")
    yield
    logger.info("heygen-pipeline-service shutdown")


app = FastAPI(title="HeyGen Pipeline Service", lifespan=lifespan)


@app.post("/generate")
async def generate(_: GenerateRequest) -> dict:
    raise HTTPException(
        status_code=410,
        detail="WF-12 now calls HeyGen directly with audio_url. /generate is disabled.",
    )


@app.post("/register-content")
async def register_content(body: RegisterContentRequest) -> dict:
    script_text = (body.script_text or "").strip()
    content_url = (body.content_url or "").strip()
    if not script_text:
        raise HTTPException(status_code=400, detail="script_text is required")
    if not content_url:
        raise HTTPException(status_code=400, detail="content_url is required")

    try:
        content_id = _register_content_sync(script_text, content_url)
    except Exception as e:
        logger.exception("[register-content] failed job_id=%s", (body.job_id or "").strip())
        raise HTTPException(status_code=500, detail=f"register content failed: {e}") from e

    logger.info(
        "[register-content] success job_id=%s content_id=%s",
        (body.job_id or "").strip(),
        content_id,
    )
    return {
        "status": "registered",
        "job_id": (body.job_id or "").strip(),
        "content_id": content_id,
        "content_url": content_url,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": "register-content-only"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
