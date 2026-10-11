# Glama builds this image and checks that the MCP server starts.
# File editing runs in engine/Dockerfile, or in a local Python process when ENGINE_URL is unset.
# Set STETWISE_HTTP_ONLY=1 when this image is the long-running HTTP server.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
USER node
EXPOSE 3000
CMD ["node", "dist/src/server.js"]
