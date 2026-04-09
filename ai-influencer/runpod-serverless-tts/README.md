# RunPod Serverless TTS Worker

This worker keeps the existing GPT-SoVITS `/tts` request shape and returns base64 WAV.

## Input

`event.input` should be the same payload currently sent by `messenger-gateway`:

```json
{
  "text": "...",
  "text_lang": "ko",
  "prompt_lang": "ko",
  "media_type": "wav",
  "streaming_mode": false,
  "top_k": 20,
  "sample_steps": 32,
  "super_sampling": true,
  "fragment_interval": 0.4,
  "ref_audio_path": "/workspace/GPT-SoVITS-v4-real/voice/voice_21.wav",
  "prompt_text": "...",
  "seed": 123456
}
```

## Output

Success:

```json
{
  "ok": true,
  "content_type": "audio/wav",
  "audio_base64": "<base64>",
  "bytes": 12345
}
```

Failure:

```json
{
  "ok": false,
  "error": "..."
}
```

## Runtime Assumptions

- GPT-SoVITS local API is already reachable in the worker runtime at:
  - `TTS_LOCAL_API_BASE` (default: `http://127.0.0.1:9880`)
- Weights/reference files are mounted from RunPod network volume (no download step here).
