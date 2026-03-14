SHELL := /bin/bash

DEVCONTAINER := devcontainer
WORKSPACE := .

.PHONY: dc-up dc-bash dc-zsh dc-rebuild

dc-up:
	$(DEVCONTAINER) up --workspace-folder $(WORKSPACE)

dc-rebuild:
	$(DEVCONTAINER) up --workspace-folder $(WORKSPACE) --remove-existing-container

dc-bash:
	@./scripts/devcontainer-exec.sh bash

dc-zsh:
	@./scripts/devcontainer-exec.sh zsh
