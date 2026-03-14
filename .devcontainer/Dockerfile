FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG LAZYGIT_VERSION=0.44.1

ENV TZ=Asia/Tokyo
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        file \
        git \
        jq \
        less \
        nodejs \
        npm \
        openssh-client \
        pipx \
        python3 \
        python3-pip \
        python3-venv \
        sudo \
        tree \
        tzdata \
        unzip \
        xz-utils \
        zsh && \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx

RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        amd64) lazygit_arch="x86_64" ;; \
        arm64) lazygit_arch="arm64" ;; \
        *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_Linux_${lazygit_arch}.tar.gz" \
      | tar -xz -C /usr/local/bin lazygit

RUN git clone --depth=1 https://github.com/ohmyzsh/ohmyzsh.git /root/.oh-my-zsh && \
    cp /root/.oh-my-zsh/templates/zshrc.zsh-template /root/.zshrc && \
    sed -i 's/^ZSH_THEME=.*/ZSH_THEME="robbyrussell"/' /root/.zshrc && \
    sed -i 's/^plugins=.*/plugins=(git python node npm docker)/' /root/.zshrc && \
    printf '\nexport PATH="/usr/local/bin:$PATH"\nalias ll="ls -lah"\n' >> /root/.zshrc

RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 30 && \
    update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 30 && \
    chsh -s /usr/bin/zsh root

WORKDIR /workspaces

CMD ["sleep", "infinity"]
