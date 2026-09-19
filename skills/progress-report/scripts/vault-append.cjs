#!/usr/bin/env node
/**
 * vault-append.cjs — atomic append of a log line to a SHARED vault project page.
 *
 * Why this exists: per-session dedup flags in precompact-report-guard.js un-serialize
 * concurrent progress-report runs. Two agents appending to the same project page via
 * read-modify-write (Edit/Write) can lose a line or tear the YAML frontmatter, which
 * silently breaks the Dataview dashboard. Prose "be atomic" is unenforceable, so the
 * shared-page write is a deterministic script.
 *
 * Usage:
 *   vault-append.cjs <page-path> <line> [--section "## Session Log"]
 *
 * Why a lock at all: the entry must land inside the target section, and real project
 * pages keep sections after it (## Vault Notes), so this is a read-modify-write, not
 * an EOF append. A lock-free O_APPEND would file entries under the wrong heading.
 *
 * Locking (mkdir advisory lock at ~/.claude/cache/vault-append-<projectSlug>.lock):
 *   - mkdir is the only grant primitive — it is atomic, so exactly one process wins.
 *   - Liveness is decided by PID, never by a clock. An earlier revision treated a lock
 *     older than 60s as abandoned; that robs a holder who is merely slow or suspended
 *     (after a laptop resume EVERY in-flight holder looks 60s stale at once), and two
 *     processes then write the same page. <lock>/owner carries "<pid>-<rand>", and a
 *     holder is abandoned only when kill(pid, 0) reports ESRCH. A suspended holder is
 *     alive, so waiters correctly wait, then fail loud at the cap.
 *   - LOCK_ABANDON_MS is only a backstop against PID reuse, not the liveness test.
 *   - Eviction is a single atomic renameSync to a per-attempt graveyard path: exactly
 *     one racer wins, losers get ENOENT and re-poll. (rmdir-then-mkdir double-grants —
 *     both racers "succeed" — which is the bug this replaced.)
 *   - Ownership is re-verified before the commit and before release, so a robbed
 *     process fails loudly (exit 5) rather than clobbering, and never deletes a lock
 *     that is not its own.
 *   - No signal handlers: run() is fully synchronous, so a JS handler cannot execute
 *     during the critical section anyway — registering one only suppresses the default
 *     terminate and makes a wedged process unkillable. finally{} covers every path a
 *     handler could; SIGKILL leaks a lock, which the PID-liveness path then reclaims.
 *
 * Exit codes: 0 ok/no-op | 2 usage or bad input | 3 lock timeout | 4 write failure
 *             | 5 lock stolen mid-operation (page NOT modified)
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const LOCK_POLL_MS = 50;
const LOCK_CAP_MS = 5000;
const LOCK_ABANDON_MS = 60 * 60 * 1000; // PID-reuse backstop only — see header.
const DEFAULT_SECTION = '## Session Log';

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

function projectSlug(pagePath) {
  return path.basename(pagePath, '.md').replace(/[^\w-]/g, '-');
}

function lockPath(slug) {
  return path.join(os.homedir(), '.claude', 'cache', `vault-append-${slug}.lock`);
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
  // except via the 1h backstop, so a transient ENOSPC would wedge this page for every
  // session for an hour. Roll back: this dir was just created by us and has no
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
 * suspended holder is ALIVE and must keep its lock, or two writers race the page.
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
 * The deadline is checked and the poll sleeps on EVERY iteration: an earlier revision
 * `continue`d past both on the evict/stat paths, so a persistent rename failure span
 * a 100%-CPU infinite loop that never reached the cap.
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

/** Insert `line` at the end of the `section` block, creating the section if absent. */
function insertIntoSection(body, section, line) {
  const lines = body.split('\n');
  const headerIdx = lines.findIndex(l => l.trim() === section);

  if (headerIdx === -1) {
    return `${body.replace(/\n+$/, '')}\n\n${section}\n${line}\n`;
  }

  // End of this section = next H2, or EOF.
  let end = lines.length;
  for (let i = headerIdx + 1; i < lines.length; i++) {
    if (/^## /.test(lines[i])) { end = i; break; }
  }

  // Back up past trailing blanks so the entry joins the list, not the gap.
  let at = end;
  while (at > headerIdx + 1 && lines[at - 1].trim() === '') at--;

  lines.splice(at, 0, line);
  return lines.join('\n');
}

function parseArgs(argv) {
  const opts = { section: DEFAULT_SECTION, positional: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--section') {
      const v = argv[i + 1];
      if (v === undefined || v.startsWith('--')) die(2, '[vault-append] --section requires a value');
      if (!v.startsWith('#')) die(2, `[vault-append] --section must be a markdown heading, got: ${v}`);
      opts.section = v;
      i++;
      continue;
    }
    opts.positional.push(argv[i]);
  }
  return opts;
}

function run() {
  const opts = parseArgs(process.argv.slice(2));
  const [pagePath, line] = opts.positional;

  if (!pagePath || !line) {
    die(2, 'usage: vault-append.cjs <page-path> <line> [--section "## Session Log"]');
  }
  if (line.includes('\n')) die(2, '[vault-append] line must not contain newlines');
  if (!fs.existsSync(pagePath)) die(2, `[vault-append] page not found: ${pagePath}`);

  const lock = lockPath(projectSlug(pagePath));
  if (!acquire(lock)) {
    die(3, `[vault-append] lock timeout after ${LOCK_CAP_MS}ms on ${lock} — page not modified. ` +
           'Another session is writing this page; retry. If this persists, inspect/remove the lock dir.');
  }

  try {
    const content = fs.readFileSync(pagePath, 'utf8');
    // Frontmatter is split off and passed through byte-for-byte: this tool appends to
    // the body only, so the index-note keys a Dataview dashboard reads cannot tear.
    const { fm, body } = splitFrontmatter(content);

    // Line-anchored, body-only: a substring match would silently swallow an entry
    // that merely appears inside a longer existing line.
    if (body.split('\n').some(l => l === line)) {
      process.stdout.write('unchanged (entry already present)\n');
      return 0;
    }

    const next = fm + insertIntoSection(body, opts.section, line);

    // Load-bearing, not belt-and-braces — do not delete as dead code. acquire() is
    // check-then-act: a racer that judged this lock dead, then stalled >50ms before
    // its renameSync, can evict a lock granted after it looked. That robs a live
    // holder (us). The PID-reuse backstop is the other, rarer path here. Either way:
    // fail loud rather than clobber whoever holds the lock now.
    if (!ownsLock(lock)) {
      die(5, '[vault-append] lock was stolen mid-operation — page NOT modified');
    }

    const tmp = `${pagePath}.tmp.${NONCE}`;
    try {
      fs.writeFileSync(tmp, next, 'utf8');
      fs.renameSync(tmp, pagePath); // same dir => same filesystem => atomic replace
    } catch (e) {
      try { fs.unlinkSync(tmp); } catch (_) {}
      die(4, `[vault-append] write failed: ${e.message}`);
    }
    process.stdout.write('appended\n');
    return 0;
  } finally {
    release(lock);
  }
}

try {
  process.exitCode = run();
} catch (e) {
  process.stderr.write((e instanceof Abort ? e.message : `[vault-append] ${e.stack || e.message}`) + '\n');
  process.exitCode = e instanceof Abort ? e.code : 1;
}
