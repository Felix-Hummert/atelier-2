import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/atelier/",
  plugins: [svelte()],
  resolve: { conditions: ["browser"] },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"]
  }
});
