#!/usr/bin/env node
/**
 * dayfile-write.cjs — locked, self-verifying writer for progress-report DAY FILES
 * (vault day-file + optional byte-identical local mirror).
 *
 * Why this exists: the builder agent was trusted, by prose instruction alone, to
 * (a) insert each re-run's H2 block at the TOP of the day-file, (b) mark the
 * previously-newest block superseded exactly once, and (c) keep the local mirror
 * byte-identical to the vault copy. The 2026-07-17 audit found material failures
 * on all of that trust: a bottom-appended block and a stale mirror shipped under a
 * self-reported "byte-identical". Prose invariants are unenforceable; this script
 * mechanizes the write AND re-reads its own output from disk, failing loud
 * (exit 6) when any invariant does not hold. A lying "done" is structurally
 * impossible for a write that goes through this tool — a caller that bypasses the
 * tool and edits the day-file directly is a skill-level contract violation this
 * script cannot see.
 *
 * Usage:
 *   dayfile-write.cjs --vault-target <abs day-file> \
 *                     --block-file <abs path holding ONE new H2 block> \
 *                     --hh-mm HH:MM \
 *                     [--local-target <abs mirror>] \
 *                     [--frontmatter-file <abs preamble>]
 *
 *   --block-file must start "## HH:MM — " (matching --hh-mm) and contain exactly
 *     one top-level H2. --frontmatter-file (full frontmatter + H1 preamble) is
 *     required when the day-file does not exist yet; ignored when it does.
 *
 * Idempotent retry: if the top block already IS the --hh-mm block with the same
 * content, the vault is left untouched and only a stale/missing mirror is healed.
 * This makes "re-run the same command" the correct recovery after a mirror-write
 * failure (exit 4 after the vault rename means the vault WAS updated — the error
 * line says so explicitly). Same header with DIFFERENT content refuses (exit 2):
 * the caller must bump --hh-mm rather than let two runs collide on one label.
 *
 * H2 boundaries are fence-aware: a "## " line inside a ``` / ~~~ code fence is
 * content, not a block boundary — progress-report bodies routinely quote markdown
 * and logs. Residual limitation: a legacy block carrying an UNCLOSED fence can
 * still fuse the blocks after it (block-files are rejected for this; pre-existing
 * file content is taken as-is — byte preservation still holds, only marker
 * placement degrades).
 *
 * Locking: primitives copied from vault-append.cjs (battle-tested there) — mkdir
 * advisory lock, PID liveness via kill(pid,0)/ESRCH (never a clock), owner nonce
 * "<pid>-<rand>", atomic renameSync eviction to a graveyard, ownership re-verified
 * before commit and before release, no signal handlers (run() is fully
 * synchronous; finally{} covers every path a handler could). See the
 * vault-append.cjs header for the full rationale behind each of those choices.
 * Lock namespace: ~/.claude/cache/dayfile-write-<slug>.lock, slug from the vault
 * target basename (same slugging as vault-append). Report basenames now carry the
 * writing session's uuid8 ("Progress Report YYYY-MM-DD <uuid8>.md"), so the lock is
 * per-session rather than per-date: two sessions writing the same project on the
 * same day take DIFFERENT locks, because they write different files and can no
 * longer collide. Legacy day-files (bare "YYYY-MM-DD.md") still slug to a shared
 * per-date lock — over-serialization is the safe direction, and a write is
 * milliseconds against a 5s cap. The lock is NOT keyed off --local-target:
 * callers must keep the mirror path correlated with the vault target (the
 * progress-report skill does).
 *
 * On a verification failure the day-file HAS already been atomically replaced
 * (tmp+rename leaves no partial state to restore); the failure is surfaced as
 * machine-readable JSON on stdout plus a loud stderr line, never repaired
 * silently — the caller decides.
 *
 * Exit codes: 0 ok | 2 usage or bad input | 3 lock timeout | 4 write failure
 *             | 5 lock stolen mid-operation | 6 post-write verification failed
 *             | 1 unexpected internal error (anything not mapped above)
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const LOCK_POLL_MS = 50;
const LOCK_CAP_MS = 5000;
const LOCK_ABANDON_MS = 60 * 60 * 1000; // PID-reuse backstop only — see vault-append header.

const BLOCK_HEADER_RE = /^## \d{2}:\d{2} — /;
// One marker LINE, any time label — matched per-line (fence-aware walkers below),
// never with /m across a whole block, so fenced quotes of a marker don't count.
// Accepts BOTH spellings on purpose. New writes say "(same session)" — a file is
// now per-session, so "same day" no longer describes what happened. Legacy files
// and the frozen audit-window snapshot corpus still carry "(same day)", and this
// walker must keep counting their markers correctly.
const MARKER_LINE_RE = /^> Superseded by the \d{2}:\d{2} block above \((?:same day|same session)\)\.$/;
const FENCE_RE = /^(```|~~~)/;

// Unique per invocation: pid alone is not enough (pids are reused).
const NONCE = `${process.pid}-${crypto.randomBytes(8).toString('hex')}`;

/** Abort carrying an exit code. NEVER call process.exit() while holding the lock —
 *  it does not unwind the stack, so `finally { release() }` would be skipped. */
class Abort extends Error {
  constructor(code, msg) {
    super(msg);
    this.code = code;
  }
}
const die = (code, msg) => { throw new Abort(code, msg); };

function targetSlug(targetPath) {
  return path.basename(targetPath, '.md').replace(/[^\w-]/g, '-');
}

function lockPath(slug) {
  return path.join(os.homedir(), '.claude', 'cache', `dayfile-write-${slug}.lock`);
}

function ownerFile(lock) {
  return path.join(lock, 'owner');
}

function sleep(ms) {
  // Synchronous: this is a short-lived CLI with no event loop to yield to.
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** Atomic grant. mkdir either creates the dir or fails EEXIST — no middle state. */
function tryGrant(lock) {
  try {
    fs.mkdirSync(lock, { recursive: false });
  } catch (e) {
    if (e.code === 'EEXIST') return false;
    if (e.code === 'ENOENT') {
      // cache dir missing — create it and let the caller retry.
      fs.mkdirSync(path.dirname(lock), { recursive: true });
      return false;
    }
    throw e;
  }
  // The grant must be all-or-nothing. If the owner write fails we would leave a lock
  // dir with no owner file — and holderIsDead() deliberately refuses to evict those
  // except via the 1h backstop, so a transient ENOSPC would wedge this day-file for
  // every session for an hour. Roll back: this dir was just created by us and has no
  // published owner, so no racer can be holding it.
  try {
    fs.writeFileSync(ownerFile(lock), NONCE);
  } catch (e) {
    try { fs.rmSync(lock, { recursive: true, force: true }); } catch (_) {}
    throw e; // still fails loud — just without the orphan
  }
  return true;
}

/** True iff we still hold the lock. Any doubt (missing/unreadable owner) => false. */
function ownsLock(lock) {
  try {
    return fs.readFileSync(ownerFile(lock), 'utf8') === NONCE;
  } catch (_) {
    return false;
  }
}

/** Age backstop — used only for PID reuse / a lock with no readable owner. */
function olderThanBackstop(lock) {
  try {
    return Date.now() - fs.statSync(lock).mtimeMs > LOCK_ABANDON_MS;
  } catch (_) {
    return false; // vanished; the next grant attempt settles it
  }
}

/**
 * Is the current holder gone? Decided by PID, not by elapsed time: a slow or
 * suspended holder is ALIVE and must keep its lock, or two writers race the file.
 * kill(pid, 0) sends no signal — it just probes existence. ESRCH => dead.
 * EPERM => alive under another uid.
 */
function holderIsDead(lock) {
  let owner;
  try {
    owner = fs.readFileSync(ownerFile(lock), 'utf8');
  } catch (_) {
    // No owner file: either a half-created lock or one from another tool.
    // Never guess it is dead — only the age backstop may reclaim it.
    return olderThanBackstop(lock);
  }

  const pid = parseInt(String(owner).split('-')[0], 10);
  if (!Number.isInteger(pid) || pid <= 0) return olderThanBackstop(lock);

  try {
    process.kill(pid, 0);
  } catch (e) {
    if (e.code === 'ESRCH') return true; // holder is gone — safe to evict
  }
  return olderThanBackstop(lock); // alive (or EPERM); only PID reuse can free it
}

/**
 * Evict an abandoned lock. renameSync is atomic, so of N racers exactly one
 * succeeds; the rest get ENOENT/ENOTEMPTY and re-poll. Never rmdir-then-mkdir: that
 * is a non-atomic read-modify-write on the very resource mkdir was protecting, and
 * lets two processes both believe they took over.
 * `seq` keeps the graveyard path unique per attempt — a grave left behind by a failed
 * rmSync must never block the next rename with ENOTEMPTY.
 */
function evictStale(lock, seq) {
  const grave = `${lock}.dead.${NONCE}.${seq}`;
  try {
    fs.renameSync(lock, grave);
  } catch (e) {
    // Lost the eviction race — expected, re-poll.
    if (e.code === 'ENOENT' || e.code === 'ENOTEMPTY' || e.code === 'EEXIST') return;
    throw e; // EACCES/EROFS/ENOSPC: a real fault — fail loud, never spin on it.
  }
  try { fs.rmSync(grave, { recursive: true, force: true }); } catch (_) { /* leaks a dir, not a lock */ }
}

/**
 * Bounded acquire. Returns true once the lock is ours, false on timeout.
 * The deadline is checked and the poll sleeps on EVERY iteration: an earlier
 * vault-append revision `continue`d past both on the evict/stat paths, so a
 * persistent rename failure span a 100%-CPU infinite loop that never reached the cap.
 */
function acquire(lock) {
  const deadline = Date.now() + LOCK_CAP_MS;
  let evictSeq = 0;
  for (;;) {
    if (tryGrant(lock)) return true;
    if (holderIsDead(lock)) evictStale(lock, evictSeq++);
    if (Date.now() >= deadline) return false;
    sleep(LOCK_POLL_MS);
  }
}

function release(lock) {
  if (!ownsLock(lock)) return; // stolen or already gone — not ours to delete
  try { fs.rmSync(lock, { recursive: true, force: true }); } catch (_) { /* reclaimed by stale path */ }
}

/** Split off YAML frontmatter so body edits can never touch it. Tolerates CRLF. */
function splitFrontmatter(content) {
  const m = /^---\r?\n/.exec(content);
  if (!m) return { fm: '', body: content };
  const close = /\r?\n---\r?\n/.exec(content.slice(m[0].length));
  if (!close) return { fm: '', body: content };
  const end = m[0].length + close.index + close[0].length;
  return { fm: content.slice(0, end), body: content.slice(end) };
}

/**
 * Split a body into [head (H1 + anything before the first H2), blocks]. Each block
 * carries its exact bytes through to the following H2 (or EOF), so untouched blocks
 * round-trip byte-identically — which is what post-write verification asserts.
 * Fence-aware: "## " inside ``` / ~~~ fences is content, not a boundary.
 */
function splitBlocks(body) {
  const idxs = [];
  let offset = 0;
  let inFence = false;
  for (const raw of body.split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (FENCE_RE.test(line)) {
      inFence = !inFence;
    } else if (!inFence && line.startsWith('## ')) {
      idxs.push(offset);
    }
    offset += raw.length + 1;
  }
  if (idxs.length === 0) return { head: body, blocks: [] };
  const blocks = [];
  for (let i = 0; i < idxs.length; i++) {
    blocks.push(body.slice(idxs[i], i + 1 < idxs.length ? idxs[i + 1] : body.length));
  }
  return { head: body.slice(0, idxs[0]), blocks };
}

function markerLine(hhmm) {
  return `> Superseded by the ${hhmm} block above (same session).`;
}

/** Count REAL marker lines in a block — fenced quotes of a marker don't count. */
function countMarkers(block) {
  let inFence = false;
  let n = 0;
  for (const raw of block.split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (FENCE_RE.test(line)) { inFence = !inFence; continue; }
    if (!inFence && MARKER_LINE_RE.test(line)) n++;
  }
  return n;
}

/** Block content with its real marker lines removed — for same-content comparison. */
function stripMarkers(block) {
  let inFence = false;
  return block.split('\n').filter((raw) => {
    const line = raw.replace(/\r$/, '');
    if (FENCE_RE.test(line)) { inFence = !inFence; return true; }
    return inFence || !MARKER_LINE_RE.test(line);
  }).join('\n');
}

/** Insert the superseded marker directly under the block's header line — works for
 *  conforming (## HH:MM —) and non-conforming (## Correction …) headers alike. */
function addMarker(block, hhmm) {
  const nl = block.indexOf('\n');
  if (nl === -1) return `${block}\n${markerLine(hhmm)}\n`;
  return `${block.slice(0, nl)}\n${markerLine(hhmm)}\n${block.slice(nl + 1)}`;
}

function parseArgs(argv) {
  const takes = new Set(['--vault-target', '--block-file', '--hh-mm', '--local-target', '--frontmatter-file', '--session-uuid']);
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!takes.has(a)) die(2, `[dayfile-write] unknown argument: ${a}`);
    const v = argv[i + 1];
    if (v === undefined || v.startsWith('--')) die(2, `[dayfile-write] ${a} requires a value`);
    opts[a.slice(2).replace(/-/g, '_')] = v;
    i++;
  }
  return opts;
}

function run() {
  const o = parseArgs(process.argv.slice(2));

  if (!o.vault_target || !o.block_file || !o.hh_mm) {
    die(2, 'usage: dayfile-write.cjs --vault-target <path> --block-file <path> --hh-mm HH:MM ' +
           '[--session-uuid <uuid>] ' +
           '[--local-target <path>] [--frontmatter-file <path>]');
  }
  if (!/^\d{2}:\d{2}$/.test(o.hh_mm)) die(2, `[dayfile-write] --hh-mm must be HH:MM, got: ${o.hh_mm}`);
  for (const k of ['vault_target', 'block_file', 'local_target', 'frontmatter_file']) {
    if (o[k] && !path.isAbsolute(o[k])) die(2, `[dayfile-write] --${k.replace(/_/g, '-')} must be an absolute path: ${o[k]}`);
  }

  // Block-file contract — all rejected BEFORE the target is touched, so bad input can
  // never half-write a day-file.
  let blockRaw;
  try {
    blockRaw = fs.readFileSync(o.block_file, 'utf8');
  } catch (e) {
    die(2, `[dayfile-write] cannot read --block-file: ${e.message}`);
  }
  const firstLine = blockRaw.split('\n', 1)[0];
  if (!BLOCK_HEADER_RE.test(firstLine)) {
    die(2, `[dayfile-write] block-file first line must match "## HH:MM — ", got: ${firstLine.slice(0, 60)}`);
  }
  // The header time and --hh-mm feed the same invariants (first-block check, marker
  // text); a mismatch would replace the file and then fail verification — catch it
  // here instead, while the day-file is still untouched.
  if (!firstLine.startsWith(`## ${o.hh_mm} — `)) {
    die(2, `[dayfile-write] block header time does not match --hh-mm ${o.hh_mm}: ${firstLine.slice(0, 60)}`);
  }
  // An unclosed fence would swallow every later block's header on the next re-split,
  // fusing blocks forever — reject it while the input is still just an input.
  if (((blockRaw.match(/^(```|~~~)/gm) || []).length) % 2 !== 0) {
    die(2, '[dayfile-write] block-file has an unclosed code fence');
  }
  const bf = splitBlocks(blockRaw);
  if (bf.blocks.length !== 1) {
    die(2, '[dayfile-write] block-file must contain exactly ONE top-level H2 block');
  }
  const rendered = blockRaw.replace(/\s+$/, '') + '\n';

  // Preamble content is target-independent, so validate it up front too — but whether
  // it is REQUIRED is decided after the lock, where existence is authoritative.
  let fmText = null;
  if (o.frontmatter_file) {
    try {
      fmText = fs.readFileSync(o.frontmatter_file, 'utf8');
    } catch (e) {
      die(2, `[dayfile-write] cannot read --frontmatter-file: ${e.message}`);
    }
    const pre = splitFrontmatter(fmText);
    if (!pre.fm) die(2, '[dayfile-write] frontmatter-file must open with a closed --- fence');
    if (!/^# /m.test(pre.body)) die(2, '[dayfile-write] frontmatter-file must include the H1 line');
    if (splitBlocks(pre.body).blocks.length !== 0) die(2, '[dayfile-write] frontmatter-file must not contain H2 blocks');
  }

  const lock = lockPath(targetSlug(o.vault_target));
  if (!acquire(lock)) {
    die(3, `[dayfile-write] lock timeout after ${LOCK_CAP_MS}ms on ${lock} — day-file not modified. ` +
           'Another session is writing this day-file; retry. If this persists, inspect/remove the lock dir.');
  }

  try {
    const violate = (msg) => {
      process.stdout.write(JSON.stringify({ ok: false, violation: msg }) + '\n');
      die(6, `[dayfile-write] post-write verification FAILED: ${msg}`);
    };

    // Existence is decided INSIDE the lock: two same-second first-runs would otherwise
    // both take the create path and the loser would clobber the winner's block.
    const exists = fs.existsSync(o.vault_target);

    let next;
    let expectedFm;
    let expectedTail = []; // exact bytes every pre-existing block must show post-write
    let supersededMarked = false;

    if (exists) {
      const preContent = fs.readFileSync(o.vault_target, 'utf8');
      const { fm, body } = splitFrontmatter(preContent);
      if (!fm) die(2, `[dayfile-write] existing day-file has no closed frontmatter fence — refusing: ${o.vault_target}`);

      // Session-collision guard. Report filenames carry only the first 8 chars of the
      // session uuid, so two different sessions CAN land on the same path. Appending
      // then would reproduce the exact cross-session stomping that per-session
      // filenames exist to prevent — so an 8-char collision must fail loudly instead.
      //
      // Runs INSIDE the lock and BEFORE any write, so it cannot race a concurrent
      // writer. `fm` is raw unparsed frontmatter TEXT, not a dict — extract by regex.
      if (!o.session_uuid) {
        die(2, `[dayfile-write] --session-uuid is required when the target exists: ${o.vault_target}`);
      }
      const fmUuid = /^session_uuid:\s*(\S+)/m.exec(fm);
      if (!fmUuid) {
        die(2, `[dayfile-write] target has no session_uuid: to compare against — refusing: ${o.vault_target}`);
      }
      if (fmUuid[1] !== o.session_uuid) {
        die(2, `[dayfile-write] session uuid8 COLLISION on ${o.vault_target}: ` +
               `target belongs to session ${fmUuid[1]}, this run is ${o.session_uuid}. ` +
               'Two different sessions share an 8-char prefix; day-file NOT modified.');
      }
      const { head, blocks } = splitBlocks(body);
      if (!/^# /m.test(head)) die(2, `[dayfile-write] existing day-file has no H1 — refusing: ${o.vault_target}`);

      // Idempotent-retry guard. Without it, a retry after "vault written, mirror
      // failed" re-inserts the same block and marks the first copy superseded BY
      // ITSELF — the exact silent-corruption class this tool exists to kill.
      if (blocks.length > 0 && blocks[0].split('\n', 1)[0].startsWith(`## ${o.hh_mm} — `)) {
        const normalize = (b) => stripMarkers(b).replace(/\s+$/, '') + '\n';
        if (normalize(blocks[0]) !== rendered) {
          die(2, `[dayfile-write] a DIFFERENT ${o.hh_mm} block already tops ${o.vault_target} — ` +
                 'two runs collided on one label; bump --hh-mm and retry');
        }
        // Same block already applied: leave the vault alone, heal only the mirror.
        if (o.local_target) {
          if (!ownsLock(lock)) die(5, '[dayfile-write] lock was stolen mid-operation — nothing modified');
          let mirrorBytes = null;
          try { mirrorBytes = fs.readFileSync(o.local_target); } catch (_) {}
          if (mirrorBytes === null || !Buffer.from(preContent, 'utf8').equals(mirrorBytes)) {
            writeAtomic(o.local_target, preContent, true);
          }
          const a = fs.readFileSync(o.vault_target);
          const b = fs.readFileSync(o.local_target);
          if (!a.equals(b)) violate('mirror is not byte-identical to the vault day-file');
        }
        process.stdout.write(JSON.stringify({
          ok: true,
          vault_target: o.vault_target,
          local_target: o.local_target || null,
          blocks_total: blocks.length,
          superseded_marked: false,
          already_applied: true,
          created: false,
        }) + '\n');
        return 0;
      }

      expectedFm = fm;
      if (blocks.length === 0) {
        // Degenerate: frontmatter + H1 but no blocks yet — nothing to supersede.
        next = fm + head.replace(/\s+$/, '') + '\n\n' + rendered;
      } else {
        let first = blocks[0];
        // Never double-mark: a block already superseded by SOME newer block keeps its
        // single marker; only an unmarked previously-newest block gets one.
        if (countMarkers(first) === 0) {
          first = addMarker(first, o.hh_mm);
          supersededMarked = true;
        }
        expectedTail = [first, ...blocks.slice(1)];
        next = fm + head + rendered + '\n' + expectedTail.join('');
      }
    } else {
      if (!fmText) die(2, `[dayfile-write] day-file does not exist and --frontmatter-file was not given: ${o.vault_target}`);
      next = fmText.replace(/\s+$/, '') + '\n\n' + rendered;
      expectedFm = splitFrontmatter(next).fm;
    }
    const expectedCount = expectedTail.length + 1;
    // The new block keeps a trailing blank line only when older blocks follow it.
    const expectedFirst = expectedTail.length > 0 ? rendered + '\n' : rendered;

    // Load-bearing, not belt-and-braces — do not delete as dead code. acquire() is
    // check-then-act: a racer that judged this lock dead, then stalled >50ms before
    // its renameSync, can evict a lock granted after it looked. That robs a live
    // holder (us). The PID-reuse backstop is the other, rarer path here. Either way:
    // fail loud rather than clobber whoever holds the lock now.
    if (!ownsLock(lock)) {
      die(5, '[dayfile-write] lock was stolen mid-operation — day-file NOT modified');
    }

    writeAtomic(o.vault_target, next, !exists);
    if (o.local_target) {
      try {
        writeAtomic(o.local_target, next, true);
      } catch (e) {
        // The vault rename already landed — exit 4 here must never read as "nothing
        // happened", or an operator retries blind. State it, and make the retry safe
        // (the idempotent-retry guard above heals exactly this).
        if (e instanceof Abort) {
          process.stdout.write(JSON.stringify({ ok: false, stage: 'mirror-write', vault_written: true }) + '\n');
          e.message += ' — the VAULT day-file WAS already updated; re-running the same command is safe and will heal the mirror';
        }
        throw e;
      }
    }

    // POST-WRITE VERIFICATION — the whole point of this tool. Re-read from DISK
    // (never trust the in-memory composition) and assert every invariant the
    // builder agent used to claim by prose. Expectations were computed from
    // PRE-write state above, so this is not circular.
    const postRaw = fs.readFileSync(o.vault_target, 'utf8');
    const post = splitFrontmatter(postRaw);
    if (!post.fm) violate('frontmatter fence torn or missing');
    if (post.fm !== expectedFm) violate('frontmatter bytes altered');
    const { head: postHead, blocks: postBlocks } = splitBlocks(post.body);
    if (!/^# /m.test(postHead)) violate('H1 missing above the first H2 block');
    if (postBlocks.length !== expectedCount) {
      violate(`block count is ${postBlocks.length}, expected ${expectedCount}`);
    }
    if (!postBlocks[0].startsWith(`## ${o.hh_mm} — `)) {
      violate(`first H2 block is not the new ${o.hh_mm} block`);
    }
    if (postBlocks[0] !== expectedFirst) {
      violate('new block bytes do not match the block-file content');
    }
    for (let i = 0; i < expectedTail.length; i++) {
      if (postBlocks[i + 1] !== expectedTail[i]) {
        violate(i === 0
          ? 'previously-newest block is not old-content-plus-marker (data loss or bad marker)'
          : `pre-existing block ${i + 1} is not byte-identical (data loss)`);
      }
    }
    if (expectedTail.length > 0) {
      const markers = countMarkers(postBlocks[1]);
      if (markers !== 1) violate(`previously-newest block carries ${markers} superseded markers, expected exactly 1`);
      if (supersededMarked && !postBlocks[1].includes(markerLine(o.hh_mm))) {
        violate(`superseded marker does not name the new ${o.hh_mm} block`);
      }
    }
    if (o.local_target) {
      const a = fs.readFileSync(o.vault_target);
      const b = fs.readFileSync(o.local_target);
      if (!a.equals(b)) violate('mirror is not byte-identical to the vault day-file');
    }

    process.stdout.write(JSON.stringify({
      ok: true,
      vault_target: o.vault_target,
      local_target: o.local_target || null,
      blocks_total: postBlocks.length,
      superseded_marked: supersededMarked,
      already_applied: false,
      created: !exists,
    }) + '\n');
    return 0;
  } finally {
    release(lock);
  }
}

/** tmp + renameSync in the target's own dir => same filesystem => atomic replace. */
function writeAtomic(target, content, mkParents) {
  const tmp = `${target}.tmp.${NONCE}`;
  try {
    if (mkParents) fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(tmp, content, 'utf8');
    fs.renameSync(tmp, target);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch (_) {}
    die(4, `[dayfile-write] write failed for ${target}: ${e.message}`);
  }
}

try {
  process.exitCode = run();
} catch (e) {
  process.stderr.write((e instanceof Abort ? e.message : `[dayfile-write] ${e.stack || e.message}`) + '\n');
  process.exitCode = e instanceof Abort ? e.code : 1;
}
