import logging
import os
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib import request as urllib_request

import psycopg2
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


DEFAULT_CHAT_MODEL_RATES_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-5.4-mini": (0.75, 4.50),
}
DEFAULT_EMBEDDING_MODEL_RATES_USD_PER_1M: dict[str, float] = {
    "text-embedding-3-small": 0.02,
}


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


def _env_float(name: str, default: float = 0.0) -> float:
    raw = str(os.environ.get(name, default) or default).strip()
    try:
        return float(raw)
    except Exception:
        return float(default)


def _model_rate(model: str, table: dict[str, float] | dict[str, tuple[float, float]]) -> float | tuple[float, float] | None:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None
    if normalized in table:
        return table[normalized]
    for key, value in table.items():
        if normalized.startswith(key):
            return value
    return None


def _estimate_chat_cost_usd(usage: dict[str, int | str]) -> float | None:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    model = str(usage.get("model") or "").strip()
    default_rates = _model_rate(model, DEFAULT_CHAT_MODEL_RATES_USD_PER_1M)
    default_input_rate = float(default_rates[0]) if isinstance(default_rates, tuple) else 0.0
    default_output_rate = float(default_rates[1]) if isinstance(default_rates, tuple) else 0.0
    input_rate = _env_float("CONTENT_METADATA_INPUT_COST_USD_PER_1M", default_input_rate)
    output_rate = _env_float("CONTENT_METADATA_OUTPUT_COST_USD_PER_1M", default_output_rate)
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return None
    if input_rate <= 0 and output_rate <= 0:
        return None
    return ((prompt_tokens / 1_000_000.0) * input_rate) + ((completion_tokens / 1_000_000.0) * output_rate)


def _estimate_embedding_cost_usd(prompt_tokens: int, *, embedding_model: str = "") -> float | None:
    default_rate = _model_rate(embedding_model, DEFAULT_EMBEDDING_MODEL_RATES_USD_PER_1M)
    rate = _env_float("CONTENT_EMBEDDING_COST_USD_PER_1M_TOKENS", float(default_rate or 0.0))
    if prompt_tokens <= 0 or rate <= 0:
        return None
    return (prompt_tokens / 1_000_000.0) * rate


def _post_cost_event(payload: dict[str, object]) -> None:
    gateway_url = (os.environ.get("COST_TRACKING_GATEWAY_URL", "http://messenger-gateway:8080") or "").strip().rstrip("/")
    secret = (os.environ.get("GATEWAY_INTERNAL_SECRET", "") or "").strip()
    if not gateway_url or not secret:
        return
    request = urllib_request.Request(
        f"{gateway_url}/internal/cost-events",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Secret": secret},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=15) as response:
        response.read()


def _record_cost_event(
    *,
    job_id: str,
    process: str,
    api_key_family: str,
    usage_json: dict[str, object],
    cost_usd: float | None,
) -> None:
    payload = {
        "job_id": job_id,
        "stage": "content",
        "process": process,
        "provider": "openai",
        "attempt_no": 1,
        "status": "success",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "usage_json": usage_json,
        "raw_response_json": {"source": "heygen-pipeline-service"},
        "cost_usd": cost_usd,
        "pricing_kind": "estimated" if cost_usd is not None else "missing",
        "pricing_source": "provider_usage_estimate" if cost_usd is not None else "unavailable",
        "api_key_family": api_key_family,
        "subject_type": "job",
        "subject_key": job_id,
        "subject_label": job_id,
        "idempotency_key": f"content:{process}:{job_id}:{usage_json.get('model') or usage_json.get('embedding_model') or 'unknown'}",
    }
    try:
        _post_cost_event(payload)
    except Exception as e:
        logger.warning("[cost] heygen-pipeline event post failed process=%s job_id=%s err=%s", process, job_id, e)


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


def _generate_metadata(script_text: str) -> tuple[dict, dict[str, int | str], float | None]:
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
    usage = {
        "prompt_tokens": int(getattr(response.usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(response.usage, "total_tokens", 0) or 0),
        "model": str(getattr(response, "model", "") or "gpt-5.4-mini"),
    }
    return {
        "title": parsed.title.strip(),
        "summary": parsed.summary.strip(),
        "tags": [str(tag).strip() for tag in parsed.tags if str(tag).strip()],
    }, usage, _estimate_chat_cost_usd(usage)


def _build_content_vector(summary: str, script_text: str) -> tuple[str, dict[str, int | str], float | None]:
    client = _openai_client_for_embedding()
    # Keep existing intent: vectorize summary + script_text together.
    embed_input = f"{summary}\n{script_text}"
    embed_resp = client.embeddings.create(model="text-embedding-3-small", input=embed_input)
    vector = embed_resp.data[0].embedding
    prompt_tokens = int(getattr(getattr(embed_resp, "usage", None), "prompt_tokens", 0) or 0)
    usage = {
        "prompt_tokens": prompt_tokens,
        "total_tokens": int(getattr(getattr(embed_resp, "usage", None), "total_tokens", 0) or prompt_tokens),
        "embedding_model": str(getattr(embed_resp, "model", "") or "text-embedding-3-small"),
        "input_chars": len(embed_input),
    }
    return "[" + ",".join(str(v) for v in vector) + "]", usage, _estimate_embedding_cost_usd(
        prompt_tokens,
        embedding_model=usage["embedding_model"],
    )


def _register_content_sync(
    script_text: str,
    content_url: str,
) -> tuple[str, dict[str, int | str], float | None, dict[str, int | str], float | None]:
    metadata, metadata_usage, metadata_cost = _generate_metadata(script_text)
    vector_str, embedding_usage, embedding_cost = _build_content_vector(metadata["summary"], script_text)

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
        return str(content_id), metadata_usage, metadata_cost, embedding_usage, embedding_cost
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
        content_id, metadata_usage, metadata_cost, embedding_usage, embedding_cost = _register_content_sync(script_text, content_url)
    except Exception as e:
        logger.exception("[register-content] failed job_id=%s", (body.job_id or "").strip())
        raise HTTPException(status_code=500, detail=f"register content failed: {e}") from e

    job_id = (body.job_id or "").strip()
    if job_id:
        _record_cost_event(
            job_id=job_id,
            process="content_metadata_generate",
            api_key_family="content_metadata",
            usage_json=metadata_usage,
            cost_usd=metadata_cost,
        )
        _record_cost_event(
            job_id=job_id,
            process="content_embedding_generate",
            api_key_family="content_embedding",
            usage_json=embedding_usage,
            cost_usd=embedding_cost,
        )

    logger.info(
        "[register-content] success job_id=%s content_id=%s",
        job_id,
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
