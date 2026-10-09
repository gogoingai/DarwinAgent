// Verify adopted PNGs; optionally rebuild the retained structural Mermaid SVGs.
// No API calls or remote media hosting. Never overwrite adopted hand-drawn PNGs.
import { readdir, readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = path.dirname(fileURLToPath(import.meta.url));
const expected = ['hero', 'architecture', 'evolution-loop', 'asset-boundary']
  .flatMap(group => ['en', 'zh-CN'].map(locale => `${group}-${locale}.png`)).sort();
const manifest = JSON.parse(await readFile(path.join(root, 'manifest.json'), 'utf8'));
const adopted = manifest.assets.map(asset => asset.file).sort();
if (JSON.stringify(adopted) !== JSON.stringify(expected)) {
  throw new Error('Manifest must list exactly the eight bilingual adopted PNGs.');
}
for (const asset of manifest.assets) {
  const bytes = await readFile(path.join(root, asset.file));
  if (bytes.length < 33 || bytes.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a'
      || bytes.subarray(12, 16).toString('ascii') !== 'IHDR'
      || bytes.readUInt32BE(16) !== asset.width || bytes.readUInt32BE(20) !== asset.height
      || createHash('sha256').update(bytes).digest('hex') !== asset.sha256) {
    throw new Error(`Adopted PNG differs from its manifest: ${asset.file}`);
  }
  const sources = [asset.editable_prompt.split('#')[0], ...asset.structural_sources];
  if (asset.creation.supporting_reference) sources.push(asset.creation.supporting_reference);
  for (const source of sources) await readFile(path.join(root, source));
}
console.log('Verified eight adopted PNGs, dimensions, SHA-256 and local source links.');

if (!process.argv.includes('--verify-only')) {
  const cli = process.env.MMDC || path.join(root, 'node_modules/.bin/mmdc');
  const extra = process.env.PUPPETEER_CONFIG ? ['-p', process.env.PUPPETEER_CONFIG] : [];
  for (const file of (await readdir(root)).filter(f => f.endsWith('.mmd')).sort()) {
    const source = path.join(root, file);
    const output = source.replace(/\.mmd$/, '.svg');
    const args = ['-i', source, '-o', output, '-c', path.join(root, 'mermaid-config.json'),
      '--no-font-embed', '-b', '#fbfcf8', '--size', '1800', ...extra];
    const result = spawnSync(cli, args, { stdio: 'inherit' });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`Rendering failed: ${file} → svg`);
  }
  console.log('Built six structural Mermaid SVGs; adopted hand-drawn PNGs are unchanged.');
}
