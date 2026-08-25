"""
scripts/lib/memory_ingest.py — Shared ingestion utilities (v1.0)

Common helpers for wiki and code ingestion pipelines.
"""
import sys, os, re, time, math, hashlib, uuid
from pathlib import Path
from typing import List

# --- qdrant-client version preflight ----------------------------------------
# The scripts use APIs added in qdrant-client 1.12 (check_compatibility=,
# get_aliases, update_collection_aliases, count(exact=)). A `git pull` that bumps
# requirements.txt does NOT touch a developer's venv, so a teammate on an older
# client would otherwise hit a raw traceback (migration) or a silent-empty
# retrieval (resolver points at a namespaced collection migration never created).
# Every entry script preflights this and prints ONE actionable line instead.
REQUIRED_QDRANT_CLIENT = (1, 12, 0)


def qdrant_client_status() -> "tuple[str, str | None]":
    """('ok' | 'old' | 'missing', installed_version_or_None). 'old' when the installed
    qdrant-client predates REQUIRED_QDRANT_CLIENT. Never raises."""
    try:
        import qdrant_client  # noqa: F401
    except Exception:
        return ("missing", None)
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            v = version("qdrant-client")
        except PackageNotFoundError:
            return ("ok", None)  # importable but no dist metadata — don't false-alarm
    except Exception:
        return ("ok", None)
    nums = re.findall(r"\d+", v)
    parts = tuple(int(n) for n in nums[:3])
    return ("old", v) if parts < REQUIRED_QDRANT_CLIENT else ("ok", v)


def qdrant_client_preflight() -> "str | None":
    """None when the client is OK; otherwise a one-line, actionable message string
    (no traceback) that an entry script can print/report before exiting."""
    status, v = qdrant_client_status()
    if status == "ok":
        return None
    req = ".".join(map(str, REQUIRED_QDRANT_CLIENT))
    if status == "missing":
        return (f"SumelaOS memory: qdrant-client not installed (need >={req}). "
                f"Run: bash scripts/setup-memory.sh")
    return (f"SumelaOS memory: qdrant-client {v} is too old (need >={req}). "
            f"Run: bash scripts/setup-memory.sh")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def get_repo_root() -> Path:
    """Return the repository root.

    Robust across layouts:
      * this framework repo (plugin sits under .sumela/memory-plugins/), and
      * an adopted project where the plugin is vendored under
        .sumela/memory-plugins/<plugin>/scripts/lib/.
    Resolution order: SUMELA_REPO_ROOT env → first ancestor containing .git or a
    top-level .sumela dir → legacy three-levels-up fallback.

    The legacy "three levels up" returned the PLUGIN dir, not the repo root, in
    every layout — silently breaking wiki/code ingestion in adopted projects.
    """
    env = os.getenv("SUMELA_REPO_ROOT")
    if env:
        return Path(env).resolve()
    p = Path(__file__).resolve()
    for parent in p.parents:
        # Walking up from .sumela/memory-plugins/<plugin>/scripts/lib/, no
        # intermediate dir has a .sumela child, so the first match is the real
        # repo root (which contains <repo>/.sumela). .git covers repos that do
        # not keep .sumela at the top level. Never matches the plugin dir.
        if (parent / ".git").exists() or (parent / ".sumela").is_dir():
            return parent
    return p.parent.parent.parent


# --- Per-project collection namespacing -------------------------------------
# A single Qdrant instance is shared across EVERY SumelaOS project on a machine
# (one `sumela-qdrant` container). The three logical collections (chat_history,
# wiki_pages, code_chunks) must therefore be namespaced per-project, or two
# projects silently intermingle their chunks AND a prune in project Y deletes a
# same-path point in project X (file_path/page_path are repo-relative, so
# `src/index.ts` collides). We derive a stable per-install slug and expose every
# collection as `{slug}__{base}`.
#
# Single source of truth: ALL scripts resolve a logical base name through
# resolve_collection_arg(), so the prefix is computed in exactly ONE place (never
# re-derived in bash). The three bare base names are treated as logical aliases
# that resolve to the physical, namespaced collection — callers (skills, hooks)
# keep passing `code_chunks` and get isolation for free.
COLLECTION_BASES = ("chat_history", "wiki_pages", "code_chunks")

# Per-base env override (full physical name). Honored for back-compat / power
# users who pin an explicit collection; otherwise the namespaced default wins.
_BASE_ENV = {
    "chat_history": "QDRANT_COLLECTION",
    "wiki_pages": "WIKI_PAGES_COLLECTION",
    "code_chunks": "CODE_CHUNKS_COLLECTION",
}


def _sanitize_slug(name: str, max_len: "int | None" = 32) -> str:
    """Reduce an arbitrary string to a Qdrant-safe, stable slug component:
    lowercase ASCII [a-z0-9-], runs collapsed to a single '-', trimmed, optionally
    capped at max_len chars. Returns '' if nothing survives (the DERIVE path falls
    back + always appends a full-strength hash, so an empty/degenerate basename never
    collides). The env-override path passes max_len=None: an explicit user prefix must
    NOT be silently truncated, or two distinct long prefixes sharing a 32-char head
    would collapse to the same collection namespace."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if max_len is not None:
        s = s[:max_len]
    return s.strip("-")


def _slug_file(repo_root: Path) -> Path:
    # Gitignored (see scripts/lib/sumela-gitignore.list → .sumela/_migration/), so
    # the slug is machine-local and never committed. Persisted ONCE: re-deriving on
    # every run would silently re-bucket a project into a fresh empty collection the
    # day the derivation algorithm changes in an upgrade — persisting pins it.
    return repo_root / ".sumela" / "_migration" / "collection-prefix"


# Cache keyed by (install root, env-prefix) so changing SUMELA_COLLECTION_PREFIX within
# one process is honored rather than returning a stale first-seen slug.
_slug_cache: "dict[tuple, str]" = {}


def _persist_slug(root: Path, slug: str) -> None:
    """Write the DERIVED slug to the prefix file so the bash pull-hook gate can read it
    WITHOUT re-deriving (closing the two-language drift gap). Only the derived slug is
    persisted — never the SUMELA_COLLECTION_PREFIX env value, or a one-off env use would
    become STICKY (silently re-bucketing the project after the env is unset). For an
    env pin the bash gate's slug won't match the marker and it harmlessly re-runs the
    (idempotent, state-gated) migration. Best-effort + a no-op when already current."""
    f = _slug_file(root)
    try:
        if f.is_file() and f.read_text(encoding="utf-8").strip() == slug:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(slug + "\n", encoding="utf-8")
    except OSError:
        pass  # best-effort; an unwritable .sumela just means we re-derive (same value)


def project_slug(repo_root: "Path | None" = None) -> str:
    """Stable per-install collection-prefix slug.

    Precedence: SUMELA_COLLECTION_PREFIX env (explicit, wins) → persisted
    .sumela/_migration/collection-prefix → derive from the install root's basename
    + an 8-hex digest of its absolute path. The effective slug is then persisted (so
    the bash hook gate reads it rather than re-deriving) and cached per process. The
    install root (the dir containing .sumela, via get_repo_root) is the project
    identity: it distinguishes sibling monorepo-subdir installs that share one git
    toplevel, which a git-toplevel-based slug would wrongly collide.
    """
    root = (repo_root or get_repo_root()).resolve()
    env = (os.getenv("SUMELA_COLLECTION_PREFIX") or "").strip()
    key = (str(root), env)
    if key in _slug_cache:
        return _slug_cache[key]

    if env:
        # Explicit user pin — sanitize but do NOT truncate (avoid two long prefixes
        # colliding on a shared 32-char head). NOT persisted (see _persist_slug).
        slug = _sanitize_slug(env, max_len=None) or "default"
    else:
        slug = ""
        f = _slug_file(root)
        try:
            if f.is_file():
                slug = f.read_text(encoding="utf-8").strip()
        except OSError:
            slug = ""
        if not slug:
            base = _sanitize_slug(root.name) or "repo"
            digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
            slug = f"{base}-{digest}"
        _persist_slug(root, slug)  # persist only the DERIVED slug, never the env pin

    _slug_cache[key] = slug
    return slug


def resolved_collection(base: str, repo_root: "Path | None" = None) -> str:
    """Physical collection name for a logical base. A per-base env override (full
    name) wins for back-compat; otherwise `{project_slug}__{base}`."""
    override = os.getenv(_BASE_ENV.get(base, ""), "")
    if override and override.strip():
        return override.strip()
    return f"{project_slug(repo_root)}__{base}"


def collection_or_alias_exists(client, name: str, aliases=None) -> bool:
    """True if `name` exists as a collection OR as an alias. An ADOPTED legacy
    collection is reachable only through an alias of the namespaced name, which a plain
    collection listing (get_collections) does NOT include — so a collection_exists-only
    check would wrongly conclude the name is free and recreate an empty collection over
    a working alias (the data-loss this feature exists to prevent). `aliases` may be a
    pre-fetched list of (alias_name, collection_name) tuples to avoid a round-trip.
    Takes a LIVE qdrant client; qdrant_client is never imported at module load here."""
    try:
        if client.collection_exists(name):
            return True
    except Exception:
        pass
    try:
        if aliases is None:
            aliases = [(a.alias_name, a.collection_name) for a in client.get_aliases().aliases]
    except Exception:
        return False
    return any(an == name for an, _ in (aliases or []))


def resolve_collection_arg(name: str, repo_root: "Path | None" = None) -> str:
    """Resolve a --collection argument: a known logical base → its namespaced
    physical name; anything else (an already-physical or custom name) → unchanged.
    This lets callers keep passing `code_chunks` while staying project-isolated."""
    if name in COLLECTION_BASES:
        return resolved_collection(name, repo_root)
    return name


# Hostile / non-relative path patterns rejected before any filesystem touch.
# These directories the project OWNS — there is no legitimate need for globs,
# absolute/UNC/drive-qualified paths, home expansion, leading-dash (option
# injection), backslashes, quotes, or control chars. Reject them all.
_BAD_PATH_CHARS = set('*?[]"\'\\\x00')
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def get_extra_ingest_dirs(repo_root: Path) -> List[Path]:
    """Resolve the project-configured EXTRA documentation ingest directories.

    Precedence (first non-empty wins):
      1. env EXTRA_INGEST_DIRS (comma- or colon-separated, repo-relative paths)
      2. .sumela/ingest.conf — one repo-relative path per line; `#` comments and
         blank lines ignored
      3. empty (the framework default — no extra dirs)

    Mechanism in the framework, policy/values in the consumer: the framework ships
    NO paths. Every configured path is validated and must:
      * be repo-relative (no absolute, `~`, drive-letter, UNC, or backslash),
      * contain no `.`/`..`/empty segments, glob chars, quotes, or control chars,
      * resolve to a real directory STRICTLY INSIDE repo_root (symlink targets are
        resolved, so a symlinked dir pointing outside the repo is rejected),
      * not be repo_root itself.
    Invalid or missing paths are skipped with a warning on stderr; this never
    raises (best-effort, clean-tree contract shared with the wiki sync).

    Returns a de-duplicated, order-stable list of absolute Paths inside repo_root.
    """
    repo_root = repo_root.resolve()
    raw: List[str] = []
    env = os.getenv("EXTRA_INGEST_DIRS")
    if env and env.strip():
        raw = re.split(r"[,:]", env)
    else:
        conf = repo_root / ".sumela" / "ingest.conf"
        if conf.is_file():
            for line in conf.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    raw.append(s)

    out: List[Path] = []
    seen = set()
    for item in raw:
        s = item.strip()
        if not s:
            continue
        if (s[0] in "/~-" or _DRIVE_RE.match(s)
                or any(c in _BAD_PATH_CHARS for c in s)
                or any(ord(c) < 32 for c in s)):
            _warn(f"extra ingest path rejected (must be a simple repo-relative dir): {item!r}")
            continue
        if any(seg in ("", ".", "..") for seg in s.split("/")):
            _warn(f"extra ingest path rejected ('.'/'..'/empty segment): {item!r}")
            continue
        cand = (repo_root / s).resolve()
        if cand == repo_root or repo_root not in cand.parents:
            _warn(f"extra ingest path escapes or equals repo root, skipped: {item!r}")
            continue
        if not cand.is_dir():
            _warn(f"extra ingest path not found (skipped): {item!r}")
            continue
        if cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
    return out


def _warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


# --- embedding input bounds --------------------------------------------------
# Ollama loads an embedding model with n_batch = n_ubatch = 2048 by default. An
# embedding model is NON-CAUSAL: bidirectional attention needs the whole sequence in
# ONE ubatch, so llama.cpp cannot split an over-long prompt — it aborts (SIGTRAP) and
# the runner process dies. Ollama then answers 500 to that request AND to every other
# request in flight, so one oversized chunk also kills its concurrent neighbours.
#
# Two independent guards, because either alone is insufficient: NUM_BATCH raises the
# ceiling but leaves an unbounded chunk unbounded, and MAX_TOKENS bounds the input but
# cannot help a caller that overrides it. Measured on qwen3-embedding:0.6b — 2048 is a
# hard cliff (1970 tokens → 200, 2104 tokens → 500, deterministic when uncached).
def _env_num(name: str, default, minimum, cast=int):
    """Read a numeric env var, falling back loudly instead of exploding at import.

    These are read at module scope, so an unparseable value would raise before any
    script's error handling exists — a bare traceback from `git pull` on every hook.
    The floor matters just as much: a negative EMBED_NUM_BATCH survived the old
    `>=` clamp and drove the char-split budget to 1, i.e. a per-character embed storm
    launched from a git hook.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        print(f"[warn] {name}={raw!r} is not a number; using {default}", file=sys.stderr)
        return default
    if isinstance(value, float) and not math.isfinite(value):
        # inf/nan parse cleanly as floats; `SUMELA_EMBED_RETRY_DELAY=inf` would put a
        # git hook's background ingest to sleep forever.
        print(f"[warn] {name}={raw!r} is not finite; using {default}", file=sys.stderr)
        return default
    if value < minimum:
        print(f"[warn] {name}={value} is below the {minimum} floor; using {minimum}",
              file=sys.stderr)
        return minimum
    return value


DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"
EMBED_DIM = 1024                      # qwen3-embedding:0.6b — pinned so a bad response is caught
EMBED_NUM_BATCH = _env_num("SUMELA_EMBED_NUM_BATCH", 8192, 512)
EMBED_RETRY_DELAY_SECONDS = _env_num("SUMELA_EMBED_RETRY_DELAY", 2.0, 0.0, float)

# Concurrent embed requests during a bulk ingest. 4 is measured, not assumed: on 60 real
# code chunks (~2.6 KB each) throughput ran 12.3 chunk/s at 1 worker, 21.1 at 2, 24.3 at
# 4 and 23.9 at 8 — so it saturates at 4 and 8 buys nothing. The gain survives even with
# OLLAMA_NUM_PARALLEL=1 (a single runner slot) because it is PIPELINING, not parallel
# compute: the next requests are already queued at the server, keeping the HTTP round
# trip and client overhead off the critical path. Lower it on a memory-constrained host.
EMBED_MAX_WORKERS = _env_num("SUMELA_EMBED_MAX_WORKERS", 4, 1)

# Word budget per chunk, shared so the two ingest scripts and chunk_text cannot drift.
EMBED_CHUNK_WORDS = 512
EMBED_CHUNK_OVERLAP = 50

# The token budget is DERIVED from the batch size rather than configured beside it.
# Two independently-set numbers carrying an invariant ("budget < batch") drift the day
# somebody lowers one of them, and the symptom — a dead runner — surfaces nowhere near
# the config change. 90% leaves room for the special tokens the server appends.
EMBED_MAX_TOKENS = _env_num("SUMELA_EMBED_MAX_TOKENS", EMBED_NUM_BATCH * 9 // 10, 64)
if EMBED_MAX_TOKENS >= EMBED_NUM_BATCH:
    print(f"[warn] SUMELA_EMBED_MAX_TOKENS ({EMBED_MAX_TOKENS}) must stay below "
          f"SUMELA_EMBED_NUM_BATCH ({EMBED_NUM_BATCH}); clamping.", file=sys.stderr)
    EMBED_MAX_TOKENS = EMBED_NUM_BATCH * 9 // 10


def estimate_tokens(text: str) -> int:
    """HARD upper bound on the token count. Counts UTF-8 BYTES, not characters.

    Qwen (like most modern models) uses BYTE-level BPE: a token covers at least one
    *byte*, not one character. Python's len() counts codepoints, so a character-based
    bound silently under-counts every multi-byte script by up to 4x — and under-counting
    is exactly what kills the runner. Measured live at the 7372-codepoint bound this
    module used to allow: Cyrillic reached 14,740 bytes and common CJK 22,110, and the
    CJK chunk returned HTTP 500 while the estimate still read 7372. CJK is the worst
    case in practice because it has no word spaces, so a whole paragraph collapses to
    one "word" and always takes the character-split path.

    The +2 covers the special tokens the server appends (this model sets add_eos_token).
    Adversarial probing across 17 content classes (digits, byte-fallback PUA, Tags,
    combining marks, ZWJ emoji, the U+FDFD NFKC bomb, CJK Ext-B) measured a peak density
    of 0.928 tokens/byte, so the bound holds with real slack rather than by a hair.
    """
    return len(text.encode("utf-8")) + 2


def _split_oversized(chunk: str, max_tokens: int) -> List[str]:
    """Split a chunk whose estimated tokens exceed max_tokens, by UTF-8 BYTE budget.

    Word-based chunking cannot bound text with few or no whitespace breaks — a minified
    bundle, a base64 blob, a single-line generated file, or any CJK prose — because all
    of those collapse to ONE "word" and sail past any word budget however small.

    Accumulates per CHARACTER while counting BYTES so a multi-byte codepoint is never
    split down the middle (which would emit invalid text and corrupt the stored chunk).
    A single character wider than the whole budget cannot be split further and is
    emitted alone; that needs a budget under ~4 bytes to happen, so it is theoretical.
    """
    if estimate_tokens(chunk) <= max_tokens:
        return [chunk]
    max_bytes = max(1, max_tokens - 2)   # inverse of the bound in estimate_tokens
    out, buf, size = [], [], 0
    for ch in chunk:
        width = len(ch.encode("utf-8"))
        if size + width > max_bytes and buf:
            out.append("".join(buf))
            buf, size = [], 0
        buf.append(ch)
        size += width
    if buf:
        out.append("".join(buf))
    return out


def chunk_text(text: str, size: int = EMBED_CHUNK_WORDS,
               overlap: int = EMBED_CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks of `size` WORDS, then hard-bound each chunk.

    `size` is a word budget, not a token budget — the two differ by up to 4x depending
    on content and script, which is why the token bound exists rather than a smaller
    `size`. The token budget is deliberately NOT a parameter: a caller able to raise it
    could hand the runner a prompt that kills it, and the one thing this module must
    guarantee is that it never emits such a chunk.
    """
    if not text.strip():
        return []
    max_tokens = EMBED_MAX_TOKENS
    words = text.split()
    if len(words) <= size:
        raw = [text]
    else:
        raw = []
        start = 0
        while start < len(words):
            end = min(start + size, len(words))
            raw.append(" ".join(words[start:end]))
            start += size - overlap
    chunks = []
    for c in raw:
        chunks.extend(_split_oversized(c, max_tokens))
    return chunks


class EmbeddingRejected(ValueError):
    """A well-formed 200 carrying an unusable vector. Deliberately NOT retried.

    Unlike a 500 from a runner that died on somebody else's oversized prompt — transient
    collateral damage, worth one retry — this is DETERMINISTIC: a runner loaded without
    embedding support answers every chunk with zeros. Retrying it spends 2 requests and a
    full EMBED_RETRY_DELAY on every chunk of the walk (at 20k chunks over 4 workers,
    hours of pure sleeping in a detached background process launched from a git hook) to
    arrive at the same rejection. That is exactly the cost hole `ollama_preflight` exists
    to prevent, and the preflight cannot see this one: GET /api/tags answers fine.

    Subclasses ValueError so existing callers that catch ValueError are unaffected.
    """


def _vector_is_usable(vector) -> bool:
    """False for an all-zero or non-finite vector — a soft backend failure that the
    dimension check cannot see.

    llama-server emits a CORRECTLY SIZED all-zero vector on internal embedding failures
    (both when the model is loaded without embedding support and when
    `llama_get_embeddings_*` returns nothing), and Ollama's L2 normalization does not
    turn that into an error: sum == 0 yields norm = 1/1e-12, and 0 * 1e12 is still 0, so
    no NaN or Inf ever appears. The result is a 200 carrying a well-formed 1024-dim
    vector of zeros that passes every check this module used to make, gets upserted as a
    normal point, and is then compared under COSINE against every future query — a
    degenerate distance for a chunk retrieval will still confidently return.

    Same family as the empty-array case the dimension check above already covers; this
    is the half that check is blind to.
    """
    total = 0.0
    for x in vector:
        # isinstance FIRST: math.isfinite raises TypeError on a str/None element, which
        # would surface as "must be real number, not str" instead of the message above.
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x):
            return False
        total += x * x
    # `any` guards the theoretical case where every component is below ~1.5e-162 and the
    # sum of squares underflows to 0.0 while the vector is legitimate.
    return total > 0.0 or any(vector)


def get_embedding(text: str, ollama_url: str, model: str = DEFAULT_EMBED_MODEL,
                  timeout: int = 120, retries: int = 1) -> List[float]:
    """Embed one string. Raises on failure so the caller can record it.

    The length bound is enforced HERE, not left to the caller. It used to live only in
    `chunk_text`, which meant any caller that did not chunk first silently opted out of
    the invariant this module defines — and `query-qdrant.py` is exactly that caller: it
    embeds raw user query text. Truncating is the right degradation for a query (an
    8k-token query is degenerate; searching its head beats killing the runner and every
    concurrent request with it) and is a no-op for the ingest paths, which pre-split.

    The retry covers collateral damage: when a runner dies on somebody else's request,
    the concurrent ones also get a 500 and succeed on a second attempt once Ollama
    restarts it.
    """
    import requests
    if estimate_tokens(text) > EMBED_MAX_TOKENS:
        _warn(f"embedding input exceeds the {EMBED_MAX_TOKENS}-token budget "
              f"({estimate_tokens(text)}); truncating to fit")
        text = _split_oversized(text, EMBED_MAX_TOKENS)[0]
    payload = {
        "model": model,
        "prompt": text,
        "options": {"num_batch": EMBED_NUM_BATCH},
    }
    last_error = None
    for attempt in range(max(1, retries + 1)):   # a negative `retries` must not skip the loop
        try:
            resp = requests.post(f"{ollama_url}/api/embeddings", json=payload, timeout=timeout)
            resp.raise_for_status()
            vector = resp.json().get("embedding") or []
            # Ollama answers 200 with an empty array on some soft failures. Unchecked,
            # that reaches PointStruct and fails the whole file's upsert AFTER its old
            # points were deleted — a hole created by a "successful" embed.
            if len(vector) != EMBED_DIM:
                raise ValueError(f"embedding has {len(vector)} dims, expected {EMBED_DIM}")
            if not _vector_is_usable(vector):
                raise EmbeddingRejected(
                    "embedding is all-zero, non-finite or non-numeric — a soft backend "
                    "failure that passes the dimension check")
            return vector
        except EmbeddingRejected:
            raise                   # deterministic — see EmbeddingRejected's docstring
        except Exception as e:      # noqa: BLE001 — re-raised below once retries are spent
            last_error = e
            if attempt < retries:
                time.sleep(EMBED_RETRY_DELAY_SECONDS)
    raise last_error


def ollama_preflight(ollama_url: str, timeout: int = 5) -> "str | None":
    """None when Ollama answers; otherwise a one-line actionable message.

    Without this the ingest discovers a stopped backend one chunk at a time, sleeping
    through its whole retry budget in a detached background process — hours of work
    achieving nothing — and reports every file as broken.
    """
    try:
        import requests
        requests.get(f"{ollama_url}/api/tags", timeout=timeout).raise_for_status()
        return None
    except Exception as e:
        return (f"Ollama is not reachable at {ollama_url} ({type(e).__name__}). "
                f"Start it (`ollama serve`) and re-run; nothing was changed.")


def deterministic_id(key: str, chunk_index: int) -> str:
    """Generate a deterministic, process-independent UUID point ID."""
    hex_str = hashlib.sha256(f"{key}_{chunk_index}".encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(hex=hex_str))


def print_report(title: str, summary_lines: list):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)
    for line in summary_lines:
        print(line)
    print("=" * 60 + "\n")
