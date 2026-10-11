import os from "node:os";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    fileParallelism: false,
    hookTimeout: 120000,
    testTimeout: 120000,
    env: {
      NODE_ENV: "test",
      STETWISE_DEV_AUTH: "1",
      STETWISE_DEV_TOKEN: "dev-stetwise",
      STETWISE_SIGNING_SECRET: "test-signing-secret",
      STETWISE_PURGE_SECRET: "test-purge-secret",
      STETWISE_DATA_DIR: path.join(os.tmpdir(), "stetwise-vitest"),
      APP_BASE_URL: "http://127.0.0.1:9"
    }
  }
});
