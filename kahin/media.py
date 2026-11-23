"""media.py — Konu adlı PNG üretimi, redaksiyon, evrensel hedef teslimatı ve gören model köprüsü."""

from __future__ import annotations

import asyncio
import base64
import ftplib
import io
import logging
import os
import re
import shlex
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import url2pathname

import httpx
import orjson
from PIL import Image, ImageDraw, ImageFont

# httpx/httpcore INFO logları URL'deki kimlik bilgilerini ve Telegram bot token'ını stderr'e basmasın.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_TR_MAP = str.maketrans("çğıİöşüÇĞİÖŞÜ", "cgiiosucgiosu")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SENSITIVE_QUERY_KEYS = frozenset({
    "token", "key", "api_key", "apikey", "secret", "password", "pass", "auth", "sig", "signature",
})
_SECRET_TEXT_PATTERNS = (
    re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]*):([^@\s/]+)@", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{4,}", re.IGNORECASE),
    re.compile(r"([?&](?:token|key|api_key|apikey|secret|password|pass|auth|sig|signature)=)[^&\s\"'<>]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", re.IGNORECASE),
    re.compile(r"\bbot\d{5,}:[A-Za-z0-9_-]{15,}\b"),
    re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{15,}\b"),
)
_NON_MULTIMODAL_MODELS = (
    "deepseek", "qwen-coder", "qwen2.5-coder", "qwen3-coder",
    "glm-4-air", "glm-4-flash", "codestral", "o1-mini",
    "llama-3", "mistral-large", "text-only",
)
_MULTIMODAL_MARKERS = ("vl", "vision", "gemini", "claude", "gpt-4o", "gpt-5")
_MAX_DEST_LEN = 4096
_MAX_TOPIC_LEN = 200
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
_TELEGRAM_PHOTO_LIMIT = 9_500_000

_VISUAL_CONTEXT_JS = r"""
((fullPage, collectRedact) => {
  const dpr = Number(window.devicePixelRatio) || 1;
  const sx = fullPage ? (window.scrollX || window.pageXOffset || 0) : 0;
  const sy = fullPage ? (window.scrollY || window.pageYOffset || 0) : 0;
  const rects = [];
  const crossOriginFrames = [];

  if (collectRedact) {
    const kwRe = /password|passwd|pwd|sifre|parola|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|cvv|cvc|otp|totp|pin|ssn|tckn/i;
    const acRe = /password|cc-number|cc-csc|one-time-code/i;
    const secRe = /(?:sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|Bearer\s+[A-Za-z0-9._-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)/;

    const addRect = (x, y, w, h, clip) => {
      if (rects.length >= 100) return;
      let x0 = x, y0 = y, x1 = x + w, y1 = y + h;
      if (clip) {
        x0 = Math.max(x0, clip.x);
        y0 = Math.max(y0, clip.y);
        x1 = Math.min(x1, clip.x + clip.w);
        y1 = Math.min(y1, clip.y + clip.h);
      }
      if (!fullPage) {
        x0 = Math.max(x0, 0);
        y0 = Math.max(y0, 0);
        x1 = Math.min(x1, window.innerWidth);
        y1 = Math.min(y1, window.innerHeight);
      }
      if (x1 - x0 < 4 || y1 - y0 < 4) return;
      rects.push({ x: x0 + sx, y: y0 + sy, w: x1 - x0, h: y1 - y0 });
    };

    const isVisible = (el, win) => {
      const st = win.getComputedStyle(el);
      return Boolean(st && st.display !== "none" && st.visibility !== "hidden" && parseFloat(st.opacity) !== 0);
    };

    const walkDoc = (doc, ox, oy, clip, depth) => {
      if (!doc || !doc.body || depth > 4 || rects.length >= 100) return;
      const win = doc.defaultView || window;
      const seen = new Set();
      const inputs = doc.querySelectorAll("input, textarea, [data-kahin-redact], [data-sensitive]");
      for (const el of inputs) {
        if (rects.length >= 100) break;
        if (!isVisible(el, win)) continue;
        const r = el.getBoundingClientRect();
        if (!r || r.width < 4 || r.height < 4) continue;
        let hit = el.hasAttribute("data-kahin-redact") || el.hasAttribute("data-sensitive");
        if (!hit) {
          const tag = (el.tagName || "").toLowerCase();
          const type = (el.getAttribute("type") || "").toLowerCase();
          if (tag === "input" && type === "password") hit = true;
        }
        if (!hit && acRe.test(el.getAttribute("autocomplete") || "")) hit = true;
        if (!hit) {
          const val = String(el.value ?? "");
          if (val.trim().length > 0) {
            const meta = [
              el.getAttribute("name") || "",
              el.getAttribute("id") || "",
              el.getAttribute("placeholder") || "",
              el.getAttribute("aria-label") || "",
            ].join(" ");
            if (kwRe.test(meta) || secRe.test(val)) hit = true;
          }
        }
        if (hit && !seen.has(el)) {
          seen.add(el);
          addRect(ox + r.left, oy + r.top, r.width, r.height, clip);
        }
      }

      const secReG = new RegExp(secRe.source, "g");
      const tw = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
      let checked = 0;
      let node;
      while ((node = tw.nextNode()) && checked < 2000 && rects.length < 100) {
        checked++;
        const raw = node.nodeValue || "";
        if (raw.length < 16) continue;
        const parent = node.parentElement;
        if (!parent || !isVisible(parent, win)) continue;
        const ptag = (parent.tagName || "").toLowerCase();
        if (ptag === "script" || ptag === "style" || ptag === "noscript") continue;
        secReG.lastIndex = 0;
        let m;
        let usedParentFallback = false;
        while ((m = secReG.exec(raw)) !== null && rects.length < 100) {
          let rect = null;
          try {
            const range = doc.createRange();
            range.setStart(node, m.index);
            range.setEnd(node, Math.min(raw.length, m.index + m[0].length));
            const rr = range.getBoundingClientRect();
            if (rr && rr.width >= 4 && rr.height >= 4) rect = rr;
          } catch (_) {}
          if (!rect && !usedParentFallback && ptag !== "html" && ptag !== "body") {
            const pr = parent.getBoundingClientRect();
            if (pr && pr.width >= 4 && pr.height >= 4 && pr.height <= Math.max(window.innerHeight * 0.85, 600)) {
              rect = pr;
              usedParentFallback = true;
            }
          }
          if (rect) {
            addRect(ox + rect.left, oy + rect.top, rect.width, rect.height, clip);
          }
        }
      }

      const frames = doc.querySelectorAll("iframe, frame");
      for (const fr of frames) {
        if (rects.length >= 100) break;
        if (!isVisible(fr, win)) continue;
        const ir = fr.getBoundingClientRect();
        if (!ir || ir.width < 8 || ir.height < 8) continue;
        const fst = win.getComputedStyle(fr);
        const bl = parseFloat(fst.borderLeftWidth || "0") || 0;
        const bt = parseFloat(fst.borderTopWidth || "0") || 0;
        const br = parseFloat(fst.borderRightWidth || "0") || 0;
        const bb = parseFloat(fst.borderBottomWidth || "0") || 0;
        const fx = ox + ir.left + bl;
        const fy = oy + ir.top + bt;
        const fw = Math.max(0, ir.width - bl - br);
        const fh = Math.max(0, ir.height - bt - bb);
        if (fw < 4 || fh < 4) continue;
        const fclip = clip
          ? {
              x: Math.max(fx, clip.x),
              y: Math.max(fy, clip.y),
              w: Math.max(0, Math.min(fx + fw, clip.x + clip.w) - Math.max(fx, clip.x)),
              h: Math.max(0, Math.min(fy + fh, clip.y + clip.h) - Math.max(fy, clip.y)),
            }
          : { x: fx, y: fy, w: fw, h: fh };
        let sameOriginOk = false;
        try {
          const cd = fr.contentDocument;
          if (cd && cd.body) {
            sameOriginOk = true;
            walkDoc(cd, fx, fy, fclip, depth + 1);
          }
        } catch (_) {}
        if (!sameOriginOk && crossOriginFrames.length < 16) {
          crossOriginFrames.push({
            src: String(fr.src || ""),
            x: fclip.x + sx,
            y: fclip.y + sy,
            w: fclip.w,
            h: fclip.h,
          });
        }
      }
    };

    walkDoc(document, 0, 0, null, 0);
  }

  return {
    url: String(location.href || ""),
    title: String(document.title || ""),
    dpr,
    scrollX: sx,
    scrollY: sy,
    innerWidth: Number(window.innerWidth) || 0,
    innerHeight: Number(window.innerHeight) || 0,
    scrollWidth: Number(document.documentElement?.scrollWidth) || 0,
    scrollHeight: Number(document.documentElement?.scrollHeight) || 0,
    sensitiveRects: rects,
    crossOriginFrames,
  };
})
"""


def _is_under_temp(path: Path) -> bool:
    resolved = path.resolve()
    temp_roots = [Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/var/tmp").resolve()]
    return any(resolved == root or root in resolved.parents for root in temp_roots)


def _kahin_home() -> Path:
    configured = os.environ.get("KAHIN_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    candidate = root / "kahin"
    if _is_under_temp(candidate):
        return Path.home() / ".local" / "share" / "kahin"
    return candidate


def default_media_dir() -> Path:
    """Varsayılan Kahin medya dizini ($KAHIN_HOME/medya). KAHIN_HOME yoksa asla /tmp altına düşmez."""
    return _kahin_home() / "medya"


def validate_destination(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "destination must be a string"
    cleaned = value.strip()
    if not cleaned:
        return None, "destination must not be empty"
    if len(cleaned) > _MAX_DEST_LEN:
        return None, f"destination exceeds {_MAX_DEST_LEN} characters"
    if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        return None, "destination contains invalid control characters"
    if cleaned.lower().startswith("file://"):
        if not url2pathname(urlsplit(cleaned).path).strip():
            return None, "file:// destination requires a non-empty path"
    return cleaned, None


def validate_topic(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "topic must be a string"
    cleaned = value.strip()
    if len(cleaned) > _MAX_TOPIC_LEN:
        return None, f"topic exceeds {_MAX_TOPIC_LEN} characters"
    if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        return None, "topic contains invalid control characters"
    return cleaned or None, None


def resolve_destination(destination: str | None = None) -> str:
    """Öncelik: çağrı parametresi > oturum varsayılanı > env > $KAHIN_HOME/medya."""
    if isinstance(destination, str) and destination.strip():
        return destination.strip()
    from kahin import _state as state  # noqa: PLC0415

    session_dest = getattr(state, "_media_destination", None)
    if isinstance(session_dest, str) and session_dest.strip():
        return session_dest.strip()
    for env_name in ("KAHIN_MEDIA_DESTINATION", "KAHIN_MEDIA_DIR"):
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    return str(default_media_dir())


def _extract_scheme(dest: str) -> str:
    if "://" in dest:
        scheme = dest.split("://", 1)[0].strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
            return scheme
    return "local"


def redact_text(text: str) -> str:
    out = text
    for pat in _SECRET_TEXT_PATTERNS:
        if pat.pattern.startswith("([a-z]"):
            out = pat.sub(r"\1:***@", out)
        elif pat.pattern.startswith(r"\bBasic"):
            out = pat.sub("Basic [REDAKTE]", out)
        elif pat.pattern.startswith("([?&]"):
            out = pat.sub(r"\1[REDAKTE]", out)
        else:
            out = pat.sub("[REDAKTE]", out)
    return out


def sanitize_destination(dest: str) -> str:
    scheme = _extract_scheme(dest)
    if scheme == "local":
        return dest
    if scheme in {"telegram", "tg"}:
        rest = dest.split("://", 1)[1]
        base, frag = (rest.split("#", 1) + [""])[:2]
        path_part, query_part = (base.split("?", 1) + [""])[:2]
        if "@" in path_part:
            _, chat_part = path_part.rsplit("@", 1)
            masked = f"{scheme}://***@{chat_part}"
        else:
            masked = f"{scheme}://{path_part}"
        if query_part:
            pairs = [
                (k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
                for k, v in parse_qsl(query_part, keep_blank_values=True)
            ]
            masked += "?" + urlencode(pairs)
        if frag:
            masked += f"#{frag}"
        return masked
    try:
        parts = urlsplit(dest)
    except ValueError:
        return redact_text(dest)
    netloc = parts.netloc
    if "@" in netloc:
        auth, hostpart = netloc.rsplit("@", 1)
        user = auth.split(":", 1)[0]
        netloc = f"{user}:***@{hostpart}" if user else f"***@{hostpart}"
    query = parts.query
    if query:
        pairs = [
            (k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
            for k, v in parse_qsl(query, keep_blank_values=True)
        ]
        query = urlencode(pairs)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _slugify(value: str, *, max_len: int = 40) -> str:
    if not value:
        return ""
    import unicodedata

    normalized = value.translate(_TR_MAP).lower()
    ascii_text = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_text).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def build_png_filename(
    url: str = "",
    title: str = "",
    topic: str | None = None,
    kind: str | None = None,
) -> str:
    parts: list[str] = []
    host = ""
    path_slug = ""
    if url:
        try:
            parsed = urlparse(url)
            host = _slugify(parsed.hostname or "", max_len=30)
            path_slug = _slugify(parsed.path.strip("/").replace("/", "-"), max_len=28)
        except ValueError:
            pass
    if host:
        parts.append(host)
    subject = _slugify(topic or "", max_len=40) or _slugify(title or "", max_len=40) or path_slug or "sayfa"
    if subject not in parts:
        parts.append(subject)
    kind_slug = _slugify(kind or "", max_len=24)
    if kind_slug:
        parts.append(kind_slug)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    prefix = "_".join(parts)[:88].rstrip("_-") or "sayfa"
    name = f"{prefix}_{stamp}_{suffix}.png"
    return name[:120]


def _flatten_child_frames(node: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(node, dict):
        return out
    for child in node.get("childFrames") or []:
        if not isinstance(child, dict):
            continue
        frame = child.get("frame")
        if isinstance(frame, dict) and isinstance(frame.get("id"), str):
            out.append({"id": frame["id"], "url": str(frame.get("url") or "")})
        out.extend(_flatten_child_frames(child))
    return out


async def _collect_cross_origin_rects(
    engine: Any,
    session_id: str | None,
    cross_frames: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolve_context = getattr(engine, "resolve_context", None)
    if not callable(resolve_context):
        return []
    try:
        tree = await asyncio.wait_for(engine.call("Page.getFrameTree", {}, session_id=session_id), timeout=0.8)
    except Exception:  # noqa: BLE001
        return []
    child_frames = _flatten_child_frames((tree or {}).get("frameTree") if isinstance(tree, dict) else None)
    if not child_frames:
        return []

    rects: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    child_expr = f"({_VISUAL_CONTEXT_JS})(false, true)"
    for cf in cross_frames:
        if not isinstance(cf, dict):
            continue
        src = str(cf.get("src") or "")
        match = next((f for f in child_frames if f["id"] not in used_ids and src and f["url"] == src), None)
        if match is None and src:
            match = next(
                (f for f in child_frames if f["id"] not in used_ids and f["url"] and (src in f["url"] or f["url"] in src)),
                None,
            )
        if match is None:
            match = next((f for f in child_frames if f["id"] not in used_ids and f["url"] not in ("", "about:blank")), None)
        if match is None:
            continue
        used_ids.add(match["id"])
        ctx_id = resolve_context(match["id"], session_id=session_id)
        if ctx_id is None:
            continue
        try:
            sub = await asyncio.wait_for(
                engine.call(
                    "Runtime.evaluate",
                    {"executionContextId": ctx_id, "expression": child_expr, "returnByValue": True},
                    session_id=session_id,
                ),
                timeout=0.8,
            )
        except Exception:  # noqa: BLE001
            continue
        sub_val = ((sub.get("result") or {}).get("value")) if isinstance(sub, dict) else None
        if not isinstance(sub_val, dict):
            continue
        fx, fy = float(cf.get("x") or 0.0), float(cf.get("y") or 0.0)
        fw, fh = float(cf.get("w") or 0.0), float(cf.get("h") or 0.0)
        for sr in sub_val.get("sensitiveRects") or []:
            if not isinstance(sr, dict):
                continue
            try:
                rx0 = max(fx, fx + float(sr["x"]))
                ry0 = max(fy, fy + float(sr["y"]))
                rx1 = min(fx + fw, fx + float(sr["x"]) + float(sr["w"]))
                ry1 = min(fy + fh, fy + float(sr["y"]) + float(sr["h"]))
            except (KeyError, TypeError, ValueError):
                continue
            if rx1 - rx0 >= 4 and ry1 - ry0 >= 4:
                rects.append({"x": rx0, "y": ry0, "w": rx1 - rx0, "h": ry1 - ry0})
    return rects


async def collect_page_visual_context(
    engine: Any,
    *,
    session_id: str | None,
    full_page: bool = False,
    collect_redact: bool = True,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Aktif sekmeden URL/başlık/boyut ve hassas alan dikdörtgenlerini toplar."""
    fallback_url = ""
    target_infos = getattr(engine, "_target_infos", None)
    current_target = getattr(engine, "_current_target", None)
    if isinstance(target_infos, dict) and isinstance(current_target, str):
        info = target_infos.get(current_target) or {}
        if isinstance(info.get("url"), str):
            fallback_url = info["url"]

    fallback: dict[str, Any] = {
        "url": fallback_url,
        "title": "",
        "dpr": 1.0,
        "scrollX": 0.0,
        "scrollY": 0.0,
        "innerWidth": 0,
        "innerHeight": 0,
        "scrollWidth": 0,
        "scrollHeight": 0,
        "sensitiveRects": [],
        "contextComplete": False,
    }
    open_dialogs = getattr(engine, "open_dialogs", None)
    if callable(open_dialogs) and session_id and open_dialogs(session_id):
        return fallback

    expr = f"({_VISUAL_CONTEXT_JS})({'true' if full_page else 'false'}, {'true' if collect_redact else 'false'})"
    try:
        res = await asyncio.wait_for(
            engine.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id=session_id),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return fallback
    val = ((res.get("result") or {}).get("value")) if isinstance(res, dict) else None
    if not isinstance(val, dict):
        return fallback
    rects = val.get("sensitiveRects")
    if not isinstance(rects, list):
        rects = []
        val["sensitiveRects"] = rects
    cross_frames = val.get("crossOriginFrames")
    if collect_redact and isinstance(cross_frames, list) and cross_frames:
        rects.extend(await _collect_cross_origin_rects(engine, session_id, cross_frames))
    val["contextComplete"] = True
    return val


def effective_scale(
    img_w: int,
    img_h: int,
    ctx: dict[str, Any],
    *,
    full_page: bool = False,
) -> tuple[float, float]:
    dpr = float(ctx.get("dpr") or 1.0)
    if full_page:
        cw = float(max(ctx.get("scrollWidth") or 0, ctx.get("innerWidth") or 0))
        sx = (img_w / cw) if cw > 0 else dpr
        return sx, sx
    iw = float(ctx.get("innerWidth") or 0)
    ih = float(ctx.get("innerHeight") or 0)
    sx = (img_w / iw) if iw > 0 else dpr
    sy = (img_h / ih) if ih > 0 else sx
    return sx, sy


def ensure_png(raw: bytes) -> tuple[bytes, int, int]:
    """Ham görüntü baytlarını geçerli PNG baytlarına ve (width, height) boyutuna dönüştürür."""
    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.size
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and img.format == "PNG":
            return raw, width, height
        rgb = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue(), width, height


def redact_png(
    png_bytes: bytes,
    rects: list[dict[str, Any]],
    *,
    scale_x: float = 1.0,
    scale_y: float | None = None,
) -> tuple[bytes, int]:
    """Hassas alan dikdörtgenlerini doğrudan PNG pikselleri üzerinde koyu maskeyle kapatır."""
    if not rects:
        return png_bytes, 0
    sx = scale_x if isinstance(scale_x, (int, float)) and scale_x > 0 else 1.0
    sy = scale_y if isinstance(scale_y, (int, float)) and scale_y > 0 else sx
    applied = 0
    with Image.open(io.BytesIO(png_bytes)) as img:
        base = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img.copy()
        w_img, h_img = base.size
        draw = ImageDraw.Draw(base)
        font = ImageFont.load_default()
        for rect in rects[:100]:
            if not isinstance(rect, dict):
                continue
            try:
                x = float(rect["x"]) * sx
                y = float(rect["y"]) * sy
                w = float(rect["w"]) * sx
                h = float(rect["h"]) * sy
            except (KeyError, TypeError, ValueError):
                continue
            x0 = max(0, int(round(x)) - 2)
            y0 = max(0, int(round(y)) - 2)
            x1 = min(w_img, int(round(x + w)) + 2)
            y1 = min(h_img, int(round(y + h)) + 2)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            draw.rectangle([x0, y0, x1, y1], fill=(17, 24, 39, 255), outline=(239, 68, 68, 255), width=1)
            if x1 - x0 >= 66 and y1 - y0 >= 14:
                draw.text((x0 + 4, y0 + 2), "[REDAKTE]", fill=(248, 250, 252, 255), font=font)
            applied += 1
        if applied == 0:
            return png_bytes, 0
        buf = io.BytesIO()
        base.save(buf, format="PNG")
        return buf.getvalue(), applied


def stitch_suite_png(frames: list[tuple[str, bytes]], header: str) -> tuple[bytes, int, int]:
    """Bir veya daha fazla kareyi etiketli ve redakte edilmiş tek bir tanı PNG suitine birleştirir."""
    panels: list[tuple[str, Image.Image]] = []
    for label, raw in frames:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        max_w, max_h = 1100, 1400
        if img.width > max_w or img.height > max_h:
            ratio = min(max_w / img.width, max_h / img.height)
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
        panels.append((label, img))

    pad = 12
    header_h = 34
    label_h = 24
    side_by_side = len(panels) > 1 and (sum(p.width for _, p in panels) + pad * (len(panels) + 1)) <= 2200

    if side_by_side:
        canvas_w = sum(p.width for _, p in panels) + pad * (len(panels) + 1)
        canvas_h = header_h + pad + label_h + max(p.height for _, p in panels) + pad
    else:
        canvas_w = max(p.width for _, p in panels) + pad * 2
        canvas_h = header_h + pad + sum(label_h + p.height + pad for _, p in panels)

    canvas_w = max(canvas_w, 480)
    canvas = Image.new("RGB", (canvas_w, canvas_h), (15, 23, 42))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    draw.rectangle([0, 0, canvas_w, header_h], fill=(30, 41, 59))
    draw.text((12, 10), redact_text(header)[:140], fill=(248, 250, 252), font=font)

    if side_by_side:
        x = pad
        y = header_h + pad
        for idx, (label, panel) in enumerate(panels, 1):
            draw.rectangle([x, y, x + panel.width, y + label_h], fill=(51, 65, 85))
            draw.text((x + 8, y + 6), f"[{idx}] {redact_text(label)[:80]}", fill=(226, 232, 240), font=font)
            canvas.paste(panel, (x, y + label_h))
            x += panel.width + pad
    else:
        y = header_h + pad
        for idx, (label, panel) in enumerate(panels, 1):
            draw.rectangle([pad, y, pad + panel.width, y + label_h], fill=(51, 65, 85))
            draw.text((pad + 8, y + 6), f"[{idx}] {redact_text(label)[:80]}", fill=(226, 232, 240), font=font)
            canvas.paste(panel, (pad, y + label_h))
            y += label_h + panel.height + pad

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), canvas.width, canvas.height


def _atomic_write_bytes(target: Path, data: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target.resolve()


def _deliver_local(png_bytes: bytes, filename: str, resolved: str) -> dict[str, Any]:
    raw_path = url2pathname(urlsplit(resolved).path) if resolved.lower().startswith("file://") else resolved
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        cwd = Path.cwd()
        base = _kahin_home() if _is_under_temp(cwd) else cwd
        candidate = base / candidate
    if candidate.suffix.lower() == ".png" and not candidate.is_dir():
        final_path = _atomic_write_bytes(candidate, png_bytes)
    else:
        final_path = _atomic_write_bytes(candidate / filename, png_bytes)
    return {
        "ok": True,
        "kind": "local",
        "path": str(final_path),
        "target": str(final_path),
    }


async def _deliver_http(png_bytes: bytes, filename: str, resolved: str, topic: str) -> dict[str, Any]:
    parts = urlsplit(resolved)
    scheme = "https" if parts.scheme.lower() == "webhook" else parts.scheme.lower()
    use_put = parts.fragment.lower() == "put"
    auth = None
    netloc = parts.netloc
    if "@" in netloc:
        _, hostpart = netloc.rsplit("@", 1)
        netloc = hostpart
        if parts.username is not None or parts.password is not None:
            auth = httpx.BasicAuth(unquote(parts.username or ""), unquote(parts.password or ""))
    clean_url = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    sanitized = sanitize_destination(resolved)
    headers = {
        "X-Kahin-Filename": quote(filename, safe=" -_./"),
        "X-Kahin-Topic": quote((topic or "")[:200], safe=" -_./"),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            if use_put:
                resp = await client.put(
                    clean_url,
                    content=png_bytes,
                    headers={**headers, "Content-Type": "image/png"},
                    auth=auth,
                )
            else:
                resp = await client.post(
                    clean_url,
                    files={"file": (filename, png_bytes, "image/png")},
                    data={"filename": filename, "topic": topic or ""},
                    headers=headers,
                    auth=auth,
                )
        if resp.status_code >= 300:
            err_msg = (
                f"HTTP {resp.status_code} (yönlendirme takip edilmedi)"
                if 300 <= resp.status_code < 400
                else redact_text(resp.text[:300])
            )
            return {
                "ok": False,
                "kind": scheme,
                "code": "media_delivery_failed",
                "target": sanitized,
                "status": resp.status_code,
                "location": sanitize_destination(resp.headers.get("location", "")) or None,
                "error": err_msg,
            }
        return {
            "ok": True,
            "kind": scheme,
            "path": None,
            "target": sanitized,
            "status": resp.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": scheme,
            "code": "media_delivery_failed",
            "target": sanitized,
            "error": redact_text(str(exc)),
        }


def _ftp_upload_sync(png_bytes: bytes, filename: str, resolved: str) -> dict[str, Any]:
    parts = urlsplit(resolved)
    scheme = parts.scheme.lower()
    sanitized = sanitize_destination(resolved)
    host = parts.hostname
    if not host:
        return {
            "ok": False,
            "kind": scheme,
            "code": "media_delivery_failed",
            "target": sanitized,
            "error": "FTP host missing",
        }
    port = parts.port or 21
    user = unquote(parts.username) if parts.username else "anonymous"
    passwd = unquote(parts.password) if parts.password else "anonymous@"
    remote_path = unquote(parts.path or "").strip("/")
    if remote_path.lower().endswith(".png"):
        dir_part, _, remote_name = remote_path.rpartition("/")
    else:
        dir_part, remote_name = remote_path, filename

    ftp: ftplib.FTP | None = None
    try:
        ftp = ftplib.FTP_TLS() if scheme == "ftps" else ftplib.FTP()
        ftp.connect(host, port, timeout=20.0)
        ftp.login(user=user, passwd=passwd)
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        if dir_part:
            for segment in [seg for seg in dir_part.split("/") if seg]:
                try:
                    ftp.cwd(segment)
                except ftplib.error_perm:
                    ftp.mkd(segment)
                    ftp.cwd(segment)
        ftp.storbinary(f"STOR {remote_name}", io.BytesIO(png_bytes))
        ftp.quit()
        return {
            "ok": True,
            "kind": scheme,
            "path": None,
            "target": sanitized,
            "remoteFile": remote_name,
        }
    except Exception as exc:  # noqa: BLE001
        err_msg = redact_text(str(exc))
        if passwd and passwd != "anonymous@":
            err_msg = err_msg.replace(passwd, "***")
        return {
            "ok": False,
            "kind": scheme,
            "code": "media_delivery_failed",
            "target": sanitized,
            "error": err_msg,
        }
    finally:
        if ftp is not None:
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass


async def _deliver_telegram(png_bytes: bytes, filename: str, resolved: str, topic: str) -> dict[str, Any]:
    sanitized = sanitize_destination(resolved)
    rest = resolved.split("://", 1)[1]
    base, frag = (rest.split("#", 1) + [""])[:2]
    path_part, query_part = (base.split("?", 1) + [""])[:2]
    if "@" in path_part:
        token, chat_id = path_part.rsplit("@", 1)
    else:
        token = os.environ.get("KAHIN_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        chat_id = path_part
    chat_id = chat_id.strip("/")
    token = token.strip()
    if not token or not chat_id:
        return {
            "ok": False,
            "kind": "telegram",
            "code": "media_delivery_failed",
            "target": sanitized,
            "error": "Telegram hedefi için bot token ve chat_id gerekli",
        }
    q = dict(parse_qsl(query_part))
    caption = q.get("caption") or topic or filename
    thread_id = q.get("thread_id")
    as_doc = frag.lower() == "document" or len(png_bytes) > _TELEGRAM_PHOTO_LIMIT
    method = "sendDocument" if as_doc else "sendPhoto"
    field = "document" if as_doc else "photo"
    data: dict[str, str] = {"chat_id": chat_id, "caption": caption[:1024]}
    if thread_id:
        data["message_thread_id"] = thread_id

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data=data,
                files={field: (filename, png_bytes, "image/png")},
            )
        if resp.status_code >= 400:
            err_txt = redact_text(resp.text[:300]).replace(token, "***")
            return {
                "ok": False,
                "kind": "telegram",
                "code": "media_delivery_failed",
                "target": sanitized,
                "status": resp.status_code,
                "error": err_txt,
            }
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg_id = ((body.get("result") or {}).get("message_id")) if isinstance(body, dict) else None
        return {
            "ok": True,
            "kind": "telegram",
            "path": None,
            "target": f"telegram://***@{chat_id}",
            "messageId": msg_id,
        }
    except Exception as exc:  # noqa: BLE001
        err_txt = redact_text(str(exc)).replace(token, "***")
        return {
            "ok": False,
            "kind": "telegram",
            "code": "media_delivery_failed",
            "target": sanitized,
            "error": err_txt,
        }


async def _deliver_sink_cmd(png_bytes: bytes, filename: str, resolved: str, topic: str) -> dict[str, Any]:
    scheme = _extract_scheme(resolved)
    sanitized = sanitize_destination(resolved)
    sink_cmd = os.environ.get("KAHIN_MEDIA_SINK_CMD", "").strip()
    if not sink_cmd:
        return {
            "ok": False,
            "kind": scheme,
            "code": "unsupported_destination_scheme",
            "target": sanitized,
            "error": f"'{scheme}://' hedefi için KAHIN_MEDIA_SINK_CMD ayarlı değil",
        }
    staging_dir = default_media_dir() / ".staging"
    staged = _atomic_write_bytes(staging_dir / f".sink-{uuid.uuid4().hex[:8]}-{filename}", png_bytes)
    proc: asyncio.subprocess.Process | None = None
    try:
        argv = shlex.split(sink_cmd) + [str(staged), resolved, filename, topic or ""]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode != 0:
            err = redact_text((stderr or stdout or b"").decode("utf-8", errors="replace")[:300])
            return {
                "ok": False,
                "kind": scheme,
                "code": "media_delivery_failed",
                "target": sanitized,
                "error": err or f"sink exited with code {proc.returncode}",
            }
        return {"ok": True, "kind": scheme, "path": None, "target": sanitized}
    except Exception as exc:  # noqa: BLE001
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        return {
            "ok": False,
            "kind": scheme,
            "code": "media_delivery_failed",
            "target": sanitized,
            "error": redact_text(str(exc)),
        }
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass


async def deliver_png(
    png_bytes: bytes,
    filename: str,
    destination: str | None = None,
    *,
    topic: str = "",
) -> dict[str, Any]:
    resolved = resolve_destination(destination)
    scheme = _extract_scheme(resolved)
    if scheme in {"local", "file"}:
        try:
            return _deliver_local(png_bytes, filename, resolved)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "kind": "local",
                "code": "media_delivery_failed",
                "target": resolved,
                "error": redact_text(str(exc)),
            }
    if scheme in {"http", "https", "webhook"}:
        return await _deliver_http(png_bytes, filename, resolved, topic)
    if scheme in {"ftp", "ftps"}:
        return await asyncio.to_thread(_ftp_upload_sync, png_bytes, filename, resolved)
    if scheme in {"telegram", "tg"}:
        return await _deliver_telegram(png_bytes, filename, resolved, topic)
    return await _deliver_sink_cmd(png_bytes, filename, resolved, topic)


def should_run_vision(vision: str = "auto") -> tuple[bool, str]:
    if vision == "always":
        return True, "explicit"
    if vision == "never":
        return False, "disabled"
    mm = os.environ.get("KAHIN_AGENT_MULTIMODAL", "").strip().lower()
    if mm in {"0", "false", "no"}:
        return True, "env_non_multimodal"
    vb = os.environ.get("KAHIN_VISION_BRIDGE", "").strip().lower()
    if vb in {"1", "true", "always", "modlens"}:
        return True, "env_vision_bridge"
    if mm in {"1", "true", "yes"}:
        return False, "env_multimodal"
    for var in ("KAHIN_AGENT_MODEL", "DEEPSEEK_MODEL", "OPENCODE_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", "MODEL"):
        val = os.environ.get(var, "").strip().lower()
        if not val:
            continue
        if any(m in val for m in _MULTIMODAL_MARKERS):
            continue
        if any(m in val for m in _NON_MULTIMODAL_MODELS):
            return True, "model_non_multimodal"
    return False, "multimodal_default"


def _find_modlens_bin() -> str | None:
    configured = os.environ.get("KAHIN_MODLENS_BIN", "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("modlens")
    if found:
        return found
    pnpm_bin = Path.home() / ".local" / "share" / "pnpm" / "bin" / "modlens"
    if pnpm_bin.is_file():
        return str(pnpm_bin)
    return None


async def analyze_with_vision_model(
    png_bytes: bytes,
    *,
    local_path: str | None = None,
    prompt: str | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Multimodal olmayan ajanlar için görüntüyü gören modele aktarır (OCR yok)."""
    focus = prompt or "Sayfadaki görsel düzeni, önemli UI elemanlarını, hataları ve kullanıcıya görünen durumu özetle."
    modlens_bin = _find_modlens_bin()
    if modlens_bin is not None:
        staged: Path | None = None
        img_path = local_path
        if not img_path or not Path(img_path).is_file():
            staging_dir = default_media_dir() / ".staging"
            staged = _atomic_write_bytes(staging_dir / f".vision-{uuid.uuid4().hex[:8]}.png", png_bytes)
            img_path = str(staged)
        proc: asyncio.subprocess.Process | None = None
        try:
            argv = [
                modlens_bin, "analyze", "-i", img_path,
                "--prompt", focus, "--timeout", str(int(timeout * 1000)),
            ]
            model_name = os.environ.get("KAHIN_MODLENS_MODEL", "").strip()
            if model_name:
                argv.extend(["-m", model_name])
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5.0)
            if proc.returncode != 0:
                err = redact_text((stderr or stdout or b"").decode("utf-8", errors="replace")[:300])
                return {"ok": False, "bridge": "modlens", "error": err or f"exit {proc.returncode}"}
            parsed = orjson.loads(stdout)
            res = parsed.get("result") if isinstance(parsed, dict) else None
            meta = parsed.get("meta") if isinstance(parsed, dict) else None
            if not isinstance(res, dict):
                return {"ok": False, "bridge": "modlens", "error": "modlens beklenmeyen JSON döndürdü"}
            # OCR alt alanı kasten dışlanır; yalnızca gören modelin görsel anlayışı döner.
            return {
                "ok": True,
                "bridge": "modlens",
                "provider": parsed.get("provider"),
                "model": meta.get("model") if isinstance(meta, dict) else None,
                "summary": res.get("summary"),
                "semantics": res.get("semantics"),
                "layout": res.get("layout"),
                "visual": res.get("visual"),
                "uncertainty": res.get("uncertainty"),
            }
        except Exception as exc:  # noqa: BLE001
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            return {"ok": False, "bridge": "modlens", "error": redact_text(str(exc))}
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass

    api_url = os.environ.get("KAHIN_VISION_API_URL", "").strip()
    api_model = os.environ.get("KAHIN_VISION_MODEL", "").strip()
    if api_url and api_model:
        api_key = os.environ.get("KAHIN_VISION_API_KEY", "").strip()
        b64 = base64.b64encode(png_bytes).decode("ascii")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": api_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": focus},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(api_url, headers=headers, json=payload)
            if resp.status_code >= 400:
                return {"ok": False, "bridge": "vision_api", "error": f"HTTP {resp.status_code}"}
            data = resp.json()
            summary = (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
            return {"ok": True, "bridge": "vision_api", "model": api_model, "summary": summary}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "bridge": "vision_api", "error": redact_text(str(exc))}

    return {"ok": False, "bridge": "none", "error": "modlens bulunamadı ve KAHIN_VISION_API_URL ayarlı değil"}


async def capture_and_deliver(
    engine: Any,
    *,
    full_page: bool = False,
    topic: str | None = None,
    destination: str | None = None,
    redact: bool = True,
    vision: str = "auto",
    kind: str | None = None,
) -> dict[str, Any]:
    """Aktif sekmeden ekran görüntüsü alır, redakte eder, konu adıyla PNG olarak teslim eder."""
    page = await engine.ensure_page()
    sid = page.get("sessionId") if isinstance(page, dict) else None
    ctx = await collect_page_visual_context(
        engine, session_id=sid, full_page=full_page, collect_redact=redact, timeout=2.0,
    )
    res = await engine.call("Page.captureScreenshot", {"format": "png", "fullPage": full_page}, session_id=sid)
    raw_b64 = res.get("data") if isinstance(res, dict) else None
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        raise RuntimeError(f"captureScreenshot returned no data: {res}")
    raw_bytes = base64.b64decode(raw_b64)
    if len(raw_bytes) > _MAX_SCREENSHOT_BYTES:
        return {
            "error": "Screenshot exceeds Kahin's bounded response size",
            "code": "result_too_large",
            "payloadBytes": len(raw_bytes),
            "maxPayloadBytes": _MAX_SCREENSHOT_BYTES,
            "delivery": {"ok": False, "code": "result_too_large", "error": "Screenshot exceeds bounded response size"},
        }
    if not ctx.get("contextComplete"):
        ctx = await collect_page_visual_context(
            engine, session_id=sid, full_page=full_page, collect_redact=redact, timeout=2.0,
        )
    if redact and not ctx.get("contextComplete"):
        return {
            "error": "Sayfa redaksiyon bağlamı alınamadığı için ekran görüntüsü güvenlik gereği teslim edilmedi (fail-closed); açıkça redact=false ile tekrar deneyebilirsiniz.",
            "code": "redaction_context_unavailable",
            "delivery": {
                "ok": False,
                "code": "redaction_context_unavailable",
                "error": "redaction context unavailable",
            },
        }
    png_bytes, width, height = ensure_png(raw_bytes)
    redacted_regions = 0
    if redact and ctx.get("sensitiveRects"):
        sx, sy = effective_scale(width, height, ctx, full_page=full_page)
        png_bytes, redacted_regions = redact_png(png_bytes, ctx["sensitiveRects"], scale_x=sx, scale_y=sy)
    effective_kind = kind or ("tam-sayfa" if full_page else None)
    filename = build_png_filename(
        url=str(ctx.get("url") or ""),
        title=str(ctx.get("title") or ""),
        topic=topic,
        kind=effective_kind,
    )
    delivery = await deliver_png(png_bytes, filename, destination, topic=topic or str(ctx.get("title") or ""))
    vision_info: dict[str, Any] | None = None
    run_vision, _ = should_run_vision(vision)
    if delivery.get("ok") and run_vision:
        vision_info = await analyze_with_vision_model(
            png_bytes,
            local_path=delivery.get("path"),
            prompt=topic,
        )
    return {
        "path": delivery.get("path"),
        "filename": filename,
        "topic": topic or (str(ctx.get("title") or "").strip() or None),
        "format": "png",
        "mimeType": "image/png",
        "bytes": len(png_bytes),
        "width": width,
        "height": height,
        "fullPage": full_page,
        "redacted": redacted_regions > 0,
        "redactedRegions": redacted_regions,
        "delivery": delivery,
        "vision": vision_info,
    }
