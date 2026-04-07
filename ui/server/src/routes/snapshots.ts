/**
 * Snapshots API — code snapshot browsing and diff computation
 *
 * Endpoints:
 *   GET /api/iterations/:id/snapshots
 *     → list all prover snapshot dirs with step counts
 *
 *   GET /api/iterations/:id/snapshots/:prover
 *     → list baseline + all steps for a prover, with file sizes
 *
 *   GET /api/iterations/:id/snapshots/:prover/:file
 *     → read a single snapshot file (baseline.lean or step-NNN.lean)
 *
 *   GET /api/iterations/:id/snapshots/:prover/diff/:step
 *     → compute unified diff between step N-1 (or baseline) and step N
 *
 *   GET /api/iterations/:id/snapshots/:prover/diff-all
 *     → return all diffs in sequence (for playback preloading)
 */
import fs from 'fs';
import path from 'path';
import type { FastifyInstance } from 'fastify';
import type { ProjectPaths } from './project.js';

interface SnapshotProverSummary {
  slug: string;
  file?: string;       // from meta.json provers.<slug>.file
  stepCount: number;
  hasBaseline: boolean;
}

interface SnapshotFileInfo {
  name: string;
  size: number;
  modified: string;
}

interface DiffResult {
  step: number;
  fromFile: string;
  toFile: string;
  diff: string;          // unified diff text
  addedLines: number;
  removedLines: number;
}

/** Simple unified diff implementation (no external deps) */
function computeUnifiedDiff(
  oldText: string, newText: string,
  oldLabel: string, newLabel: string,
  contextLines = 3,
): string {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');

  // LCS-based diff (simple O(n*m) for reasonable file sizes)
  const m = oldLines.length;
  const n = newLines.length;

  // Build edit script using Myers-like approach (simplified)
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (oldLines[i] === newLines[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  // Extract hunks
  interface Change { type: 'keep' | 'add' | 'remove'; oldIdx: number; newIdx: number; line: string }
  const changes: Change[] = [];
  let i = 0, j = 0;
  while (i < m || j < n) {
    if (i < m && j < n && oldLines[i] === newLines[j]) {
      changes.push({ type: 'keep', oldIdx: i, newIdx: j, line: oldLines[i] });
      i++; j++;
    } else if (i < m && (j >= n || dp[i + 1][j] >= dp[i][j + 1])) {
      changes.push({ type: 'remove', oldIdx: i, newIdx: j, line: oldLines[i] });
      i++;
    } else if (j < n) {
      changes.push({ type: 'add', oldIdx: i, newIdx: j, line: newLines[j] });
      j++;
    }
  }

  // Group into hunks with context
  const hunks: Change[][] = [];
  let currentHunk: Change[] = [];
  let lastChangeIdx = -999;

  for (let k = 0; k < changes.length; k++) {
    const c = changes[k];
    if (c.type !== 'keep') {
      // Include context before
      const ctxStart = Math.max(lastChangeIdx === -999 ? 0 : lastChangeIdx + 1, k - contextLines);
      if (currentHunk.length > 0 && ctxStart > lastChangeIdx + 1 + contextLines) {
        // Gap too large, start new hunk
        // Add trailing context to current hunk
        for (let t = lastChangeIdx + 1; t < Math.min(lastChangeIdx + 1 + contextLines, k); t++) {
          if (changes[t].type === 'keep') currentHunk.push(changes[t]);
        }
        hunks.push(currentHunk);
        currentHunk = [];
      }
      // Add leading context
      for (let t = ctxStart; t < k; t++) {
        if (!currentHunk.includes(changes[t])) currentHunk.push(changes[t]);
      }
      currentHunk.push(c);
      lastChangeIdx = k;
    }
  }
  // Trailing context for last hunk
  if (currentHunk.length > 0) {
    for (let t = lastChangeIdx + 1; t < Math.min(lastChangeIdx + 1 + contextLines, changes.length); t++) {
      if (changes[t].type === 'keep') currentHunk.push(changes[t]);
    }
    hunks.push(currentHunk);
  }

  if (hunks.length === 0) return '';

  // Format unified diff
  let result = `--- ${oldLabel}\n+++ ${newLabel}\n`;
  for (const hunk of hunks) {
    const firstOld = hunk.find(c => c.type !== 'add')?.oldIdx ?? 0;
    const firstNew = hunk.find(c => c.type !== 'remove')?.newIdx ?? 0;
    const oldCount = hunk.filter(c => c.type !== 'add').length;
    const newCount = hunk.filter(c => c.type !== 'remove').length;
    result += `@@ -${firstOld + 1},${oldCount} +${firstNew + 1},${newCount} @@\n`;
    for (const c of hunk) {
      if (c.type === 'keep') result += ` ${c.line}\n`;
      else if (c.type === 'remove') result += `-${c.line}\n`;
      else if (c.type === 'add') result += `+${c.line}\n`;
    }
  }

  return result;
}

function countDiffLines(diff: string): { added: number; removed: number } {
  let added = 0, removed = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++;
    else if (line.startsWith('-') && !line.startsWith('---')) removed++;
  }
  return { added, removed };
}

/** Aggregate snapshots across all iterations, grouped by file slug */
interface FileSnapshotSummary {
  slug: string;
  file?: string;            // e.g. "SnapshotTest/Nat.lean"
  iterations: {
    id: string;             // "iter-001"
    stepCount: number;
    hasBaseline: boolean;
  }[];
  totalSteps: number;
}

export function register(fastify: FastifyInstance, paths: ProjectPaths) {
  const { logsPath } = paths;

  /** Sanitize URL params to prevent path traversal */
  const safe = (s: string) => path.basename(s);

  // --- Cross-iteration file-centric API ---

  /** List all files that have snapshots, aggregated across iterations */
  fastify.get('/api/snapshot-files', async () => {
    if (!fs.existsSync(logsPath)) return [];

    const iterDirs = fs.readdirSync(logsPath)
      .filter(d => d.startsWith('iter-') && fs.statSync(path.join(logsPath, d)).isDirectory())
      .sort();

    // Aggregate by slug across iterations
    const fileMap = new Map<string, FileSnapshotSummary>();

    for (const iterDir of iterDirs) {
      const snapshotsDir = path.join(logsPath, iterDir, 'snapshots');
      if (!fs.existsSync(snapshotsDir)) continue;

      // Read meta for file name mapping
      let provers: Record<string, { file: string }> = {};
      try {
        const meta = JSON.parse(fs.readFileSync(path.join(logsPath, iterDir, 'meta.json'), 'utf-8'));
        provers = meta.provers || {};
      } catch { /* ignore */ }

      for (const slug of fs.readdirSync(snapshotsDir)) {
        const slugPath = path.join(snapshotsDir, slug);
        if (!fs.statSync(slugPath).isDirectory()) continue;

        const files = fs.readdirSync(slugPath);
        const stepCount = files.filter(f => f.startsWith('step-') && f.endsWith('.lean')).length;
        const hasBaseline = files.includes('baseline.lean');

        if (!fileMap.has(slug)) {
          fileMap.set(slug, {
            slug,
            file: provers[slug]?.file,
            iterations: [],
            totalSteps: 0,
          });
        }
        const entry = fileMap.get(slug)!;
        if (!entry.file && provers[slug]?.file) entry.file = provers[slug].file;
        entry.iterations.push({ id: iterDir, stepCount, hasBaseline });
        entry.totalSteps += stepCount;
      }
    }

    return Array.from(fileMap.values()).sort((a, b) => a.slug.localeCompare(b.slug));
  });

  /** Get full timeline for a file: all steps across all iterations */
  fastify.get<{ Params: { slug: string } }>(
    '/api/snapshot-files/:slug/timeline',
    async (req, reply) => {
      const { slug } = req.params;
      if (!fs.existsSync(logsPath)) return [];
      const safeSlug = safe(slug);

      const iterDirs = fs.readdirSync(logsPath)
        .filter(d => d.startsWith('iter-') && fs.statSync(path.join(logsPath, d)).isDirectory())
        .sort();

      const timeline: {
        iteration: string;
        step: number;        // 0 = baseline
        file: string;        // filename: baseline.lean / step-001.lean
        ts?: string;         // timestamp from code_snapshot event in jsonl
        proverLog?: string;  // e.g. "SnapshotTest_Nat" for log cross-reference
        sourceFile?: string; // actual edited file recorded by code_snapshot event
        diff?: string;       // unified diff from previous step (or baseline)
        addedLines?: number;
        removedLines?: number;
      }[] = [];

      let prevContent: string | null = null;

      for (const iterDir of iterDirs) {
        const snapDir = path.join(logsPath, iterDir, 'snapshots', safeSlug);
        if (!fs.existsSync(snapDir)) continue;

        // Read code_snapshot provenance from prover jsonl
        const tsMap = new Map<number, string>();  // step → ts
        const sourceFileMap = new Map<number, string>(); // step → actual edited file
        const proverJsonlPath = path.join(logsPath, iterDir, 'provers', `${safeSlug}.jsonl`);
        if (fs.existsSync(proverJsonlPath)) {
          try {
            const lines = fs.readFileSync(proverJsonlPath, 'utf-8').split('\n').filter(Boolean);
            for (const line of lines) {
              const entry = JSON.parse(line);
              if (entry.event === 'code_snapshot' && entry.step) {
                if (entry.ts) tsMap.set(entry.step, entry.ts);
                if (entry.file) sourceFileMap.set(entry.step, entry.file);
              }
            }
          } catch { /* ignore parse errors */ }
        }

        const allFiles = fs.readdirSync(snapDir)
          .filter(f => f.endsWith('.lean'))
          .sort();

        for (const fname of allFiles) {
          const content = fs.readFileSync(path.join(snapDir, fname), 'utf-8');
          const step = fname === 'baseline.lean' ? 0 : parseInt(fname.replace('step-', '').replace('.lean', ''), 10);

          let diff: string | undefined;
          let addedLines: number | undefined;
          let removedLines: number | undefined;

          if (prevContent !== null && content !== prevContent) {
            diff = computeUnifiedDiff(prevContent, content, 'previous', fname);
            const counts = countDiffLines(diff);
            addedLines = counts.added;
            removedLines = counts.removed;
          }

          timeline.push({
            iteration: iterDir,
            step,
            file: fname,
            ts: tsMap.get(step),
            proverLog: safeSlug,
            sourceFile: sourceFileMap.get(step),
            diff,
            addedLines,
            removedLines,
          });
          prevContent = content;
        }
      }

      return timeline;
    },
  );

  /** Read a snapshot file for a specific file+iteration */
  fastify.get<{ Params: { slug: string; iteration: string; file: string } }>(
    '/api/snapshot-files/:slug/:iteration/:file',
    async (req, reply) => {
      const { slug, iteration, file: fileName } = req.params;
      const safeFile = safe(fileName);
      const filePath = path.join(logsPath, safe(iteration), 'snapshots', safe(slug), safeFile);
      if (!fs.existsSync(filePath)) return reply.status(404).send({ error: 'File not found' });
      return { name: safeFile, iteration: safe(iteration), content: fs.readFileSync(filePath, 'utf-8') };
    },
  );

  // --- Per-iteration snapshot APIs (kept for compatibility) ---

  // List all prover snapshot dirs for an iteration
  fastify.get<{ Params: { id: string } }>(
    '/api/iterations/:id/snapshots',
    async (req, reply) => {
      const { id } = req.params;
      if (!id.startsWith('iter-')) return reply.status(400).send({ error: 'Invalid iteration id' });

      const snapshotsDir = path.join(logsPath, safe(id), 'snapshots');
      if (!fs.existsSync(snapshotsDir)) return [];

      // Read meta.json for prover file mapping
      let provers: Record<string, { file: string }> = {};
      try {
        const meta = JSON.parse(fs.readFileSync(path.join(logsPath, safe(id), 'meta.json'), 'utf-8'));
        provers = meta.provers || {};
      } catch { /* ignore */ }

      const result: SnapshotProverSummary[] = [];
      for (const dir of fs.readdirSync(snapshotsDir).sort()) {
        const dirPath = path.join(snapshotsDir, dir);
        if (!fs.statSync(dirPath).isDirectory()) continue;
        const files = fs.readdirSync(dirPath);
        const stepCount = files.filter(f => f.startsWith('step-') && f.endsWith('.lean')).length;
        const hasBaseline = files.includes('baseline.lean');
        result.push({
          slug: dir,
          file: provers[dir]?.file,
          stepCount,
          hasBaseline,
        });
      }
      return result;
    },
  );

  // List files in a prover's snapshot dir
  fastify.get<{ Params: { id: string; prover: string } }>(
    '/api/iterations/:id/snapshots/:prover',
    async (req, reply) => {
      const { id, prover } = req.params;
      if (!id.startsWith('iter-')) return reply.status(400).send({ error: 'Invalid iteration id' });

      const proverSnapDir = path.join(logsPath, safe(id), 'snapshots', safe(prover));
      if (!fs.existsSync(proverSnapDir)) return reply.status(404).send({ error: 'No snapshots' });

      const files: SnapshotFileInfo[] = [];
      for (const f of fs.readdirSync(proverSnapDir).filter(f => f.endsWith('.lean')).sort()) {
        const stat = fs.statSync(path.join(proverSnapDir, f));
        files.push({ name: f, size: stat.size, modified: stat.mtime.toISOString() });
      }
      return files;
    },
  );

  // Read a single snapshot file
  fastify.get<{ Params: { id: string; prover: string; file: string } }>(
    '/api/iterations/:id/snapshots/:prover/:file',
    async (req, reply) => {
      const { id, prover, file: fileName } = req.params;
      if (!id.startsWith('iter-')) return reply.status(400).send({ error: 'Invalid iteration id' });

      const safeFile = path.basename(fileName);
      const filePath = path.join(logsPath, safe(id), 'snapshots', safe(prover), safeFile);
      if (!fs.existsSync(filePath)) return reply.status(404).send({ error: 'File not found' });

      return { name: safeFile, content: fs.readFileSync(filePath, 'utf-8') };
    },
  );

  // Compute diff between step N-1 (or baseline) and step N
  fastify.get<{ Params: { id: string; prover: string; step: string } }>(
    '/api/iterations/:id/snapshots/:prover/diff/:step',
    async (req, reply) => {
      const { id, prover, step: stepStr } = req.params;
      if (!id.startsWith('iter-')) return reply.status(400).send({ error: 'Invalid iteration id' });

      const step = parseInt(stepStr, 10);
      if (isNaN(step) || step < 1) return reply.status(400).send({ error: 'Invalid step number' });

      const snapDir = path.join(logsPath, safe(id), 'snapshots', safe(prover));
      if (!fs.existsSync(snapDir)) return reply.status(404).send({ error: 'No snapshots' });

      const stepPadded = step.toString().padStart(3, '0');
      const toFile = `step-${stepPadded}.lean`;
      const toPath = path.join(snapDir, toFile);
      if (!fs.existsSync(toPath)) return reply.status(404).send({ error: `Step ${step} not found` });

      // Determine the "from" file
      let fromFile: string;
      if (step === 1) {
        fromFile = 'baseline.lean';
      } else {
        const prevPadded = (step - 1).toString().padStart(3, '0');
        fromFile = `step-${prevPadded}.lean`;
      }
      const fromPath = path.join(snapDir, fromFile);
      if (!fs.existsSync(fromPath)) return reply.status(404).send({ error: `Previous file ${fromFile} not found` });

      const oldText = fs.readFileSync(fromPath, 'utf-8');
      const newText = fs.readFileSync(toPath, 'utf-8');
      const diff = computeUnifiedDiff(oldText, newText, `a/${fromFile}`, `b/${toFile}`);
      const { added, removed } = countDiffLines(diff);

      return { step, fromFile, toFile, diff, addedLines: added, removedLines: removed } as DiffResult;
    },
  );

  // Return all diffs in sequence for playback preloading
  fastify.get<{ Params: { id: string; prover: string } }>(
    '/api/iterations/:id/snapshots/:prover/diff-all',
    async (req, reply) => {
      const { id, prover } = req.params;
      if (!id.startsWith('iter-')) return reply.status(400).send({ error: 'Invalid iteration id' });

      const snapDir = path.join(logsPath, safe(id), 'snapshots', safe(prover));
      if (!fs.existsSync(snapDir)) return reply.status(404).send({ error: 'No snapshots' });

      const stepFiles = fs.readdirSync(snapDir)
        .filter(f => f.startsWith('step-') && f.endsWith('.lean'))
        .sort();

      const diffs: DiffResult[] = [];
      for (let i = 0; i < stepFiles.length; i++) {
        const fromFile = i === 0 ? 'baseline.lean' : stepFiles[i - 1];
        const toFile = stepFiles[i];
        const fromPath = path.join(snapDir, fromFile);
        const toPath = path.join(snapDir, toFile);
        if (!fs.existsSync(fromPath) || !fs.existsSync(toPath)) continue;

        const oldText = fs.readFileSync(fromPath, 'utf-8');
        const newText = fs.readFileSync(toPath, 'utf-8');
        const diff = computeUnifiedDiff(oldText, newText, `a/${fromFile}`, `b/${toFile}`);
        const { added, removed } = countDiffLines(diff);
        diffs.push({ step: i + 1, fromFile, toFile, diff, addedLines: added, removedLines: removed });
      }

      return diffs;
    },
  );
}
