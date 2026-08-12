import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/atelier/",
  plugins: [svelte()],
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"]
  }
});
