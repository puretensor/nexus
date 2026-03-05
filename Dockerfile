FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client ffmpeg ripgrep curl git fonts-dejavu-core fonts-dejavu-extra \
    pandoc poppler-utils qpdf nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Node.js document generation packages (DOCX, PPTX skills)
RUN npm install -g docx pptxgenjs

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Claude CLI binary (213MB ELF, only needs glibc)
# Staged into build context as claude-bin/claude before build
COPY claude-bin/claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# Document skills seed (pdf, docx, pptx, xlsx)
COPY .claude-skills-seed/ /app/.claude-skills-seed/

# Application code
COPY . .

# Create dirs for persistent data and output
RUN mkdir -p /data/state/observers /data/hal /output \
    /app/.ssh /app/.claude /app/.config/puretensor/gdrive_tokens && \
    useradd -m -u 1000 -d /app nexus && \
    chown -R nexus:nexus /app /data /output

USER nexus

ENV HOME=/app \
    PYTHONUNBUFFERED=1 \
    CLAUDE_BIN=/usr/local/bin/claude

EXPOSE 9876

CMD ["python3", "nexus.py"]
