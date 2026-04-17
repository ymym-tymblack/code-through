# Project Description

Code-Through is an AI coding agent focused on explain, flow, review, and workspace-aware command-line workflows.

Added features:
/flow: By specifying functions or classes contained in a designated file, it explains the flow of their processing.

/review: Provides a detailed review of the specified file. (It also automatically applies changes if you edit the file.)

/explain: Detailed explanation of specified files or directories.

Everything can be explained using natural language.

# Setup

```bash
git clone https://github.com/ymym-tymblack/code-through.git
cd code-through
uv sync
uv run codet setup
uv run codet --workspace /path/to/workspace
```
