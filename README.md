# Project Description

code-through is an extension of hermes-agent.

Added features:
/flow: By specifying functions or classes contained in a designated file, it explains the flow of their processing.

/review: Provides a detailed review of the specified file. (It also automatically applies changes if you edit the file.)

/explain: Detailed explanation of specified files or directories.

Everything can be explained using natural language.

# Install

Install Code Through as a standalone CLI with `pipx` so it does not interfere with the Python environment of the project you are working on.

```bash
pipx install code-through
```

If `pipx` is not installed yet:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Restart your shell after `pipx ensurepath` if needed.

# First Run

```bash
code-through setup
code-through
```

If you want the direct agent entrypoint too:

```bash
code-through-agent
```

# Development

`uv` is still supported for contributors and local development, but it is no longer the recommended install path for normal use.

```bash
git clone https://github.com/ymym-tymblack/code-through.git
cd code-through
uv sync
uv run code-through setup
uv run code-through --workspace /path/to/workspace
```

# Local vLLM


`/review`, `/diff`, `/explain`, and `/flow` can use a local OpenAI-compatible `vLLM` endpoint instead of `Ollama`.

The repository now defaults companion analysis to:

```yaml
analysis:
  enabled: true
  provider: custom
  model: LilaRest/gemma-4-31B-it-NVFP4-turbo
  base_url: http://127.0.0.1:8000/v1
  api_key_env: ""
```

For an RTX 5090 setup, start `vLLM` with:

```bash
docker compose up -d
```

The included [docker-compose.yaml](/workspaces/code-through/docker-compose.yaml) runs:

- `vllm/vllm-openai:cu130-nightly`
- `LilaRest/gemma-4-31B-it-NVFP4-turbo`
- OpenAI-compatible API on `http://127.0.0.1:8000/v1`

If you want the endpoint to require authentication, set `VLLM_API_KEY` before starting Compose and then point `analysis.api_key_env` at that env var.
