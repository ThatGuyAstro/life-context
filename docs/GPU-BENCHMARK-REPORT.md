# Gemma 4 26B GPU validation report

Date: 2026-08-05  
Host: Windows, NVIDIA GeForce RTX 3090 24,576 MiB  
Runtime: llama.cpp `b9898` (`version: 9898`, commit `3d4cbdf18`)

## Exact server

One server was started and left running:

```text
PID 39064
E:\LocalLLM\bin\llama.cpp\b9898\llama-server.exe
--host 127.0.0.1 --port 8080
--model E:\LocalLLM\cache\llama.cpp\models--ggml-org--gemma-4-26B-A4B-it-GGUF\snapshots\ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3\gemma-4-26B-A4B-it-Q4_K_M.gguf
--mmproj E:\LocalLLM\cache\llama.cpp\models--ggml-org--gemma-4-26B-A4B-it-GGUF\snapshots\ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3\mmproj-gemma-4-26B-A4B-it-Q8_0.gguf
--device CUDA0 --n-gpu-layers 999 --split-mode none --main-gpu 0
--ctx-size 8192 --parallel 1 --batch-size 1024 --ubatch-size 512
--flash-attn on --metrics --jinja --no-webui
```

The model GGUF is 16,796,015,136 bytes and the projector is 806,408,000
bytes. The executable, model, and projector were present locally; no download
or replacement was performed.

`GET /health` returned `{"status":"ok"}`. `GET /v1/models` returned model
`gemma-4-26b-a4b-it`, `n_params=25,233,142,046`, `n_ctx=8192`, `ftype=Q4_K -
Medium`, and capabilities `completion,multimodal`.

## Direct multimodal smoke

Request: `POST /v1/chat/completions`, model `gemma-4-26b-a4b-it`, a deterministic
1x1 PNG data URL, `temperature=0`, `seed=7`, `max_tokens=64`, streaming enabled.

```text
HTTP status: 200
First SSE event: 562.106 ms
First non-empty token/reasoning delta: 562.166 ms
Total response time: 1,047.950 ms
Usage: 76 prompt tokens, 64 completion tokens, 140 total
Derived completion throughput: 61.1 tokens/s over wall-clock response time
Peak sampled utilization: 88%
Sampled VRAM: 20,211 MiB of 24,576 MiB
```

The returned stream described the image as a solid black image. A separate
non-streaming image request returned HTTP 200 in 708.260 ms and reported llama.cpp
timings of 206.8 prompt tokens/s and 122.2 predicted tokens/s.

## Life Context ingestion and analysis

The context server was configured to use `http://127.0.0.1:8080/v1` with model
`gemma-4-26b-a4b-it`, one worker, and revision
`ggml-org/gemma-4-26B-A4B-it-GGUF@ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3`.

The first queue attempt exposed an endpoint compatibility issue: llama.cpp
returned HTTP 400, `Failed to initialize samplers: failed to parse grammar`,
for the full `json_schema` response format. The same server accepts
`json_object`. The API/dashboard launcher was restarted with
`CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE=json_object`; Life Context still validates
the returned object against the full Pydantic/schema contract after generation.

The subsequent capture-to-analysis request used the 68-byte PNG artifact with
SHA-256
`431ced6916a2a21a156e38701afe55bbd7f88969fbbfc56d7fe099d47f265460`.

```text
Queue result: 1 accepted, 1 completed, 0 new failures
Worker: 1 active, processed=1
Inference latency: 7,613.877 ms
Request ID: 7486162e-af26-49e1-a212-96f62276ab59
Response ID: chatcmpl-AnzSeXSUp5KJwpcBUHWmGRi8u4dmBOJ1
Usage: 616 prompt tokens, 771 completion tokens, 1,387 total
Validation: valid
```

The persisted observation summary was: “The system is performing a smoke test
for JSON object multimodal integration, as identified in the task payload.”
Its provenance records the endpoint, model revision, prompt/schema hashes, raw
response, usage, latency, and `structured_output_mode=json_object`.

## Reproducible validation command

The CPU-side harness is [`scripts/gpu_validation.py`](../scripts/gpu_validation.py).
It never launches a model and defaults to a dry-run:

```powershell
python scripts/gpu_validation.py --mode full --image path\to\screenshot.png
```

Add `--execute` only when the endpoint is explicitly ready. It records endpoint
health, model listing, latency, throughput, failure rate, queue growth, and
`nvidia-smi` VRAM/utilization samples. It does not stop or replace any model.

## Limits

This is a smoke/short-run validation, not a quality benchmark or hard real-time
SLA. The queue path used one successful event; sustained throughput, long-run
queue growth, multi-monitor capture, and semantic vector retrieval remain
unmeasured. The server is intentionally single-slot (`--parallel 1`) and no
second GPU process was started.
