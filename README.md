# Project Description

hermes-agent ex is an extension of hermes-agent.

Added features:
/flow: By specifying functions or classes contained in a designated file, it explains the flow of their processing.

/review: Provides a detailed review of the specified file. (It also automatically applies changes if you edit the file.)

/explain: Detailed explanation of specified files or directories.

Everything can be explained using natural language.

# Setup

```bash
git clone https://github.com/ymym-tymblack/hermes-agent-ex.git
cd hermes-agent-ex
uv sync
uv run hermes setup
uv run hermes --workspace /path/to/workspace
```
