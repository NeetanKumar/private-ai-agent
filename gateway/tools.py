"""Read-only tool registry. This is the ONLY set of tools that can ever exist.

The allowlist lives in code. Config may narrow it (`tools.enabled`) but a config entry naming a
tool that is not in READ_ONLY_TOOLS is a startup error, so a write or exec tool cannot be added by
configuration, by a client-supplied tool definition, or by anything the model says.

Executors take the authenticated user id from the gateway, never from tool arguments, and touch
only that user's files and that user's document store.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from rag.loaders import TEXT_SUFFIXES, load_text
from rag.types import RagError

_USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SEARCH_SUFFIXES = TEXT_SUFFIXES | {".pdf"}


class ToolError(Exception):
    """Bad arguments or a failed read. Message is safe to show the model."""


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    files_root: Path
    max_result_chars: int
    max_file_bytes: int
    rag: Any = None                      # rag.retrieve.Retriever or None


# ---- argument validation (small JSON-schema subset; unknown keys are rejected) -------------------

def validate_args(schema: Dict[str, Any], args: Any) -> Dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    props = schema.get("properties", {})
    extra = set(args) - set(props)
    if extra:
        raise ToolError(f"unexpected argument(s): {', '.join(sorted(map(str, extra)))}")
    for req in schema.get("required", []):
        if req not in args:
            raise ToolError(f"missing argument: {req}")
    for k, v in args.items():
        spec = props[k]
        t = spec.get("type")
        if t == "string":
            if not isinstance(v, str):
                raise ToolError(f"{k} must be a string")
            if not (spec.get("minLength", 0) <= len(v) <= spec.get("maxLength", 10_000)):
                raise ToolError(f"{k} has an invalid length")
        elif t == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                raise ToolError(f"{k} must be an integer")
            if not (spec.get("minimum", -10**9) <= v <= spec.get("maximum", 10**9)):
                raise ToolError(f"{k} is out of range")
        else:
            raise ToolError(f"{k}: unsupported type")
    return args


# ---- path sandbox -------------------------------------------------------------------------------------

def _user_root(ctx: ToolContext) -> Path:
    if not _USER_RE.fullmatch(ctx.user_id):
        raise ToolError("invalid user")
    return (ctx.files_root / ctx.user_id).resolve()


def resolve_user_path(ctx: ToolContext, rel: str) -> Path:
    """A relative path inside this user's folder. Absolute paths, '..', NUL bytes and symlinks that
    lead outside the folder are all refused."""
    root = _user_root(ctx)
    if "\x00" in rel or rel.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", rel):
        raise ToolError("path must be relative to your files folder")
    if any(part == ".." for part in re.split(r"[\\/]", rel)):
        raise ToolError("path may not contain '..'")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ToolError("path is outside your files folder") from None
    return target


def _clip(text: str, ctx: ToolContext) -> str:
    return text if len(text) <= ctx.max_result_chars else text[: ctx.max_result_chars] + "\n[truncated]"


def _iter_files(ctx: ToolContext):
    root = _user_root(ctx)
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        try:
            r = p.resolve()
            r.relative_to(root)                       # skip symlinks that escape
        except (ValueError, OSError):
            continue
        if r.is_file() and p.suffix.lower() in _SEARCH_SUFFIXES:
            yield p, r


# ---- executors ------------------------------------------------------------------------------------------

async def _list_files(ctx: ToolContext, args: Dict[str, Any]) -> str:
    root = _user_root(ctx)
    names = [str(p.relative_to(root)) for p, _ in _iter_files(ctx)][:200]
    return "\n".join(names) if names else "(no files)"


async def _read_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    p = resolve_user_path(ctx, args["path"])
    if not p.is_file():
        raise ToolError("file not found")
    if p.stat().st_size > ctx.max_file_bytes:
        raise ToolError("file too large")
    try:
        return _clip(load_text(p), ctx)
    except RagError as e:
        raise ToolError(str(e)) from None


async def _search_files(ctx: ToolContext, args: Dict[str, Any]) -> str:
    terms = [t for t in re.findall(r"\w+", args["query"].lower()) if len(t) > 1]
    if not terms:
        raise ToolError("query has no searchable words")
    root = _user_root(ctx)
    out: List[str] = []
    for p, r in _iter_files(ctx):
        if r.stat().st_size > ctx.max_file_bytes:
            continue
        try:
            text = load_text(r)
        except RagError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if all(t in low for t in terms):
                out.append(f"{p.relative_to(root)}:{n}: {line.strip()[:200]}")
                if len(out) >= 20:
                    return _clip("\n".join(out), ctx)
    return _clip("\n".join(out), ctx) if out else "no matches"


async def _context_query(ctx: ToolContext, args: Dict[str, Any]) -> str:
    if ctx.rag is None:
        raise ToolError("document search is not enabled")
    try:
        hits = await ctx.rag.retrieve(ctx.user_id, args["query"], k=args.get("top_k"))
    except RagError:
        raise ToolError("document search is unavailable") from None
    if not hits:
        return "not in documents"
    return _clip("\n\n".join(f"[{h.doc_name} #{h.chunk_idx} score={h.score:.2f}] {h.text}" for h in hits), ctx)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: Dict[str, Any]
    run: Callable[[ToolContext, Dict[str, Any]], Awaitable[str]]

    def definition(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.schema}}


def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


READ_ONLY_TOOLS: Dict[str, ToolSpec] = {t.name: t for t in (
    ToolSpec("list_files", "List the files in the user's files folder.", _obj({}, []), _list_files),
    ToolSpec("read_file", "Read one text, markdown or PDF file from the user's files folder.",
             _obj({"path": {"type": "string", "minLength": 1, "maxLength": 256,
                            "description": "Relative path, e.g. notes/plan.md"}}, ["path"]), _read_file),
    ToolSpec("search_files", "Find lines containing all the given words across the user's files.",
             _obj({"query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"]), _search_files),
    ToolSpec("context_query", "Search the user's ingested private documents and return matching passages.",
             _obj({"query": {"type": "string", "minLength": 1, "maxLength": 500},
                   "top_k": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]), _context_query),
)}


def get_spec(name: str, enabled: List[str]) -> Optional[ToolSpec]:
    """The spec if `name` is both in the code allowlist and enabled by config, else None."""
    return READ_ONLY_TOOLS.get(name) if name in enabled else None


async def execute(name: str, args: Any, ctx: ToolContext, enabled: List[str]) -> str:
    spec = get_spec(name, enabled)
    if spec is None:
        raise KeyError(name)
    return await spec.run(ctx, validate_args(spec.schema, args))
