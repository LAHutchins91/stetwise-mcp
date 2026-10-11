# Glama builds this image and checks that the MCP server starts.
# This image is also the long-running Node process for MCP, uploads, and downloads.
# It does not include Python. Set ENGINE_URL to the private engine container.
# Set STETWISE_HTTP_ONLY=1 so a closed stdin does not also start stdio.
# Vercel cannot accept a 25 MB body. Point STETWISE_FILE_BASE_URL at this process
# and set STETWISE_ACCEPT_UPLOADS=0 on Vercel.
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
