# FraudClarity

FraudClarity is a Gradio-based retrieval-augmented assistant for exploring Tazama fraud rules. The current repository is centered on a standalone runtime in `app.py`, with Docker support for local execution and a small notebook footprint kept mainly for ingest and future experimentation.

## What This Repo Contains

- A standalone web app in `app.py`
- Docker packaging in `Dockerfile`
- A persisted Chroma vector store in `chroma_db/`
- Prompt templates in `prompts/`
- A required rule reference document in `docs/rule_trigger_discovery.md`
- An ingest notebook in `experimental_notebooks/enhance_input_reasoning/ingest.ipynb` for rebuilding the vector store

## Features

- Chat UI for asking rule-grounded fraud questions
- Retrieval from a local Chroma vector store
- Ollama as the default answer provider
- Optional Claude answer provider when `ANTHROPIC_API_KEY` is configured
- Heuristic judge and super-judge scoring paths for answer review
- Docker-friendly runtime for local browser access

## Runtime Architecture

At startup, `app.py`:

- loads environment variables from `.env` when present
- opens `chroma_db/` as a persisted Chroma vector store
- loads the default system prompt from `prompts/system_prompt.txt`
- reads `docs/rule_trigger_discovery.md` for explicit rule-reference evidence
- connects to Ollama at `OLLAMA_HOST`

The app will fail at startup if either of these are missing:

- `chroma_db/`
- `prompts/system_prompt.txt`

## Repository Structure

```text
app.py
Dockerfile
requirements.txt
.env.example
chroma_db/
docs/
  rule_trigger_discovery.md
experimental_notebooks/
  enhance_input_reasoning/
    ingest.ipynb
prompts/
  system_prompt.txt
  system_prompt_claude.txt
```

## Prerequisites

### Required

- Python 3.10+ if you want to run outside Docker
- Docker Desktop if you want to run the app in a container
- Ollama installed and running on your host machine

### Required Ollama models

Pull these on the host machine:

```bash
ollama pull mistral:7b-instruct
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

Notes:

- `mistral:7b-instruct` is the preferred answer model
- `llama3.1:8b` is the fallback answer model
- `nomic-embed-text` is required for retrieval embeddings

## Environment Variables

You can copy `.env.example` to `.env` for local runs.

Common variables:

- `OLLAMA_HOST`: Ollama base URL
- `ANTHROPIC_API_KEY`: required only if you use Claude from the UI
- `FRAUDCLARITY_HOST`: bind host for the Gradio server
- `FRAUDCLARITY_PORT`: preferred port
- `FRAUDCLARITY_MAX_PORT`: highest fallback port to try

Docker defaults already set these runtime values:

- `FRAUDCLARITY_HOST=0.0.0.0`
- `FRAUDCLARITY_PORT=7860`
- `FRAUDCLARITY_MAX_PORT=7860`
- `OLLAMA_HOST=http://host.docker.internal:11434`

## Quick Start With Docker

This is the primary way to run the current repo.

### 1. Build the image

From the repository root:

```bash
docker build -t fraudclarity .
```

### 2. Run the container

```bash
docker run --rm -p 7860:7860 fraudclarity
```

### 3. Open the app

```text
http://localhost:7860
```

Important notes:

- The browser URL is `http://localhost:7860`, not `0.0.0.0`
- Ollama must be running on the host before the container starts answering requests
- The Docker image expects `chroma_db/` to already exist in the repository

## Run Locally Without Docker

### 1. Create and activate a virtual environment

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Start the app

```bash
python app.py
```

Then open:

```text
http://127.0.0.1:7860
```

## Rebuild the Vector Store

The runtime app does not create embeddings or rebuild `chroma_db/`. It only consumes an existing vector store.

If you change the source documents or want to expand input coverage, use:

- `experimental_notebooks/enhance_input_reasoning/ingest.ipynb`

Use that notebook to:

- ingest updated documents
- regenerate embeddings
- rebuild `chroma_db/`

## Prompts

- `prompts/system_prompt.txt`: default prompt used by the Ollama path
- `prompts/system_prompt_claude.txt`: prompt used when Claude is selected and available

## Answer Providers

FraudClarity supports two answer-provider modes from the UI:

- `ollama`: default and expected local path
- `claude`: optional, requires `ANTHROPIC_API_KEY`

If Claude is selected without `ANTHROPIC_API_KEY`, the app will report a runtime error for that provider.

## Troubleshooting

### The app fails with `Vector store not found`

`chroma_db/` is missing. Rebuild it with the ingest notebook before running the app.

### The app starts but answers fail

Check that:

- Ollama is running
- `OLLAMA_HOST` is correct
- the required Ollama models are installed on the host

### Docker starts but the browser cannot connect

Use:

- `http://localhost:7860`

If the port is already in use, stop the existing container or choose a different host port.

### Claude is unavailable in the UI

Set `ANTHROPIC_API_KEY` in `.env` or your shell environment before launching the app.

## Development Notes

- `app.py` is the primary runtime entrypoint
- `experimental_notebooks/` is no longer the primary execution path
- the ingest notebook remains useful for future enhancements to source coverage and retrieval inputs

## Minimal Runtime Repo

If you are extracting a smaller runtime-only repository, keep at least:

- `app.py`
- `Dockerfile`
- `requirements.txt`
- `chroma_db/`
- `docs/rule_trigger_discovery.md`
- `prompts/system_prompt.txt`

Optionally keep:

- `prompts/system_prompt_claude.txt`
- `.env.example`
- `experimental_notebooks/enhance_input_reasoning/ingest.ipynb`

## License and Ownership

Use and redistribution should follow the policies and ownership terms attached to this repository and its source materials.
