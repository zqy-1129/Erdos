/**
 * ESLint 9 扁平配置（客户端 SP3-*）：js recommended + typescript-eslint recommended。
 * TS 文件由 tsc 承担类型检查，此处聚焦风格与可疑模式（CI 门禁）。
 */
import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.ts", "**/*.tsx"],
    rules: {
      // 类型安全由 tsc strict 承担；ESLint 不重复报 explicit any（桥接层需访问未知载荷）
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);