FROM node:22-slim

WORKDIR /app

# ffmpeg for potential voice note conversion
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY package.json ./
RUN npm install --production

COPY index.js ./

# Auth state persisted via PVC mount
VOLUME /app/auth

EXPOSE 3100

CMD ["node", "index.js"]
