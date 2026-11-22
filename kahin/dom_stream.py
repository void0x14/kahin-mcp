"""Browser-native DOM observation primitives.

The stream is deliberately implemented in the page with ``MutationObserver``
and a bounded ring buffer. Mirage only transports the observation signal and
asks the page for snapshots/deltas when an agent requests them. This keeps the
data real, prevents a fast page from blocking the Juggler reader, and makes a
lost cursor recoverable through a fresh snapshot.
"""

from __future__ import annotations

import orjson

DOM_STREAM_BINDING_NAME = "__dom_notify_v1"
DOM_STREAM_GLOBAL = "kahin.dom.stream.v1"


# This is intentionally plain page JavaScript. It uses only Web APIs available
# in Firefox/Camoufox and does not invent a CDP DOM domain for Juggler.
DOM_STREAM_INIT_SCRIPT = r"""
(() => {
  const KEY = Symbol.for("kahin.dom.stream.v1");
  const LEGACY_KEY = "__kahin_dom_stream_v1";
  const BINDING = "__dom_notify_v1";
  // Remove the pre-stealth public property when a page is reused by an older
  // init script.  The live stream now lives under a Symbol and is not exposed
  // through Object.getOwnPropertyNames(window).
  if (window[LEGACY_KEY]) {
    try { window[LEGACY_KEY].stop?.(); } catch (_) {}
    try { delete window[LEGACY_KEY]; } catch (_) { window[LEGACY_KEY] = null; }
  }
  if (window[KEY] && window[KEY].version === 1 && window[KEY].active) return;
  if (window[KEY] && window[KEY].version === 1 && !window[KEY].active) {
    try { window[KEY].stop?.(); } catch (_) {}
    try { delete window[KEY]; } catch (_) { window[KEY] = null; }
  }

  const clamp = (value, low, high, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.max(low, Math.min(high, n)) : fallback;
  };

  const textOf = (node, limit = 240) => {
    const text = String(node && (node.innerText || node.textContent || ""))
      .replace(/\s+/g, " ").trim();
    return text.length > limit ? text.slice(0, limit) + "…" : text;
  };

  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== "hidden" && style.display !== "none";
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "img") return "img";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (["button", "submit", "reset"].includes(type)) return "button";
      return "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return null;
  };

  const nameOf = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const labelled = labelledBy.split(/\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map((node) => textOf(node, 120)).join(" ").trim();
      if (labelled) return labelled;
    }
    if (el.labels && el.labels.length) {
      const label = Array.from(el.labels).map((node) => textOf(node, 120)).join(" ").trim();
      if (label) return label;
    }
    const title = el.getAttribute("title");
    if (title) return title.trim();
    const alt = el.getAttribute("alt");
    if (alt) return alt.trim();
    const placeholder = el.getAttribute("placeholder");
    if (placeholder) return placeholder.trim();
    return textOf(el, 160);
  };

  const safeAttributes = (el) => {
    const names = ["id", "name", "type", "role", "href", "target", "placeholder",
      "aria-label", "aria-labelledby", "aria-expanded", "aria-checked", "aria-selected",
      "aria-disabled", "aria-haspopup", "data-testid", "data-test", "for"];
    const result = {};
    for (const name of names) {
      if (!el.hasAttribute(name)) continue;
      // Password values are never returned as page observations.
      result[name] = name === "value" ? "[redacted]" : el.getAttribute(name);
    }
    return result;
  };

  const safeAttributeValue = (el, name, value) => {
    if (name === "value" && el instanceof HTMLInputElement &&
        (el.getAttribute("type") || "").toLowerCase() === "password") {
      return "[redacted]";
    }
    return value;
  };

  const boot = (options) => {
    const old = window[KEY];
    if (old && old.active) {
      if (options && options.maxEvents) old.maxEvents = clamp(options.maxEvents, 32, 2000, old.maxEvents);
      return old;
    }

    const nodeIds = new WeakMap();
    const liveNodes = new Map();
    const events = [];
    const maxMutationRecordsPerCallback = 128;
    let nextNodeId = 1;
    let nextSeq = 0;
    let revision = 0;
    let overflowed = false;
    const streamId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    // Node ids must not collide across documents. A WeakMap alone is not
    // enough because this init script is evaluated again after navigation and
    // the counter starts at one for every new document. Keep the ref compact,
    // but include this stream's generation so an old ref can never resolve to
    // a new document's node by accident.
    const nodeGeneration = streamId.replace(/[^a-z0-9]/gi, "").slice(-8);
    const maxSnapshotBytes = 350000;
    const maxDrainBytes = 350000;
    const state = {
      version: 1,
      active: true,
      streamId,
      maxEvents: clamp(options && options.maxEvents, 32, 2000, 512),
      observer: null,
    };

    const idOf = (node) => {
      if (!node || (node.nodeType !== 1 && node.nodeType !== 3)) return null;
      let id = nodeIds.get(node);
      if (!id) {
        id = `n${nodeGeneration}-${nextNodeId++}`;
        nodeIds.set(node, id);
        liveNodes.set(id, node);
      }
      return id;
    };

    const removeKnown = (node, budget = {remaining: 256}) => {
      if (!node || budget.remaining <= 0) return;
      budget.remaining -= 1;
      const id = nodeIds.get(node);
      if (id) liveNodes.delete(id);
      if (node.childNodes) Array.from(node.childNodes).forEach((child) => removeKnown(child, budget));
    };

    const descriptor = (el, textLimit = 240) => {
      if (!(el instanceof Element)) {
        return {nodeId: idOf(el), nodeType: el && el.nodeType, text: textOf(el, textLimit)};
      }
      const rect = el.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      const item = {
        nodeId: idOf(el),
        tag,
        role: roleOf(el),
        name: nameOf(el),
        text: textOf(el, textLimit),
        visible: visible(el),
        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        attributes: safeAttributes(el),
        actions: [],
      };
      if (["input", "textarea", "select"].includes(tag) || el.isContentEditable) {
        item.value = type === "password" ? "[redacted]" : String(el.value ?? el.textContent ?? "").slice(0, 500);
      }
      if ("disabled" in el) item.disabled = Boolean(el.disabled);
      if ("checked" in el) item.checked = Boolean(el.checked);
      if ("selected" in el) item.selected = Boolean(el.selected);
      if (roleOf(el) === "button" || tag === "a") item.actions.push("click");
      if (roleOf(el) === "textbox") item.actions.push("focus", "type");
      if (tag === "select") item.actions.push("select");
      return item;
    };

    const shallow = (node) => descriptor(node, 160);

    // Mutation delivery runs on the page's main thread, often while a large
    // document is still being parsed. Layout and computed-style reads here
    // turn an observer into a second page renderer and can block navigation.
    // Mutation deltas only need a bounded identity; agents use snapshot for
    // the expensive semantic/geometry view.
    const mutationDescriptor = (node) => {
      if (!(node instanceof Element)) {
        const result = {nodeId: idOf(node), nodeType: node && node.nodeType};
        if (node && node.nodeType === 3) result.text = textOf(node, 120);
        return result;
      }
      return {nodeId: idOf(node), tag: node.tagName.toLowerCase(),
        attributes: safeAttributes(node)};
    };

    const snapshot = (options = {}) => {
      const selector = typeof options.selector === "string" ? options.selector : "";
      const root = selector ? document.querySelector(selector) : document.documentElement;
      if (!root) return {error: "not_found", selector, streamId, cursor: nextSeq, revision};
      const maxNodes = clamp(options.maxNodes, 1, 5000, 800);
      const maxDepth = clamp(options.maxDepth, 1, 32, 12);
      const includeHidden = options.includeHidden === true;
      let count = 0;
      let truncated = false;
      let serializedBytes = 0;
      const walk = (el, depth) => {
        if (count >= maxNodes) { truncated = true; return null; }
        const info = descriptor(el, clamp(options.textLimit, 20, 2000, 240));
        if (!includeHidden && depth > 0 && !info.visible && !info.actions.length) return null;
        // Bound the object before Runtime.evaluate serializes it across the
        // Juggler pipe. maxNodes alone is not a safe byte bound on real pages:
        // one node can carry long text/attributes and a mutation-heavy page
        // can make a modest tree exceed the sidecar's response budget.
        const infoBytes = JSON.stringify(info).length + 32;
        if (count > 0 && serializedBytes + infoBytes > maxSnapshotBytes) {
          truncated = true;
          return null;
        }
        count += 1;
        serializedBytes += infoBytes;
        info.children = [];
        if (depth < maxDepth) {
          for (const child of Array.from(el.children)) {
            const captured = walk(child, depth + 1);
            if (captured) info.children.push(captured);
            if (truncated) break;
          }
        } else if (el.children.length) {
          info.children = [{truncated: true, childCount: el.children.length}];
          truncated = true;
        }
        return info;
      };
      const tree = walk(root, 0);
      return {
        streamId,
        cursor: nextSeq,
        revision,
        url: location.href,
        title: document.title,
        readyState: document.readyState,
        focused: document.activeElement ? idOf(document.activeElement) : null,
        nodeCount: count,
        truncated,
        root: tree,
      };
    };

    const drain = (options = {}) => {
      const reset = Boolean(options.streamId && options.streamId !== streamId);
      const after = reset ? 0 : Math.max(0, Number(options.after) || 0);
      const limit = clamp(options.limit, 1, 500, 100);
      const first = events.length ? events[0].seq : nextSeq + 1;
      const ringDropped = events.length > 0 && after < first - 1;
      const dropped = reset || overflowed || ringDropped;
      overflowed = false;
      const available = events.filter((event) => event.seq > after);
      const selected = [];
      let selectedBytes = 128;
      for (const event of available) {
        if (selected.length >= limit) break;
        const eventBytes = JSON.stringify(event).length + 16;
        if (selected.length > 0 && selectedBytes + eventBytes > maxDrainBytes) break;
        selected.push(event);
        selectedBytes += eventBytes;
      }
      const cursor = selected.length ? selected[selected.length - 1].seq : after;
      return {
        streamId,
        // Return the last sequence actually delivered. Returning nextSeq
        // here would skip pending events when the caller uses a small limit.
        cursor,
        nextSeq,
        revision,
        reset,
        dropped,
        events: selected,
        pending: available.length,
        hasMore: available.length > selected.length,
        url: location.href,
      };
    };

    const notify = () => {
      try {
        if (typeof window[BINDING] === "function") {
          window[BINDING](JSON.stringify({streamId, cursor: nextSeq, revision}));
        }
      } catch (_) {
        // The observer remains useful when the transport is temporarily gone.
      }
    };

    const push = (event) => {
      nextSeq += 1;
      revision += 1;
      events.push(Object.assign({seq: nextSeq, timestamp: Date.now()}, event));
      while (events.length > state.maxEvents) events.shift();
    };

    const mutation = (record) => {
      const target = mutationDescriptor(record.target);
      if (record.type === "attributes") {
        return {type: "attributes", target, attribute: record.attributeName,
          oldValue: safeAttributeValue(record.target, record.attributeName, record.oldValue),
          value: safeAttributeValue(record.target, record.attributeName,
            record.target.getAttribute(record.attributeName))};
      }
      if (record.type === "characterData") {
        return {type: "characterData", target, oldValue: record.oldValue,
          value: textOf(record.target, 500)};
      }
      const addedNodes = Array.from(record.addedNodes);
      const removedNodes = Array.from(record.removedNodes);
      const added = addedNodes.slice(0, 8).map(mutationDescriptor);
      const removed = removedNodes.slice(0, 8).map((node) => {
        const result = mutationDescriptor(node);
        removeKnown(node);
        return result;
      });
      return {type: "childList", target, added, removed,
        addedCount: addedNodes.length, removedCount: removedNodes.length,
        addedTruncated: addedNodes.length > added.length,
        removedTruncated: removedNodes.length > removed.length};
    };

    const pageEvent = (event) => {
      if (!state.active || !(event.target instanceof Element)) return;
      push({type: "event", event: event.type, target: shallow(event.target)});
      notify();
    };
    const pageEventNames = ["input", "change", "focusin", "focusout", "click"];
    pageEventNames.forEach((name) => document.addEventListener(name, pageEvent, true));

    state.observer = new MutationObserver((records) => {
      if (!state.active) return;
      const selected = records.slice(0, maxMutationRecordsPerCallback);
      if (records.length > selected.length) {
        overflowed = true;
        push({type: "overflow", droppedCount: records.length - selected.length});
      }
      selected.forEach((record) => push(mutation(record)));
      if (records.length) notify();
    });
    state.observer.observe(document, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeOldValue: true,
      characterData: true,
      characterDataOldValue: true,
    });
    state.snapshot = snapshot;
    state.drain = drain;
    state.status = () => ({streamId, cursor: nextSeq, nextSeq, revision, pending: events.length,
      active: state.active, dropped: overflowed, url: location.href,
      readyState: document.readyState});
    state.configure = (next) => {
      state.maxEvents = clamp(next && next.maxEvents, 32, 2000, state.maxEvents);
      while (events.length > state.maxEvents) events.shift();
      return state.status();
    };
    state.action = (options = {}) => {
      const nodeId = options.nodeId;
      const action = options.action;
      const el = liveNodes.get(String(nodeId));
      if (!(el instanceof Element) || !el.isConnected) {
        return {error: "stale_node", nodeId, requiresSnapshot: true};
      }
      const allowed = ["click", "focus", "hover", "type", "scroll", "select"];
      if (!allowed.includes(action)) return {error: "unsupported_action", action, allowed};
      const tag = el.tagName.toLowerCase();
      const inputType = String(el.type || "").toLowerCase();
      const editable = el.isContentEditable || tag === "textarea" || (tag === "input" && !["button","checkbox","file","hidden","image","radio","range","reset","submit"].includes(inputType));
      if (action === "type" && (roleOf(el) !== "textbox" || !editable)) {
        return {error: "not_text_input", nodeId, role: roleOf(el) || inputType};
      }
      if (action === "type" && (el.disabled || el.readOnly || el.getAttribute("aria-disabled") === "true")) {
        return {error: "element is disabled or read-only", code: "not_editable", nodeId};
      }
      el.scrollIntoView({block: "center", inline: "center"});
      if (action === "select") {
        if (!(el instanceof HTMLSelectElement)) return {error: "not_select", nodeId};
        const wanted = String(options.text ?? "");
        const option = Array.from(el.options).find((candidate) =>
          candidate.value === wanted || candidate.textContent?.trim() === wanted);
        if (!option) return {error: "option_not_found", nodeId, value: wanted};
        el.value = option.value;
        el.dispatchEvent(new Event("input", {bubbles: true}));
        el.dispatchEvent(new Event("change", {bubbles: true}));
      }
      let needsClear = false;
      let needsEndKey = false;
      if (action === "focus" || action === "type") {
        el.focus();
        if (editable && !el.disabled && !el.readOnly && el.getAttribute("aria-disabled") !== "true") {
          const doc = el.ownerDocument || document;
          const win = doc.defaultView || window;
          const before = el.isContentEditable ? String(el.textContent || "") : String(el.value || "");
          const hasContent = before.length > 0 || Boolean(!el.isContentEditable && el.validity && el.validity.badInput);
          const clear = action === "type" && options.clear === true;
          if (clear && hasContent) {
            if (el.isContentEditable) {
              const r = doc.createRange();
              r.selectNodeContents(el);
              const s = win.getSelection();
              if (s) { s.removeAllRanges(); s.addRange(r); }
              needsClear = true;
            } else if (typeof el.select === "function") {
              el.select();
              needsClear = true;
            }
          } else if (!clear && hasContent) {
            if (el.isContentEditable) {
              const r = doc.createRange();
              r.selectNodeContents(el);
              r.collapse(false);
              const s = win.getSelection();
              if (s) { s.removeAllRanges(); s.addRange(r); }
            } else if (typeof el.setSelectionRange === "function") {
              try { el.setSelectionRange(before.length, before.length); } catch (_) { needsEndKey = true; }
            } else {
              needsEndKey = true;
            }
          }
        }
      }
      const rect = el.getBoundingClientRect();
      let x = rect.x + rect.width / 2;
      let y = rect.y + rect.height / 2;
      let owner = window;
      while (owner !== owner.top) {
        const frame = owner.frameElement;
        if (!frame) break;
        const frameRect = frame.getBoundingClientRect();
        x += frameRect.x;
        y += frameRect.y;
        owner = owner.parent;
      }
      return {ready: true, nodeId, action, tag,
        role: roleOf(el), x, y, scrolled: action === "scroll", needsClear, needsEndKey};
    };
    state.stop = () => {
      state.active = false;
      if (state.observer) state.observer.disconnect();
      pageEventNames.forEach((name) => document.removeEventListener(name, pageEvent, true));
      return state.status();
    };
    window[KEY] = state;
    return state;
  };

  boot({});
})();
"""


def js_call(method: str, params: dict[str, object] | None = None) -> str:
    """Build a safe expression calling the page's DOM stream object."""
    encoded = orjson.dumps(params or {}).decode()
    return f"(() => {{ const s = window[Symbol.for({DOM_STREAM_GLOBAL!r})]; if (!s || !s.{method}) return {{error: 'dom stream unavailable'}}; return s.{method}({encoded}); }})()"
