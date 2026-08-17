import eslint from "@eslint/js";
import svelte from "eslint-plugin-svelte";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**", ".svelte-kit/**"] },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs["flat/recommended"],
  {
    files: ["**/*.svelte"],
    languageOptions: {
      parserOptions: { parser: tseslint.parser },
      globals: {
        window: "readonly",
        sessionStorage: "readonly",
        atob: "readonly",
        TextDecoder: "readonly",
        Event: "readonly",
        KeyboardEvent: "readonly",
        HTMLButtonElement: "readonly",
        HTMLDivElement: "readonly",
        HTMLElement: "readonly",
        CSS: "readonly",
        ResizeObserver: "readonly"
      }
    }
  }
);
