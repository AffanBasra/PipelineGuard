# Deploying the demo to Streamlit Community Cloud

The free host for the public demo. Chosen over a Hugging Face Space because the
Docker and Gradio SDKs there now require a paid PRO account.

## Deploy

1. Push the branch to GitHub.
2. <https://share.streamlit.io> → **Create app** → **Deploy a public app from a
   repo**.
3. Fill in:

   | Field | Value |
   |---|---|
   | Repository | `AffanBasra/PipelineGuard` |
   | Branch | the branch you pushed |
   | Main file path | `deploy/streamlit-cloud/app.py` |

4. Deploy. Nothing goes in **Advanced settings** — no secrets, no environment
   variables. `app.py` carries the whole configuration, because Community Cloud
   offers no way to set environment variables and a config that lives only in a
   web form is a config nobody can review.

## What happens on first boot

| Step | Cost |
|---|---|
| `pip install -r deploy/streamlit-cloud/requirements.txt` | a few minutes, once per deploy |
| Download the pinned commits of both repos | ~415 MB, once per container |
| Load the encoder | ~15–20 s |

The download is ~415 MB rather than the repo's full 1.6 GB because the
checkpoint ships fp32, fp16 and bf16 weights side by side and this build reads
only the bf16 file. `prefetch_pinned(variant=...)` skips the other two.

Community Cloud has no build step, so `app.py` does at boot what the Docker
image does at build: fetch exactly the pinned commits, then forbid the network.
`prefetch.go_offline()` is the second half, and it flips
`huggingface_hub.constants` as well as the environment variable — by then the
library has already read the variable into a module constant.

If the prefetch fails, the app stays online and runs **unpinned** rather than
failing to start, and says so both on the page and in the sidebar's model
provenance panel.

## Why bf16

Community Cloud allows roughly 1 GB of RAM per app. The shipped fp32 checkpoint
does not fit.

| Build | Resident | Peak | ms/record | PERSON | ADDRESS |
|---|---:|---:|---:|---:|---:|
| medium fp32 (the pipeline's) | 1,780 MB | 1,790 | 94 | 99.4% | 100% |
| **medium bf16 (this build)** | **845 MB** | **858** | 225 | 99.4% | 100% |
| small bf16 | 735 MB | 768 | 142 | 99.3% | 100% |
| small int8 | 1,569 MB | 1,574 | 26 | 7.6% | 4.9% |

Measured on CPU torch in a Linux container over 50 memos per pass; see
`docs/tier2-detection-findings.md` §27 and `scripts/probe_model_footprint.py`.

bf16 keeps the **same repo at the same commit** — the pin still holds, only the
precision differs — which is why it wins over the smaller checkpoint despite
110 MB more resident. The demo and the pipeline stay the same model.

Add the app's own footprint (~96 MB for Streamlit, pandas and Arrow) and the
total is ~950 MB against ~1,000. **That is 70 MB of headroom, and it is thin.**
The untested case is a single long pasted document rather than the short memos
the probe used. If the app starts reporting resource limits, the fallback is
`TIER2_MODEL=gliner-community/gliner_small-v2.5` with its own revision and a
re-swept threshold — a different checkpoint, so §6.1 applies and the threshold
does not carry over.

int8 is in the table only to record that it was measured and rejected. It is
faster and it destroys the model.

## The other host

`deploy/hf-space/` still holds the Docker build. It bakes the weights into the
image, needs no boot-time download, and runs fp32 — use it on any Docker host
with more than 1 GB.