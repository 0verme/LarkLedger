import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
      "/readyz": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    // WSL / 低性能环境下 worker 并发启动会竞争资源而触发 vitest 硬编码的
    // 60s 启动超时（误报 "Timeout waiting for worker to respond"）。
    // 限制并发数缓解启动竞争（不影响测试语义）。
    maxWorkers: 2,
  },
});
