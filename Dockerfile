# Glama builds this image and checks that the MCP server starts.
# Hosted file bytes go to Supabase Storage with signed URLs, not through this process.
# Set ENGINE_URL to the engine container. It fetches and writes those objects.
# Set STETWISE_HTTP_ONLY=1 so a closed stdin does not also start stdio.
# Local and dev still accept PUT and GET on /v1/upload and /v1/download.
# Set STETWISE_ACCEPT_UPLOADS=0 on the Vercel MCP process.
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
