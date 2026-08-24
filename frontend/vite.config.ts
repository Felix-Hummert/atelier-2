import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig, type PluginOption } from "vite";

import { PRODUCT_NAME } from "./src/lib/productName";

/**
 * index.html cannot import from src, so the one product-name owner
 * (`src/lib/productName.ts`) is injected here instead of duplicating the
 * literal in HTML.
 */
const injectProductName: PluginOption = {
  name: "inject-product-name",
  transformIndexHtml: (html) => html.replaceAll("%PRODUCT_NAME%", PRODUCT_NAME)
};

export default defineConfig({
  base: "/atelier/",
  plugins: [svelte(), injectProductName],
  resolve: { conditions: ["browser"] },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"]
  }
});
