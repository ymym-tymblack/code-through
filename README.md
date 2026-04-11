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
