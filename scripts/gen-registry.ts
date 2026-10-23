/**
 * bots/<id>/index.ts を走査して src/generated/bots.ts を生成する。
 * Worker もバックテストもこのレジストリから bot を読む。
 */
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type { BotDefinition } from "../src/core/bot";

const root = resolve(import.meta.dirname, "..");
const botsDir = join(root, "bots");
const outDir = join(root, "src/generated");

const dirs = readdirSync(botsDir, { withFileTypes: true })
  .filter((d) => d.isDirectory() && !d.name.startsWith("_") && existsSync(join(botsDir, d.name, "index.ts")))
  .map((d) => d.name)
  .sort();

const errors: string[] = [];
for (const dir of dirs) {
  const mod = (await import(pathToFileURL(join(botsDir, dir, "index.ts")).href)) as { default?: BotDefinition };
  const bot = mod.default;
  if (!bot || typeof bot.strategy !== "function") {
    errors.push(`bots/${dir}/index.ts: export default defineBot({...}) がありません`);
  } else if (bot.id !== dir) {
    errors.push(`bots/${dir}: id "${bot.id}" はディレクトリ名と一致させてください`);
  }
}
if (errors.length) {
  console.error(errors.join("\n"));
  process.exit(1);
}

const lines = [
  "// 自動生成ファイル。編集しないこと (npm run gen)",
  'import type { BotDefinition } from "../core/bot";',
  ...dirs.map((d, i) => `import bot${i} from "../../bots/${d}/index";`),
  "",
  `export const bots: BotDefinition<any>[] = [${dirs.map((_, i) => `bot${i}`).join(", ")}];`,
  "",
];
mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, "bots.ts"), lines.join("\n"));
console.log(`registered ${dirs.length} bot(s): ${dirs.join(", ") || "(none)"}`);
