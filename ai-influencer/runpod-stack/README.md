# Runpod Stack

`runpod-stack` is the persistent bootstrap scaffold for Runpod GPU pods.

Expected layout on the network volume:

- `bin/start-all.sh`
- `bin/stop-all.sh`
- `bin/aws-reverse-tunnel.sh`
- `bin/bootstrap-aws-tunnel-key.sh`
- `env/runpod-services.env`
- `ssh/runpod_to_aws_ed25519`
- `ssh/known_hosts`
- `logs/`
- `run/`

Typical pod startup:

```bash
cp /workspace/seedlab-eval-repo/ai-influencer/runpod-stack/env/runpod-services.env.example /workspace/runpod-stack/env/runpod-services.env
bash /workspace/runpod-stack/bin/bootstrap-aws-tunnel-key.sh
bash /workspace/runpod-stack/bin/start-all.sh
```

GPU evaluation knobs:

- `SEEDLAB_EVAL_DEVICE=auto|cuda|cpu` (default `auto`)
- `SEEDLAB_EVAL_REQUIRE_GPU=true|false` (default `false`)
- `SEEDLAB_EVAL_MODEL_CACHE_DIR=/workspace/runpod-stack/cache/seedlab-models`
- `SEEDLAB_REFERENCE_AUDIO_S3_URI=s3://hari-contents-skn22/seed-lab-reference/hari-global-v1/manifest.json`
- `SEEDLAB_REFERENCE_AUDIO_CACHE_DIR=/workspace/runpod-stack/cache/seedlab-ref-cache`
- These variables can live in `runpod-services.env`; `start-all.sh` exports them before starting `runpod-seedlab-eval-service`.
- The default reference corpus is `hari-global-v1`; when configured, speaker similarity and intonation checks can use the RunPod GPU.

Troubleshooting:

- If `start-all.sh` fails with `tts-api failed to listen on :9880`, check `/workspace/runpod-stack/logs/tts-api.log` first.
- `api_v2.py` must run from `GPT_SOVITS_ROOT`; relative model paths inside GPT-SoVITS depend on the current working directory.
- First cold start can take a few minutes before `api_v2.py` opens `127.0.0.1:9880`. Increase `TTS_API_STARTUP_TIMEOUT_SECONDS` if needed.
- Seed Lab evaluation is hybrid: OpenAI ASR/note stay remote, while `speechbrain` and `distillmos` can use the local GPU when CUDA is available.
- Set `SEEDLAB_EVAL_REQUIRE_GPU=true` if you want the eval service to fail fast instead of silently falling back to CPU.
