// Run from docs/assets after npm install. No API calls or remote media hosting.
import { readdir, readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import sharp from 'sharp';
const root = path.dirname(fileURLToPath(import.meta.url));
const cli = process.env.MMDC || path.join(root, 'node_modules/.bin/mmdc');
const extra = process.env.PUPPETEER_CONFIG ? ['-p', process.env.PUPPETEER_CONFIG] : [];
for (const file of (await readdir(root)).filter(f => f.endsWith('.mmd')).sort()) {
  const source = path.join(root, file);
  for (const format of ['svg', 'png']) {
    const output = source.replace(/\.mmd$/, `.${format}`);
    const args = ['-i', source, '-o', output, '-c', path.join(root, 'mermaid-config.json'),
      '--no-font-embed', '-b', '#fbfcf8', '--size', '1800', '-s', '2', ...extra];
    const result = spawnSync(cli, args, { stdio: 'inherit' });
    if (result.status !== 0) throw new Error(`Rendering failed: ${file} → ${format}`);
  }
}
for (const file of ['hero-en.svg', 'hero-zh-CN.svg']) {
  await sharp(await readFile(path.join(root, file)), { density: 192 })
    .png().toFile(path.join(root, file.replace(/\.svg$/, '.png')));
}
console.log('Built six Mermaid SVG/PNG diagrams and two SVG-source hero PNGs.');
