FROM mcr.microsoft.com/playwright:v1.47.2-jammy

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY server.mjs ./

ENV PORT=8080
ENV DATA_DIR=/data
EXPOSE 8080

CMD ["node", "server.mjs"]