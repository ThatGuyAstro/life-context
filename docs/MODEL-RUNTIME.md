# Local model runtime

This workspace uses a separately managed local `llama.cpp` process for image
analysis. The Life Context server does not download, replace, or silently
start model files. Start the model first, verify it, then start Life Context.

## Measured Gemma 4 26B profile

The pinned Windows profile was measured on 2026-08-05 with an RTX 3090:

| Item            | Value                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------- |
| Runtime         | llama.cpp `b9898`, commit `3d4cbdf18`                                                       |
| Runtime path    | `E:\LocalLLM\bin\llama.cpp\b9898\llama-server.exe`                                          |
| Model           | `gemma-4-26B-A4B-it-Q4_K_M.gguf`                                                            |
| Projector       | `mmproj-gemma-4-26B-A4B-it-Q8_0.gguf`                                                       |
| Model revision  | `ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3`                                                  |
| Model source    | [ggml-org/gemma-4-26B-A4B-it-GGUF](https://huggingface.co/ggml-org/gemma-4-26B-A4B-it-GGUF) |
| Model metadata  | Apache-2.0; check upstream terms before redistribution                                      |
| API             | `http://127.0.0.1:8080/v1`                                                                  |
| GPU placement   | `CUDA0`, `--split-mode none`, `--main-gpu 0`, full layer offload                            |
| Request profile | `--ctx-size 8192`, `--parallel 1`, batch `1024`, ubatch `512`                               |
| Vision          | Multimodal projector supplied with `--mmproj`                                               |

The exact launcher profile is kept in
[`scripts/model-runtime.ps1`](../scripts/model-runtime.ps1). The
[`scripts/start-gemma-26b.ps1`](../scripts/start-gemma-26b.ps1) entry point is
a compatibility wrapper around that canonical launcher, so the two paths do
not accumulate different flags. The model and projector are expected to be
already present under `E:\LocalLLM`; no download or replacement is performed.

The measured command is equivalent to:

```text
llama-server.exe --host 127.0.0.1 --port 8080
  --model <gemma-4-26B-A4B-it-Q4_K_M.gguf>
  --mmproj <mmproj-gemma-4-26B-A4B-it-Q8_0.gguf>
  --alias gemma-4-26b-a4b-it --device CUDA0 --n-gpu-layers 999
  --split-mode none --main-gpu 0 --ctx-size 8192 --parallel 1
  --batch-size 1024 --ubatch-size 512 --flash-attn on
  --metrics --jinja --no-webui
```

The 8192 context and one-request slot are deliberate measured-runtime
settings, not a claim that the model supports only that context or that the
configuration is a quality/latency SLA.

## Preflight and start

Preflight validates the executable, model, projector, port, SHA-256 records,
and command without starting a GPU process:

```powershell
.\scripts\model-runtime.ps1
.\scripts\model-runtime.ps1 -Json
```

Start only after reviewing the paths and hashes:

```powershell
.\scripts\model-runtime.ps1 -Start
# Equivalent compatibility entry point:
.\scripts\start-gemma-26b.ps1
```

The launcher binds only to loopback, refuses to take over an occupied port,
starts one `llama-server.exe`, and writes logs under the ignored
`.context-artifacts\model-runtime` directory. Check the server before starting
the application:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-RestMethod http://127.0.0.1:8080/v1/models
```

Do not use `llama update` for this pinned setup. If the runtime is upgraded,
pin the release tag and record the archive SHA-256 before changing the
launcher path or the `current` junction.

## Life Context wiring and structured output

The Life Context API server owns the model endpoint and model identity. The
inference client runs inside that server and owns provider authentication and
wire-format selection:

```powershell
$env:CONTEXT_SERVER_MODEL_ENDPOINT = "http://127.0.0.1:8080/v1"
$env:CONTEXT_SERVER_MODEL_NAME = "gemma-4-26b-a4b-it"
$env:CONTEXT_SERVER_MODEL_REVISION = "ggml-org/gemma-4-26B-A4B-it-GGUF@ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3"
$env:CONTEXT_SERVER_WORKER_COUNT = "1"
$env:CONTEXT_MODEL_REQUIRES_MULTIMODAL = "true"
$env:CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE = "auto"
pnpm server
```

`auto` probes the model listing before the first structured request. It uses
`json_schema` only when the endpoint explicitly advertises JSON Schema support;
otherwise it uses `json_object`. The measured b9898 Gemma endpoint accepted
`json_object` but returned HTTP 400 for the full JSON Schema grammar, so its
effective mode is `json_object` under `auto`. Life Context still validates the
returned object against the complete Pydantic contract after generation.

For a deterministic compatibility run, use:

```powershell
.\scripts\restart-local-json-object.ps1
```

Normal local restart uses capability-aware `auto`:

```powershell
.\scripts\restart-local.ps1
```

The environment names are intentionally distinct. `CONTEXT_SERVER_*` values
configure the Life Context API process; `CONTEXT_MODEL_*` values configure the
model client embedded in that process. In particular, do not replace
`CONTEXT_SERVER_MODEL_ENDPOINT` or `CONTEXT_SERVER_MODEL_NAME` with generic
model-client names when starting `pnpm server`. The standalone
[`scripts/gpu_validation.py`](../scripts/gpu_validation.py) harness has its
own defaults, `CONTEXT_MODEL_ENDPOINT`, `CONTEXT_MODEL_NAME`, and
`CONTEXT_SERVER_URL`; those do not configure the live server.

`CONTEXT_SERVER_AUTH_TOKEN` protects the Life Context API. It is separate from
`CONTEXT_MODEL_API_KEY`, which is sent to the configured model endpoint if that
provider requires a bearer key. Never put either secret in a checked-in file.

## Stop and replace safely

Inspect the owning command line before stopping the model. Do not kill every
`llama-server.exe` process on the machine:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" |
  Select-Object ProcessId,CommandLine
```

Only replace the pinned runtime after the old process is stopped and the
preflight has passed against the new executable/model/projector set.
