/** npm run new-bot -- <id> : テンプレートから bot を作る */
import { cpSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const id = process.argv[2];
if (!id || !/^[a-z0-9][a-z0-9-]{0,62}$/.test(id)) {
  console.error("usage: npm run new-bot -- <id>   (a-z, 0-9, -)");
  process.exit(1);
}
const root = resolve(import.meta.dirname, "..");
const dest = join(root, "bots", id);
if (existsSync(dest)) {
  console.error(`bots/${id} already exists`);
  process.exit(1);
}
cpSync(join(root, "bots/_template"), dest, { recursive: true });
const file = join(dest, "index.ts");
writeFileSync(file, readFileSync(file, "utf8").replace(/__BOT_ID__/g, id));
console.log(`created bots/${id}/index.ts`);
