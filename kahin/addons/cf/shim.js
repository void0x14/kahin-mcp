// Browser surface for Cloudflare's challenge page, running on QuickJS.
//
// The Cloudflare interstitial is a normal web page: it ships an obfuscated
// "orchestrate" bundle that reads window._cf_chl_opt, runs its sensors and
// talks to /cdn-cgi/challenge-platform/*. The bundle needs a DOM, a clock and
// the network — nothing else. This file provides the DOM and the clock; the
// network comes from the host bridge (__ka_http), which the Python driver
// answers with real responses over the scraper's own TLS profile.
//
// Kahin-owned knobs, read by the host before this file runs:
//   __KA_USER_AGENT  - navigator.userAgent
//   __KA_LOCATION    - absolute URL of the challenged page
//   __KA_HTML_FILE   - file with the challenged page's markup
//
// The surface is deliberately narrow: only what the challenge touches is
// implemented, and a missing API answers with a recording stub so a gap
// shows up as a name instead of a silent undefined.

'use strict';

var __ka_events = [];
var __ka_wlog = [];
var __ka_cookies = {};
var __ka_timers = [];
var __ka_listeners = {};
var __ka_elements = {};
var __ka_all = [];

var __ka_ua = typeof __KA_USER_AGENT === 'string' && __KA_USER_AGENT
  ? __KA_USER_AGENT
  : 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
var __ka_url = typeof __KA_LOCATION === 'string' && __KA_LOCATION
  ? __KA_LOCATION
  : 'https://example.invalid/';

var noop = function () {};

function __ka_note(kind, detail) {
  __ka_events.push(kind + (detail ? ': ' + detail : ''));
  if (__ka_events.length > 400) __ka_events.shift();
}

function __ka_wlog_push(line) {
  __ka_wlog.push(line);
  if (__ka_wlog.length > 400) __ka_wlog.shift();
}

/* Every global read is recorded: the challenge reads the window hundreds of
 * times and the log shows where it stops. */
var __ka_probe = new Proxy(globalThis, {
  get: function (target, key) {
    if (key === '__ka_probe' || key === '__ka_wlog' || key === '__ka_events') return target[key];
    __ka_wlog_push('g:' + String(key));
    var value = target[key];
    if (value === undefined && typeof key === 'string' && key.charAt(0) !== '_') {
      __ka_wlog_push('g-missing:' + key);
    }
    return value;
  },
  set: function (target, key, value) {
    target[key] = value;
    return true;
  }
});

/* ------------------------------------------------------------ URL parsing */

function __ka_parse_url(href) {
  var out = {
    protocol: 'https:', host: '', hostname: '', port: '',
    pathname: '/', search: '', hash: '', origin: '', href: String(href || '')
  };
  var m = /^([a-zA-Z][a-zA-Z0-9+.-]*:)?\/\/([^/?#]*)([^?#]*)?(\?[^#]*)?(#.*)?$/.exec(String(href || ''));
  if (!m) {
    out.pathname = String(href || '/');
    return out;
  }
  out.protocol = m[1] ? m[1].toLowerCase() : 'https:';
  var authority = m[2] || '';
  var at = authority.lastIndexOf('@');
  if (at >= 0) authority = authority.slice(at + 1);
  out.host = authority;
  out.hostname = authority.split(':')[0];
  if (authority.indexOf(':') >= 0) out.port = authority.split(':')[1];
  out.pathname = m[3] || '/';
  out.search = m[4] || '';
  out.hash = m[5] || '';
  out.origin = out.protocol + '//' + out.host;
  return out;
}

function __ka_absolute(target, base) {
  var next = String(target || '');
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(next)) return next;
  var b = __ka_parse_url(base || location_href());
  if (next.charAt(0) === '/') return b.origin + next;
  if (next.charAt(0) === '#') return b.origin + b.pathname + b.search + next;
  var dir = b.pathname.replace(/[^\/]*$/, '');
  return b.origin + dir + next;
}

function location_href() {
  return location && location._href ? location._href : __ka_url;
}

/* ------------------------------------------------------------ URLSearchParams */

function URLSearchParams(init) {
  this._pairs = [];
  if (typeof init === 'string') {
    var s = init.charAt(0) === '?' ? init.slice(1) : init;
    if (s) {
      s.split('&').forEach(function (pair) {
        if (!pair) return;
        var eq = pair.indexOf('=');
        var k = eq >= 0 ? pair.slice(0, eq) : pair;
        var v = eq >= 0 ? pair.slice(eq + 1) : '';
        this._pairs.push([__ka_decode_uri(k), __ka_decode_uri(v)]);
      }, this);
    }
  } else if (init && typeof init === 'object') {
    for (var key in init) {
      if (Object.prototype.hasOwnProperty.call(init, key)) {
        this._pairs.push([String(key), String(init[key])]);
      }
    }
  }
}
URLSearchParams.prototype.append = function (k, v) { this._pairs.push([String(k), String(v)]); };
URLSearchParams.prototype.set = function (k, v) {
  var key = String(k), seen = false;
  for (var i = 0; i < this._pairs.length; i++) {
    if (this._pairs[i][0] === key) {
      if (!seen) { this._pairs[i][1] = String(v); seen = true; }
      else this._pairs.splice(i--, 1);
    }
  }
  if (!seen) this._pairs.push([key, String(v)]);
};
URLSearchParams.prototype.get = function (k) {
  var key = String(k);
  for (var i = 0; i < this._pairs.length; i++) if (this._pairs[i][0] === key) return this._pairs[i][1];
  return null;
};
URLSearchParams.prototype.getAll = function (k) {
  var key = String(k), out = [];
  for (var i = 0; i < this._pairs.length; i++) if (this._pairs[i][0] === key) out.push(this._pairs[i][1]);
  return out;
};
URLSearchParams.prototype.has = function (k) { return this.get(k) !== null; };
URLSearchParams.prototype.delete = function (k) {
  var key = String(k);
  this._pairs = this._pairs.filter(function (p) { return p[0] !== key; });
};
URLSearchParams.prototype.toString = function () {
  return this._pairs.map(function (p) {
    return encodeURIComponent(p[0]).replace(/%20/g, '+') + '=' +
           encodeURIComponent(p[1]).replace(/%20/g, '+');
  }).join('&');
};
URLSearchParams.prototype.entries = function () {
  var out = [];
  this._pairs.forEach(function (p) { out.push([p[0], p[1]]); });
  return out;
};
URLSearchParams.prototype.forEach = function (fn, thisArg) {
  for (var i = 0; i < this._pairs.length; i++) {
    fn.call(thisArg, this._pairs[i][1], this._pairs[i][0], this);
  }
};
URLSearchParams.prototype.keys = function () {
  var out = [];
  for (var i = 0; i < this._pairs.length; i++) out.push(this._pairs[i][0]);
  return out;
};
URLSearchParams.prototype.values = function () {
  var out = [];
  for (var i = 0; i < this._pairs.length; i++) out.push(this._pairs[i][1]);
  return out;
};
URLSearchParams.prototype[Symbol.iterator] = URLSearchParams.prototype.entries;

function __ka_decode_uri(s) {
  try { return decodeURIComponent(String(s).replace(/\+/g, ' ')); } catch (e) { return String(s); }
}

function URL(href, base) {
  var resolved = base ? __ka_absolute(href, base) : String(href || '');
  var p = __ka_parse_url(resolved);
  this.href = resolved;
  this.protocol = p.protocol;
  this.host = p.host;
  this.hostname = p.hostname;
  this.port = p.port;
  this.pathname = p.pathname;
  this.search = p.search;
  this.hash = p.hash;
  this.origin = p.origin;
  var self = this;
  Object.defineProperty(this, 'searchParams', {
    get: function () { return new URLSearchParams(self.search); }
  });
}
URL.prototype.toString = function () { return this.href; };

/* ---------------------------------------------------------------- base64 */

var __BT = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function btoa(s) {
  s = String(s); var out = '';
  for (var i = 0; i < s.length; i += 3) {
    var a = s.charCodeAt(i), b = i + 1 < s.length ? s.charCodeAt(i + 1) : 0,
        c = i + 2 < s.length ? s.charCodeAt(i + 2) : 0;
    out += __BT[a >> 2] + __BT[((a & 3) << 4) | (b >> 4)]
         + (i + 1 < s.length ? __BT[((b & 15) << 2) | (c >> 6)] : '=')
         + (i + 2 < s.length ? __BT[c & 63] : '=');
  }
  return out;
}
function atob(s) {
  s = String(s); var out = '', i = 0;
  while (i < s.length) {
    var e1 = __BT.indexOf(s[i++]), e2 = __BT.indexOf(s[i++]),
        e3 = __BT.indexOf(s[i++]), e4 = __BT.indexOf(s[i++]);
    out += String.fromCharCode((e1 << 2) | (e2 >> 4));
    if (e3 >= 0) out += String.fromCharCode(((e2 & 15) << 4) | (e3 >> 2));
    if (e4 >= 0) out += String.fromCharCode((e3 & 3) << 6 | e4);
  }
  return out;
}

/* ------------------------------------------------------------ utf8 codec */

function TextEncoder() {}
TextEncoder.prototype.encode = function (s) {
  var out = [], str = String(s);
  for (var i = 0; i < str.length; i++) {
    var c = str.charCodeAt(i);
    if (c < 0x80) out.push(c);
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
    else if (c >= 0xd800 && c < 0xdc00 && i + 1 < str.length) {
      var c2 = str.charCodeAt(i + 1);
      var cp = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00);
      out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
      i++;
    } else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
  }
  return new Uint8Array(out);
};
function TextDecoder() {}
TextDecoder.prototype.decode = function (bytes) {
  var out = '', i = 0;
  if (!bytes) return '';
  if (typeof bytes === 'string') return bytes;
  while (i < bytes.length) {
    var b = bytes[i++];
    if (b < 0x80) out += String.fromCharCode(b);
    else if (b < 0xe0) out += String.fromCharCode(((b & 0x1f) << 6) | (bytes[i++] & 63));
    else if (b < 0xf0) out += String.fromCharCode(((b & 0x0f) << 12) | ((bytes[i++] & 63) << 6) | (bytes[i++] & 63));
    else {
      var cp = ((b & 0x07) << 18) | ((bytes[i++] & 63) << 12) | ((bytes[i++] & 63) << 6) | (bytes[i++] & 63);
      cp -= 0x10000;
      out += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
    }
  }
  return out;
};

/* --------------------------------------------------------------- location */

var __ka_parsed = __ka_parse_url(__ka_url);
var location = {
  _href: __ka_url,
  protocol: __ka_parsed.protocol,
  host: __ka_parsed.host,
  hostname: __ka_parsed.hostname,
  port: __ka_parsed.port,
  pathname: __ka_parsed.pathname,
  search: __ka_parsed.search,
  hash: __ka_parsed.hash,
  origin: __ka_parsed.origin,
  ancestorOrigins: { length: 0 },
  assign: function (u) { this.href = String(u); },
  replace: function (u) { this.href = String(u); },
  reload: noop,
  toString: function () { return this._href; }
};
/* document.location and window.location are the SAME object in a browser.
 * The challenge compares them by identity, so they must not be two
 * separate objects here either. */
Object.defineProperty(location, 'href', {
  get: function () { return location._href; },
  set: function (v) {
    location._href = String(v);
    __ka_note('navigate', String(v));
  }
});

/* A history entry change moves the page: location follows, like in a
 * browser. The challenge rewrites the URL before it builds its request. */
function __ka_navigate(target) {
  if (target === undefined || target === null || target === '') return;
  var next = __ka_absolute(target, location._href);
  var parsed = __ka_parse_url(next);
  location._href = next;
  location.pathname = parsed.pathname;
  location.search = parsed.search;
  location.hash = parsed.hash;
  document.URL = next;
  document.documentURI = next;
  __ka_note('navigate', next);
}

/* -------------------------------------------------------------- navigator */

var navigator = {
  userAgent: __ka_ua,
  appVersion: '5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  appName: 'Netscape',
  appCodeName: 'Mozilla',
  platform: 'Linux x86_64',
  vendor: 'Google Inc.',
  vendorSub: '',
  product: 'Gecko',
  productSub: '20030107',
  language: 'en-US',
  languages: ['en-US', 'en'],
  onLine: true,
  cookieEnabled: true,
  doNotTrack: null,
  hardwareConcurrency: 8,
  deviceMemory: 8,
  maxTouchPoints: 0,
  webdriver: false,
  pdfViewerEnabled: true,
  plugins: {
    length: 5,
    item: function (i) { return this[i] || null; },
    namedItem: function (n) {
      var names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer', 'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
      for (var i = 0; i < names.length; i++) if (names[i] === n) return this[i];
      return null;
    },
    0: { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    1: { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    2: { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    3: { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    4: { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
  },
  mimeTypes: {
    length: 2,
    item: function (i) { return this[i] || null; },
    namedItem: function (n) {
      var names = ['application/pdf', 'text/pdf'];
      for (var i = 0; i < names.length; i++) if (names[i] === n) return this[i];
      return null;
    },
    0: { type: 'application/pdf', suffixes: ['pdf'], description: 'Portable Document Format' },
    1: { type: 'text/pdf', suffixes: ['pdf'], description: 'Portable Document Format' }
  },
  connection: { effectiveType: '4g', rtt: 50, downlink: 10, saveData: false },
  javaEnabled: function () { return false; },
  sendBeacon: function () { return true; },
  storage: { estimate: function () { return Promise.resolve({ quota: 1e9, usage: 0 }); } },
  permissions: {
    query: function () { return Promise.resolve({ state: 'prompt', onchange: null }); }
  },
  userAgentData: {
    brands: [
      { brand: 'Chromium', version: '131' },
      { brand: 'Not_A Brand', version: '24' }
    ],
    mobile: false,
    platform: 'Linux',
    getHighEntropyValues: function () {
      return Promise.resolve({
        architecture: 'x86', bitness: '64', model: '', platform: 'Linux',
        platformVersion: '6.5.0', uaFullVersion: '131.0.6778.86', fullVersionList: []
      });
    }
  }
};

var screen = {
  width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
  colorDepth: 24, pixelDepth: 24, deviceXDPI: 96,
  orientation: { angle: 0, type: 'landscape-primary' }
};

var innerWidth = 1920, innerHeight = 947, outerWidth = 1920, outerHeight = 1040;
var devicePixelRatio = 1;

/* ---------------------------------------------------------------- crypto */

var crypto = (function () {
  var seed = 0x2f6e2b1;
  function next() {
    seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5; seed |= 0;
    return (seed >>> 0) / 4294967296;
  }
  return {
    getRandomValues: function (arr) {
      for (var i = 0; i < arr.length; i++) arr[i] = Math.floor(next() * 256) & 0xff;
      return arr;
    },
    randomUUID: function () {
      var hex = '0123456789abcdef', out = '';
      for (var i = 0; i < 36; i++) {
        if (i === 8 || i === 13 || i === 18 || i === 23) { out += '-'; continue; }
        if (i === 14) { out += '4'; continue; }
        out += hex[Math.floor(next() * 16)];
      }
      return out;
    },
    subtle: {
      digest: function () { return Promise.reject(new Error('subtle.digest unavailable')); },
      encrypt: function () { return Promise.reject(new Error('subtle.encrypt unavailable')); },
      decrypt: function () { return Promise.reject(new Error('subtle.decrypt unavailable')); }
    }
  };
})();

/* ---------------------------------------------------------------- timers */

function setTimeout(fn, delay) {
  var args = Array.prototype.slice.call(arguments, 2);
  __ka_timers.push(function () {
    try { fn.apply(null, args); } catch (e) { __ka_note('timer-error', (e && e.name ? e.name + ': ' + e.message : String(e)) + ' | ' + (e && e.stack ? String(e.stack).split('\n').slice(0, 3).join(' <> ') : '')); }
  });
  return __ka_timers.length;
}
function setInterval(fn, delay) {
  var args = Array.prototype.slice.call(arguments, 2);
  __ka_timers.push(function () {
    try { fn.apply(null, args); } catch (e) { __ka_note('timer-error', (e && e.name ? e.name + ': ' + e.message : String(e)) + ' | ' + (e && e.stack ? e.stack : '')); }
  });
  return __ka_timers.length;
}
function clearTimeout() {}
function clearInterval() {}
function queueMicrotask(fn) {
  __ka_timers.push(function () { try { fn(); } catch (e) {} });
}
function requestAnimationFrame(fn) {
  __ka_timers.push(function () { try { fn(0); } catch (e) {} });
  return __ka_timers.length;
}
function cancelAnimationFrame() {}

var __ka_perf_log = [];
function __ka_perf_entry(name, type, start) {
  return {
    name: String(name),
    entryType: String(type),
    startTime: start,
    duration: Math.max(0, performance.now() - start),
    initiatorType: type === 'navigation' ? 'navigation' : 'script',
    workerStart: 0, fetchStart: start, domainLookupStart: 0, domainLookupEnd: 0,
    connectStart: 0, connectEnd: 0, secureConnectionStart: 0,
    requestStart: start, responseStart: start, responseEnd: performance.now(),
    transferSize: 0, encodedBodySize: 0, decodedBodySize: 0,
    redirectStart: 0, redirectEnd: 0, nextHopProtocol: 'h2', serverTiming: []
  };
}
function __ka_perf_entries() {
  var out = [];
  for (var i = 0; i < __ka_perf_log.length; i++) out.push(__ka_perf_log[i]);
  out.push(__ka_perf_entry(location_href(), 'navigation', 0));
  return out;
}
/* The challenge reads the page's own resource timeline: the navigation
 * entry and the bundle fetch. A real browser has them, so the shim
 * records them too — the challenge measures the load timing. */
var performance = {
  timeOrigin: 0,
  now: function () { return Date.now() - this.timeOrigin; },
  timing: { navigationStart: 0, loadEventEnd: 0, domainLookupEnd: 0 },
  mark: noop, measure: noop, clearMarks: noop, clearMeasures: noop,
  getEntries: function () { return __ka_perf_entries(); },
  getEntriesByType: function (type) {
    var wanted = String(type);
    return __ka_perf_entries().filter(function (e) { return e.entryType === wanted; });
  },
  getEntriesByName: function (name) {
    var wanted = String(name);
    return __ka_perf_entries().filter(function (e) { return e.name === wanted; });
  },
  memory: { usedJSHeapSize: 10485760, totalJSHeapSize: 33554432, jsHeapSizeLimit: 2147483648 }
};

var console = {
  log: function () { __ka_note('console.log', Array.prototype.map.call(arguments, String).join(' ')); },
  warn: function () { __ka_note('console.warn', Array.prototype.map.call(arguments, String).join(' ')); },
  error: function () { __ka_note('console.error', Array.prototype.map.call(arguments, String).join(' ')); },
  info: function () {}, debug: function () {}, trace: function () {}, table: function () {}
};

/* --------------------------------------------------------------- storage */

function __ka_store() {
  var data = {};
  return {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
    setItem: function (k, v) { data[k] = String(v); },
    removeItem: function (k) { delete data[k]; },
    clear: function () { data = {}; },
    key: function (i) { var keys = Object.keys(data); return i < keys.length ? keys[i] : null; },
    get length() { return Object.keys(data).length; }
  };
}
var localStorage = __ka_store();
var sessionStorage = __ka_store();

/* --------------------------------------------------------------- cookies */

function __ka_cookie_seed(raw) {
  if (typeof raw !== 'string' || !raw) return;
  raw.split(';').forEach(function (pair) {
    pair = pair.trim();
    if (!pair) return;
    var eq = pair.indexOf('=');
    if (eq <= 0) return;
    __ka_cookies[pair.slice(0, eq).trim()] = pair.slice(eq + 1).trim();
  });
}
__ka_cookie_seed(typeof __KA_COOKIES === 'string' ? __KA_COOKIES : '');

function __ka_cookie_string() {
  var out = [];
  for (var k in __ka_cookies) {
    if (Object.prototype.hasOwnProperty.call(__ka_cookies, k)) out.push(k + '=' + __ka_cookies[k]);
  }
  return out.join('; ');
}

function __ka_apply_set_cookie(value) {
  var first = String(value).split(';')[0];
  var eq = first.indexOf('=');
  if (eq <= 0) return;
  var name = first.slice(0, eq).trim();
  var val = first.slice(eq + 1).trim();
  if (name) __ka_cookies[name] = val;
}

/* --------------------------------------------------------------- history */

var history = {
  length: 1, state: null,
  pushState: function (s, t, u) { history.state = s; __ka_navigate(u); },
  replaceState: function (s, t, u) { history.state = s; __ka_navigate(u); },
  go: noop, back: noop, forward: noop
};

/* ---------------------------------------------------------------- events */

function Event(type) { this.type = type; this.defaultPrevented = false; }
Event.prototype.preventDefault = function () { this.defaultPrevented = true; };
Event.prototype.stopPropagation = function () {};
Event.prototype.stopImmediatePropagation = function () {};
Event.prototype.initEvent = function (t) { this.type = t; };
function CustomEvent(type, init) { Event.call(this, type); this.detail = (init && init.detail) || null; }
CustomEvent.prototype = Object.create(Event.prototype);
function UIEvent(type, init) { Event.call(this, type); }
UIEvent.prototype = Object.create(Event.prototype);
function MouseEvent(type, init) { UIEvent.call(this, type); }
MouseEvent.prototype = Object.create(UIEvent.prototype);
function ProgressEvent(type) { Event.call(this, type); this.lengthComputable = false; this.loaded = 0; this.total = 0; }
ProgressEvent.prototype = Object.create(Event.prototype);

var EventTarget = function () { this.__ka_listeners = {}; };
EventTarget.prototype.addEventListener = function (t, f) {
  if (!f) return;
  (this.__ka_listeners[t] = this.__ka_listeners[t] || []).push(f);
};
EventTarget.prototype.removeEventListener = function (t, f) {
  var list = this.__ka_listeners[t];
  if (!list) return;
  var i = list.indexOf(f);
  if (i >= 0) list.splice(i, 1);
};
EventTarget.prototype.dispatchEvent = function (ev) {
  var list = this.__ka_listeners[ev.type] || [];
  for (var i = 0; i < list.length; i++) {
    try { list[i].call(this, ev); } catch (e) { __ka_note('listener-error', String(e)); }
  }
  /* An on<type> property is a listener too, like in a browser. */
  var handler = this['on' + ev.type];
  if (typeof handler === 'function') {
    try { handler.call(this, ev); } catch (e) { __ka_note('handler-error', String(e)); }
  }
  return true;
};

function addEventListener(t, f) {
  (__ka_listeners[t] = __ka_listeners[t] || []).push(f);
}
function removeEventListener(t, f) {
  var list = __ka_listeners[t];
  if (!list) return;
  var i = list.indexOf(f);
  if (i >= 0) list.splice(i, 1);
}
function dispatchEvent(ev) {
  var list = __ka_listeners[ev.type] || [];
  for (var i = 0; i < list.length; i++) {
    try { list[i](ev); } catch (e) { __ka_note('listener-error', String(e)); }
  }
  var handler = globalThis['on' + ev.type] || __ka_probe['on' + ev.type];
  if (typeof handler === 'function') {
    try { handler(ev); } catch (e) { __ka_note('handler-error', String(e)); }
  }
  return true;
}

/* ----------------------------------------------------------------- nodes */

function Node() {}
Node.ELEMENT_NODE = 1; Node.TEXT_NODE = 3; Node.DOCUMENT_NODE = 9;

function Element(tag) {
  EventTarget.call(this);
  this.tagName = String(tag || 'div').toUpperCase();
  this.nodeName = this.tagName;
  this.nodeType = 1;
  this.style = { setProperty: noop, getPropertyValue: function () { return ''; }, cssText: '' };
  this.attributes = {};
  this.childNodes = [];
  this.children = [];
  this.parentNode = null;
  this.ownerDocument = null;
  this.innerHTML = '';
  this.innerText = '';
  this.textContent = '';
  this.value = '';
  this.id = '';
  this.className = '';
  this.src = '';
  this.href = '';
  this.nonce = '';
  this.async = false;
  this.defer = false;
  this.crossOrigin = null;
  this.referrerPolicy = '';
  this.type = '';
  this.width = 0;
  this.height = 0;
  this.nextSibling = null;
  this.previousSibling = null;
  this.firstChild = null;
  this.lastChild = null;
  this.nextElementSibling = null;
  this.previousElementSibling = null;

  var self = this;
  this.classList = {
    add: function (c) {
      var parts = String(self.className || '').split(/\s+/).filter(Boolean);
      if (parts.indexOf(String(c)) < 0) parts.push(String(c));
      self.className = parts.join(' ');
    },
    remove: function (c) {
      self.className = String(self.className || '').split(/\s+/)
        .filter(function (p) { return p && p !== String(c); }).join(' ');
    },
    toggle: function (c, force) {
      var has = this.contains(c);
      if (force === undefined ? has : !force) this.remove(c); else this.add(c);
      return !has;
    },
    contains: function (c) {
      return String(self.className || '').split(/\s+/).indexOf(String(c)) >= 0;
    },
    item: function (i) { return String(self.className || '').split(/\s+/).filter(Boolean)[i] || null; },
    get length() { return String(self.className || '').split(/\s+/).filter(Boolean).length; }
  };
}
Element.prototype = Object.create(EventTarget.prototype);

Element.prototype.setAttribute = function (k, v) {
  this.attributes[k] = String(v);
  if (k === 'class') this.className = String(v);
  if (k === 'id') { this.id = String(v); __ka_elements[String(v)] = this; }
  if (k === 'src' && this.tagName === 'SCRIPT') {
    /* The property has to follow the attribute, like in a browser, and the
     * load starts once the script is in the document. */
    this.src = String(v);
    if (this.parentNode) __ka_load_script(String(v), this);
  }
};
Element.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
};
Element.prototype.removeAttribute = function (k) { delete this.attributes[k]; };
Element.prototype.hasAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attributes, k);
};
Element.prototype.appendChild = function (c) {
  if (!c) return c;
  c.parentNode = this;
  this.childNodes.push(c);
  this.children.push(c);
  this.firstChild = this.firstChild || c;
  this.lastChild = c;
  if (c.__ka_on_append) c.__ka_on_append();
  return c;
};
Element.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i >= 0) { this.childNodes.splice(i, 1); this.children.splice(i, 1); }
  return c;
};
Element.prototype.insertBefore = function (c) { return this.appendChild(c); };
Element.prototype.replaceChild = function (c) { return c; };
Element.prototype.cloneNode = function () { return new Element(this.tagName); };
Element.prototype.contains = function () { return false; };
Element.prototype.hasChildNodes = function () { return this.childNodes.length > 0; };
Element.prototype.getElementsByTagName = function () { return []; };
Element.prototype.getElementsByClassName = function () { return []; };
Element.prototype.querySelector = function () { return null; };
Element.prototype.querySelectorAll = function () { return []; };
Element.prototype.getBoundingClientRect = function () {
  return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
};
Element.prototype.getClientRects = function () { return []; };
Element.prototype.focus = noop;
Element.prototype.blur = noop;
Element.prototype.click = function () { this.dispatchEvent(new MouseEvent('click')); };
Element.prototype.getContext = function () { return __ka_canvas_context(); };
Element.prototype.toDataURL = function () { return 'data:image/png;base64,iVBORw0KGgo='; };
Element.prototype.attachShadow = function () { return __ka_shadow_root(); };
Element.prototype.animate = function () { return { finished: Promise.resolve(), cancel: noop }; };
Element.prototype.scrollIntoView = noop;
Element.prototype.getAnimations = function () { return []; };
Element.prototype.setPointerCapture = noop;
Element.prototype.releasePointerCapture = noop;
Element.prototype.hasPointerCapture = function () { return false; };
Element.prototype.insertAdjacentHTML = function () { return null; };
Element.prototype.closest = function () { return null; };
Element.prototype.matches = function () { return false; };
Element.prototype.remove = function () {
  if (this.parentNode && this.parentNode.removeChild) this.parentNode.removeChild(this);
};

function HTMLElement() {}
HTMLElement.prototype = Object.create(Element.prototype);

function __ka_shadow_root() {
  var root = new Element('shadow-root');
  root.mode = 'closed';
  root.host = null;
  return root;
}

function __ka_canvas_context() {
  /* A real 2D context returns non-empty pixels; the challenge fingerprints
   * the canvas, so the surface has to look painted. */
  var painted = false;
  return {
    fillStyle: '#000000', strokeStyle: '#000000', globalAlpha: 1,
    font: '10px sans-serif', textBaseline: 'alphabetic',
    canvas: null,
    fillRect: function () { painted = true; },
    clearRect: noop, strokeRect: noop, beginPath: noop,
    closePath: noop, moveTo: noop, lineTo: noop, arc: noop, arcTo: noop,
    bezierCurveTo: noop, quadraticCurveTo: noop, rect: noop,
    fill: function () { painted = true; },
    stroke: noop, clip: noop, save: noop, restore: noop, translate: noop,
    rotate: noop, scale: noop, transform: noop, setTransform: noop,
    measureText: function (t) {
      return { width: String(t).length * 6, actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 2 };
    },
    fillText: function () { painted = true; },
    strokeText: noop,
    drawImage: function () { painted = true; },
    createLinearGradient: function () { return { addColorStop: noop }; },
    createRadialGradient: function () { return { addColorStop: noop }; },
    createPattern: function () { return null; },
    getImageData: function (x, y, w, h) {
      var data = new Uint8ClampedArray(Math.max(1, w * h * 4));
      if (painted) {
        for (var i = 0; i < data.length; i += 4) {
          data[i] = 32; data[i + 1] = 64; data[i + 2] = 128; data[i + 3] = 255;
        }
      }
      return { data: data, width: w, height: h };
    },
    putImageData: noop,
    createImageData: function (w, h) {
      return { data: new Uint8ClampedArray(Math.max(1, w * h * 4)), width: w, height: h };
    },
    isPointInPath: function () { return false; },
    setLineDash: noop, getLineDash: function () { return []; }
  };
}

/* -------------------------------------------------------------- document */

var document = new EventTarget();
document.nodeType = 9;
document.readyState = 'loading';
document.referrer = '';
document.title = '';
document.domain = __ka_parsed.hostname;
document.URL = __ka_parsed.href;
document.documentURI = __ka_parsed.href;
document.characterSet = 'UTF-8';
document.charset = 'UTF-8';
document.compatMode = 'CSS1Compat';
document.contentType = 'text/html';
document.visibilityState = 'visible';
document.hidden = false;
document.designMode = 'off';
document.dir = 'ltr';
document.currentScript = null;
document.body = new Element('body');
document.head = new Element('head');
document.documentElement = new Element('html');
document.body.parentNode = document.documentElement;
document.head.parentNode = document.documentElement;
document.defaultView = null;
document.location = location;
/* document.location is window.location in a browser — same identity. */
Object.defineProperty(document, 'location', {
  configurable: true,
  get: function () { return location; },
  set: function (v) { location.href = String(v); }
});
document.forms = [];
document.scripts = [];
document.links = [];
document.images = [];
document.implementation = {
  createHTMLDocument: function () { return document; },
  hasFeature: function () { return true; }
};
document.fonts = {
  ready: Promise.resolve(null),
  status: 'loaded',
  check: function () { return true; },
  load: function () { return []; },
  add: function () {},
  delete: function () {},
  forEach: function () {},
  size: 0
};
document.createEvent = function (t) {
  var ev = new Event(t || 'Event');
  ev.initEvent = function (n) { this.type = n; };
  return ev;
};
document.createTextNode = function (t) {
  var n = new Node();
  n.nodeType = 3; n.textContent = String(t); n.nodeName = '#text';
  return n;
};
document.createDocumentFragment = function () { return new Element('fragment'); };
document.createComment = function (t) { var n = new Node(); n.nodeType = 8; n.textContent = String(t); return n; };
document.createRange = function () {
  return {
    setStart: noop, setEnd: noop, collapse: noop, selectNode: noop,
    createContextualFragment: function () { return document.createElement('div'); },
    getBoundingClientRect: function () {
      return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
    commonAncestorContainer: document.body
  };
};
document.getElementById = function (id) {
  return __ka_elements[String(id)] || null;
};
document.getElementsByTagName = function (tag) {
  var t = String(tag).toLowerCase();
  if (t === 'head') return [document.head];
  if (t === 'body') return [document.body];
  if (t === 'html') return [document.documentElement];
  var out = [];
  for (var i = 0; i < __ka_all.length; i++) {
    if (__ka_all[i].tagName.toLowerCase() === t) out.push(__ka_all[i]);
  }
  return out;
};
document.getElementsByClassName = function (cls) {
  var out = [];
  for (var i = 0; i < __ka_all.length; i++) {
    if (__ka_all[i].classList && __ka_all[i].classList.contains(String(cls))) out.push(__ka_all[i]);
  }
  return out;
};
document.getElementsByName = function () { return []; };
document.querySelector = function (sel) {
  for (var i = 0; i < __ka_all.length; i++) {
    if (__ka_matches(__ka_all[i], sel)) return __ka_all[i];
  }
  return null;
};
document.querySelectorAll = function (sel) {
  var out = [];
  for (var i = 0; i < __ka_all.length; i++) {
    if (__ka_matches(__ka_all[i], sel)) out.push(__ka_all[i]);
  }
  return out;
};
document.elementFromPoint = function () { return null; };
document.elementsFromPoint = function () { return []; };
document.hasFocus = function () { return true; };
document.execCommand = function () { return false; };
document.adoptNode = function (node) { return node; };
document.write = function () {
  __ka_note('document.write', Array.prototype.map.call(arguments, String).join(''));
};
document.writeln = document.write;
document.open = noop;
document.close = noop;
document.createNodeIterator = function () {
  return { nextNode: function () { return null; }, detach: noop };
};
document.createTreeWalker = function () {
  return { nextNode: function () { return null; }, detach: noop };
};

Object.defineProperty(document, 'cookie', {
  configurable: true,
  get: function () { return __ka_cookie_string(); },
  set: function (v) {
    var s = String(v);
    var first = s.split(';')[0];
    var eq = first.indexOf('=');
    if (eq > 0) {
      var name = first.slice(0, eq).trim();
      var val = first.slice(eq + 1).trim();
      if (name) {
        if (val === '' || /expires=Thu, 01 Jan 1970/i.test(s)) delete __ka_cookies[name];
        else __ka_cookies[name] = val;
      }
    }
    __ka_note('cookie-set', s.slice(0, 120));
  }
});

function __ka_matches(el, sel) {
  var text = String(sel == null ? '' : sel).trim();
  if (!text) return false;
  var parts = text.split(',');
  for (var p = 0; p < parts.length; p++) {
    var one = parts[p].trim();
    var m = /^([a-zA-Z][A-Za-z0-9-]*)?((?:[.#][A-Za-z0-9_-]+)*)(?:\[([A-Za-z0-9_-]+)(?:=["']?([^"'\]]*)["']?)?\])?$/.exec(one);
    if (!m) continue;
    var tag = m[1];
    if (tag && el.tagName !== tag.toUpperCase()) continue;
    var rest = m[2] || '';
    var re = /([.#])([A-Za-z0-9_-]+)/g, mm, ok = true;
    while ((mm = re.exec(rest)) !== null) {
      if (mm[1] === '#') { if (el.id !== mm[2]) { ok = false; break; } }
      else if (!el.classList || !el.classList.contains(mm[2])) { ok = false; break; }
    }
    if (ok && m[3]) {
      var actual = el.getAttribute ? el.getAttribute(m[3]) : null;
      if (actual === null || actual === undefined) { ok = false; }
      else if (m[4] !== undefined && String(actual) !== String(m[4])) { ok = false; }
    }
    if (ok) return true;
  }
  return false;
}

/* ------------------------------------------------------- script loading */

/* A script runs in its own scope, but its free variables resolve against the
 * page's global object — like a <script> tag in a browser. The wrapper
 * passes the page's globals in, so the bundle sees window/self/top as the
 * page's global object. */
function __ka_script_scope(code) {
  var names = ['window', 'self', 'top', 'parent', 'document', 'navigator',
    'location', 'screen', 'history', 'performance', 'crypto', 'console',
    'localStorage', 'sessionStorage', 'globalThis'];
  var params = names.map(function (n) { return '__ka_g_' + n; }).join(',');
  var body = names.map(function (n) {
    return 'var ' + n + ' = __ka_g_' + n + ';';
  }).join('');
  /* eslint-disable no-new-func */
  var fn = new Function(params, body + '\n' + code);
  return fn.apply(null, names.map(function (n) {
    if (n === 'window' || n === 'self' || n === 'top' || n === 'parent' || n === 'globalThis') return __ka_probe;
    return globalThis[n];
  }));
}

function __ka_eval_global(code, source, element) {
  var previous = document.currentScript;
  if (element) {
    try { document.currentScript = element; } catch (e) {}
  }
  try {
    __ka_script_scope(code);
    return true;
  } catch (e) {
    __ka_note('script-error', source + ': ' + (e && e.name ? e.name + ': ' + e.message : String(e)) +
      ' | ' + (e && e.stack ? String(e.stack).split('\n').slice(0, 3).join(' <> ') : ''));
    return false;
  } finally {
    try { document.currentScript = previous; } catch (e) {}
  }
}

function __ka_load_script(src, element) {
  var url = __ka_absolute(src, location._href);
  __ka_note('script-load', url);
  var __ka_started = performance.now();
  var headers = 'Accept: */*\r\n';
  var res = __ka_http('GET', url, headers, '');
  __ka_perf_log.push(__ka_perf_entry(url, 'resource', __ka_started));
  if (!res || res.status !== 200) {
    __ka_note('script-failed', url + ' status=' + (res ? res.status : 'none'));
    if (element && typeof element.onerror === 'function') {
      try { element.onerror(new Event('error')); } catch (e) {}
    }
    return;
  }
  var ok = __ka_eval_global(res.body, url, element);
  if (element) {
    if (ok && typeof element.onload === 'function') {
      try { element.onload(new Event('load')); } catch (e) {}
    } else if (!ok && typeof element.onerror === 'function') {
      try { element.onerror(new Event('error')); } catch (e) {}
    }
  }
}

/* An iframe with srcdoc gets that markup as its document. Kahin evaluates the
 * scripts it carries in the page realm: the challenge uses the sandbox frame
 * to run code that reports back into window._cf_chl_opt. */
function __ka_load_srcdoc(html, element) {
  __ka_note('iframe-srcdoc', html.slice(0, 400));
  var re = /<script[^>]*>([\s\S]*?)<\/script>/gi;
  var m, ran = 0;
  while ((m = re.exec(html)) !== null) {
    if (m[1].trim()) { __ka_eval_global(m[1], 'srcdoc-script'); ran++; }
  }
  if (ran === 0 && html.trim()) __ka_eval_global(html, 'srcdoc-body');
  setTimeout(function () {
    try { element.dispatchEvent(new Event('load')); } catch (e) {}
    try { if (typeof element.onload === 'function') element.onload(new Event('load')); } catch (e) {}
  }, 0);
}

/* A sandboxed iframe gets its own realm in a browser. Kahin runs it in the
 * same realm but with its own document/window pair, which is what the
 * challenge actually uses it for: a clean document to write into. */
function __ka_frame_window() {
  var frameDoc = {
    nodeType: 9, readyState: 'complete', URL: 'about:blank', domain: '',
    title: '', referrer: '', cookie: '', characterSet: 'UTF-8', compatMode: 'CSS1Compat',
    write: function (html) {
      var text = String(html);
      __ka_note('frame-write', text.slice(0, 600));
      var re = /<script[^>]*>([\s\S]*?)<\/script>/gi;
      var m, ran = 0;
      while ((m = re.exec(text)) !== null) {
        if (m[1].trim()) { __ka_eval_global(m[1], 'frame-script'); ran++; }
      }
      if (ran === 0 && text.trim()) __ka_eval_global(text, 'frame-body');
    },
    writeln: function (html) { this.write(html); },
    open: noop, close: noop,
    createElement: function (tag) { return document.createElement(tag); },
    createTextNode: function (t) { return document.createTextNode(t); },
    getElementById: function () { return null; },
    getElementsByTagName: function () { return []; },
    getElementsByClassName: function () { return []; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    addEventListener: noop, removeEventListener: noop, dispatchEvent: noop,
    body: new Element('body'), head: new Element('head'), documentElement: new Element('html'),
    forms: [], scripts: [], images: [], links: [],
    createEvent: function (t) { var ev = new Event(t || 'Event'); ev.initEvent = function (n) { this.type = n; }; return ev; },
    implementation: { createHTMLDocument: function () { return frameDoc; }, hasFeature: function () { return true; } },
    location: {
      href: 'about:blank', protocol: 'about:', host: '', hostname: '',
      pathname: 'blank', search: '', hash: '', origin: 'null',
      toString: function () { return 'about:blank'; }
    }
  };
  var win = {
    document: frameDoc,
    location: frameDoc.location,
    navigator: navigator,
    screen: screen,
    history: { length: 1, pushState: noop, replaceState: noop, state: null },
    parent: null, top: null, frames: [], length: 0,
    name: '', closed: false, isSecureContext: true, origin: 'null',
    addEventListener: function (t, f) {
      (__ka_listeners['frame:' + t] = __ka_listeners['frame:' + t] || []).push(f);
    },
    removeEventListener: noop, dispatchEvent: noop,
    postMessage: function (data) { __ka_note('frame-postmessage', String(data).slice(0, 200)); },
    setTimeout: setTimeout, clearTimeout: clearTimeout,
    setInterval: setInterval, clearInterval: clearInterval,
    queueMicrotask: queueMicrotask,
    atob: atob, btoa: btoa, crypto: crypto, console: console, performance: performance,
    fetch: fetch, XMLHttpRequest: XMLHttpRequest,
    Event: Event, CustomEvent: CustomEvent, MessageEvent: Event, ErrorEvent: Event
  };
  win.self = win;
  win.parent = win;
  win.top = win;
  frameDoc.defaultView = win;
  return win;
}

document.createElementNS = function (ns, tag) { return document.createElement(tag); };

document.createElement = function (tag) {
  var t = String(tag || 'div').toLowerCase();
  var node = new Element(t);
  node.ownerDocument = document;
  __ka_all.push(node);
  if (t === 'script') {
    var srcValue = '';
    Object.defineProperty(node, 'src', {
      configurable: true,
      get: function () { return srcValue; },
      set: function (v) {
        srcValue = String(v);
        /* A script may get its src after it was inserted; the load has to
         * start then, not only on append. */
        if (srcValue && node.parentNode) __ka_load_script(srcValue, node);
      }
    });
    node.__ka_on_append = function () {
      if (srcValue) {
        __ka_load_script(srcValue, node);
      } else if (node.textContent) {
        __ka_eval_global(node.textContent, 'inline', node);
      }
    };
  } else if (t === 'iframe') {
    var iframeSrc = '';
    Object.defineProperty(node, 'src', {
      configurable: true,
      get: function () { return iframeSrc; },
      set: function (v) {
        iframeSrc = String(v);
      }
    });
    var frameWin = null;
    Object.defineProperty(node, 'contentWindow', {
      configurable: true,
      get: function () {
        __ka_note('iframe-contentwindow', iframeSrc);
        if (!frameWin) frameWin = __ka_frame_window();
        return frameWin;
      }
    });
    Object.defineProperty(node, 'contentDocument', {
      configurable: true,
      get: function () {
        __ka_note('iframe-contentdocument', iframeSrc);
        if (!frameWin) frameWin = __ka_frame_window();
        return frameWin.document;
      }
    });
    node.__ka_on_append = function () {
      __ka_note('iframe', iframeSrc);
      try { node.dispatchEvent(new Event('load')); } catch (e) { __ka_note('iframe-load-error', String(e)); }
      try { if (typeof node.onload === 'function') node.onload(new Event('load')); } catch (e) { __ka_note('iframe-onload-error', (e && e.stack) ? e.stack : String(e)); }
      setTimeout(function () {
        try { node.dispatchEvent(new Event('load')); } catch (e) {}
        try { if (typeof node.onload === 'function') node.onload(new Event('load')); } catch (e) { __ka_note('iframe-onload-error', (e && e.stack) ? e.stack : String(e)); }
      }, 0);
    };
  }
  /* Writes are logged: the challenge configures its sandbox iframe through
   * properties (srcdoc/src/style), not only attributes. */
  return new Proxy(node, {
    get: function (target, key) {
      if (key in target) return target[key];
      /* HTML element properties mirror their attributes: meta.content,
       * script.nonce, a.href ... */
      if (target.attributes && Object.prototype.hasOwnProperty.call(target.attributes, key)) {
        return target.attributes[key];
      }
      var dashed = String(key).replace(/[A-Z]/g, function (c) { return '-' + c.toLowerCase(); });
      if (target.attributes && Object.prototype.hasOwnProperty.call(target.attributes, dashed)) {
        return target.attributes[dashed];
      }
      __ka_note('missing-element-property', String(key));
      return undefined;
    },
    set: function (target, key, value) {
      var name = String(key);
      if (name !== 'src' && name !== 'textContent' && name !== 'innerHTML' &&
          name !== 'style' && name !== 'nodeValue' && name !== 'parentNode' &&
          name !== 'childNodes' && name !== 'children' && name !== 'ownerDocument' &&
          name !== 'attributes' && name !== '__ka_on_append' && name !== 'nodeType' &&
          name !== 'tagName' && name !== 'nodeName' && name !== 'id' && name !== 'className') {
        __ka_note('el-set', target.tagName + '.' + name + '=' + String(value).slice(0, 300));
      }
      /* Reflect the write onto the element itself, so the property setters
       * (script.src, iframe.src) run. */
      Reflect.set(target, key, value);
      if (name === 'srcdoc') __ka_load_srcdoc(String(value), target);
      if (name === 'id' && value) __ka_elements[String(value)] = target;
      return true;
    }
  });
};

/* ------------------------------------------------------------------ XHR */

function XMLHttpRequest() {
  EventTarget.call(this);
  this.readyState = 0;
  this.status = 0;
  this.statusText = '';
  this.responseText = '';
  this.response = '';
  this.responseType = '';
  this.responseURL = '';
  this.timeout = 0;
  this.withCredentials = false;
  this.onreadystatechange = null;
  this.onload = null;
  this.onerror = null;
  this.onloadend = null;
  this.onabort = null;
  this.ontimeout = null;
  this.onprogress = null;
  this._method = 'GET';
  this._url = '';
  this._headers = [];
}
XMLHttpRequest.prototype = Object.create(EventTarget.prototype);
XMLHttpRequest.prototype.open = function (method, url, async) {
  this._method = String(method || 'GET').toUpperCase();
  this._url = String(url);
  this.readyState = 1;
  this._fire('readystatechange');
};
XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
  this._headers.push(String(k) + ': ' + String(v));
};
XMLHttpRequest.prototype.getResponseHeader = function (k) {
  var key = String(k).toLowerCase();
  var headers = this._responseHeaders || {};
  for (var name in headers) {
    if (Object.prototype.hasOwnProperty.call(headers, name) && name.toLowerCase() === key) {
      return headers[name];
    }
  }
  return null;
};
XMLHttpRequest.prototype.getAllResponseHeaders = function () {
  return this._responseHeaderText || '';
};
XMLHttpRequest.prototype.abort = noop;
XMLHttpRequest.prototype.overrideMimeType = noop;
XMLHttpRequest.prototype._fire = function (type) {
  var handler = this['on' + type];
  if (typeof handler === 'function') {
    try { handler.call(this, new ProgressEvent(type)); } catch (e) {
      __ka_note('xhr-handler-error', String(e));
    }
  }
  this.dispatchEvent(new Event(type));
};
XMLHttpRequest.prototype.send = function (body) {
  var url = __ka_absolute(this._url, location._href);
  var headers = this._headers.length ? this._headers.join('\r\n') + '\r\n' : '';
  if (body !== undefined && body !== null && !headers) {
    headers = 'Content-Type: application/x-www-form-urlencoded\r\n';
  }
  __ka_note('xhr', this._method + ' ' + url);
  var res = __ka_http(this._method, url, headers, body === undefined || body === null ? '' : String(body));
  if (!res) {
    this.readyState = 4;
    this.status = 0;
    this._fire('readystatechange');
    this._fire('error');
    this._fire('loadend');
    return;
  }
  this.status = res.status;
  this.statusText = res.status === 200 ? 'OK' : '';
  this.responseURL = res.url || url;
  this._responseHeaders = res.headers || {};
  this._responseHeaderText = res.headerText || '';
  var text = res.body == null ? '' : String(res.body);
  this.responseText = text;
  this.response = this.responseType === 'json' ? __ka_safe_json(text) : text;
  var setCookie = __ka_header(res, 'set-cookie');
  if (setCookie) __ka_apply_set_cookie(setCookie);
  this.readyState = 4;
  this._fire('readystatechange');
  if (this.status >= 200 && this.status < 400) this._fire('load');
  else this._fire('error');
  this._fire('loadend');
};

function __ka_header(res, name) {
  var wanted = String(name).toLowerCase();
  var headers = (res && res.headers) || {};
  for (var key in headers) {
    if (Object.prototype.hasOwnProperty.call(headers, key) && key.toLowerCase() === wanted) {
      return headers[key];
    }
  }
  return null;
}

function __ka_safe_json(text) {
  try { return JSON.parse(text); } catch (e) { return null; }
}

/* ---------------------------------------------------------------- fetch */

function Headers(init) {
  this._map = {};
  if (init) {
    if (typeof init === 'string') {
      init.split('\n').forEach(function (line) {
        var idx = line.indexOf(':');
        if (idx > 0) this._map[line.slice(0, idx).trim().toLowerCase()] = line.slice(idx + 1).trim();
      }, this);
    } else if (Array.isArray(init)) {
      init.forEach(function (pair) { this._map[String(pair[0]).toLowerCase()] = String(pair[1]); }, this);
    } else {
      for (var k in init) {
        if (Object.prototype.hasOwnProperty.call(init, k)) this._map[k.toLowerCase()] = String(init[k]);
      }
    }
  }
}
Headers.prototype.get = function (k) {
  var key = String(k).toLowerCase();
  return Object.prototype.hasOwnProperty.call(this._map, key) ? this._map[key] : null;
};
Headers.prototype.has = function (k) { return this.get(k) !== null; };
Headers.prototype.set = function (k, v) { this._map[String(k).toLowerCase()] = String(v); };
Headers.prototype.append = function (k, v) { this._map[String(k).toLowerCase()] = String(v); };
Headers.prototype.delete = function (k) { delete this._map[String(k).toLowerCase()]; };
Headers.prototype.forEach = function (fn) {
  for (var k in this._map) {
    if (Object.prototype.hasOwnProperty.call(this._map, k)) fn(this._map[k], k, this);
  }
};
Headers.prototype.keys = function () { return Object.keys(this._map); };

function Response(body, init) {
  init = init || {};
  this.status = init.status === undefined ? 200 : init.status;
  this.statusText = init.statusText || '';
  this.ok = this.status >= 200 && this.status < 300;
  this.headers = init.headers instanceof Headers ? init.headers : new Headers(init.headers || {});
  this._body = body === undefined || body === null ? '' : String(body);
  this.type = 'basic';
  this.url = init.url || '';
  this.redirected = false;
  this.bodyUsed = false;
}
Response.prototype.text = function () { this.bodyUsed = true; return Promise.resolve(this._body); };
Response.prototype.json = function () { this.bodyUsed = true; return Promise.resolve(__ka_safe_json(this._body)); };
Response.prototype.arrayBuffer = function () {
  this.bodyUsed = true;
  var bytes = new Uint8Array(this._body.length);
  for (var i = 0; i < this._body.length; i++) bytes[i] = this._body.charCodeAt(i) & 0xff;
  return Promise.resolve(bytes.buffer);
};
Response.prototype.blob = function () { return Promise.resolve(new Blob([this._body])); };
Response.prototype.clone = function () { return new Response(this._body, this); };

function Request(url, init) {
  init = init || {};
  this.url = String(url);
  this.method = String(init.method || 'GET').toUpperCase();
  this.headers = init.headers instanceof Headers ? init.headers : new Headers(init.headers || {});
  this.body = init.body || null;
  this.mode = 'cors';
  this.credentials = init.credentials || 'same-origin';
  this.cache = 'default';
  this.redirect = 'follow';
  this.referrer = '';
}

function fetch(input, init) {
  init = init || {};
  var url = typeof input === 'string' ? input : (input && input.url) || '';
  var method = init.method || (input && input.method) || 'GET';
  var headers = '';
  var src = init.headers || (input && input.headers);
  if (src instanceof Headers) {
    src.forEach(function (v, k) { headers += k + ': ' + v + '\r\n'; });
  } else if (typeof src === 'string') {
    headers += src + '\r\n';
  } else if (src) {
    for (var k in src) {
      if (Object.prototype.hasOwnProperty.call(src, k)) headers += k + ': ' + src[k] + '\r\n';
    }
  }
  var body = init.body !== undefined
    ? init.body
    : (input && input.body !== undefined && input.body !== null ? input.body : '');
  var absolute = __ka_absolute(url, location._href);
  __ka_note('fetch', String(method) + ' ' + absolute);
  var res = __ka_http(String(method), absolute, headers, body === null || body === undefined ? '' : String(body));
  if (!res) return Promise.reject(new TypeError('network request failed'));
  var setCookie = __ka_header(res, 'set-cookie');
  if (setCookie) __ka_apply_set_cookie(setCookie);
  var response = new Response(res.body, {
    status: res.status, headers: res.headers, url: absolute
  });
  return Promise.resolve(response);
}

/* ------------------------------------------------------------ FormData */

function FormData() { this._entries = []; }
FormData.prototype.append = function (k, v) { this._entries.push([String(k), String(v)]); };
FormData.prototype.set = function (k, v) {
  var key = String(k);
  this._entries = this._entries.filter(function (e) { return e[0] !== key; });
  this._entries.push([key, String(v)]);
};
FormData.prototype.get = function (k) {
  var key = String(k);
  for (var i = 0; i < this._entries.length; i++) if (this._entries[i][0] === key) return this._entries[i][1];
  return null;
};
FormData.prototype.getAll = function (k) {
  var key = String(k), out = [];
  for (var i = 0; i < this._entries.length; i++) if (this._entries[i][0] === key) out.push(this._entries[i][1]);
  return out;
};
FormData.prototype.has = function (k) { return this.get(k) !== null; };
FormData.prototype.delete = function (k) {
  var key = String(k);
  this._entries = this._entries.filter(function (e) { return e[0] !== key; });
};
FormData.prototype.forEach = function (fn) {
  this._entries.forEach(function (e) { fn(e[1], e[0], this); }, this);
};
FormData.prototype.entries = function () {
  var out = [];
  this._entries.forEach(function (e) { out.push([e[0], e[1]]); });
  return out;
};

/* --------------------------------------------------------------- others */

function Blob(parts, options) {
  var text = '';
  if (parts) {
    if (typeof parts === 'string') text = parts;
    else if (Array.isArray(parts)) {
      for (var i = 0; i < parts.length; i++) text += String(parts[i]);
    }
  }
  this._text = text;
  this.size = text.length;
  this.type = (options && options.type) || '';
}
Blob.prototype.text = function () { return Promise.resolve(this._text); };
Blob.prototype.slice = function () { return new Blob([this._text]); };

/* URL.createObjectURL/revokeObjectURL are part of the browser support check:
 * the challenge builds a Blob, turns it into a URL and loads it in a Worker. */
var __ka_blob_urls = {};
var __ka_blob_seq = 0;
URL.createObjectURL = function (blob) {
  var id = 'blob:kahin/' + (++__ka_blob_seq);
  __ka_blob_urls[id] = blob;
  return id;
};
URL.revokeObjectURL = function (id) { delete __ka_blob_urls[String(id)]; };
function File() {}
function FileReader() { this.readyState = 0; }
FileReader.prototype.readAsText = function () {
  this.readyState = 2; this.result = '';
  if (this.onload) this.onload(new Event('load'));
};
FileReader.prototype.readAsDataURL = function () {
  this.readyState = 2; this.result = 'data:,';
  if (this.onload) this.onload(new Event('load'));
};

function MutationObserver() { this.observe = noop; this.disconnect = noop; this.takeRecords = function () { return []; }; }
function IntersectionObserver() { this.observe = noop; this.disconnect = noop; }
function ResizeObserver() { this.observe = noop; this.disconnect = noop; }
function PerformanceObserver(callback) {
  this._callback = callback;
  this._types = [];
  this.observe = function (options) {
    var wanted = (options && options.entryTypes) || (options && options.type ? [options.type] : []);
    for (var i = 0; i < wanted.length; i++) this._types.push(String(wanted[i]));
  };
  this.disconnect = function () { this._types = []; };
  this.takeRecords = function () { return []; };
}

function matchMedia(query) {
  return {
    matches: false, media: String(query), onchange: null,
    addListener: noop, removeListener: noop,
    addEventListener: noop, removeEventListener: noop, dispatchEvent: noop
  };
}
function getComputedStyle() {
  return { getPropertyValue: function () { return ''; }, length: 0, fontFamily: 'system-ui', fontSize: '16px' };
}

function Image() { Element.call(this, 'img'); }
Image.prototype = Object.create(Element.prototype);
function Audio() { Element.call(this, 'audio'); }
Audio.prototype = Object.create(Element.prototype);

/* A Worker is a real capability check for the challenge: it creates one
 * from a Blob URL and looks for ReadableStream.prototype.pipeTo. */
/* ReadableStream/WritableStream: the challenge checks
 * ReadableStream.prototype.pipeTo as a browser-support signal. */
function ReadableStream(source) {
  this._source = source || {};
  this.locked = false;
}
ReadableStream.prototype.getReader = function () {
  var self = this;
  var queue = [];
  return {
    read: function () { return Promise.resolve(queue.shift() || { value: undefined, done: true }); },
    releaseLock: function () { self.locked = false; },
    cancel: function () { return Promise.resolve(); },
    closed: Promise.resolve()
  };
};
ReadableStream.prototype.pipeTo = function (dest) {
  try {
    if (dest && typeof dest.write === 'function') dest.write(new Uint8Array(0));
    if (dest && typeof dest.close === 'function') dest.close();
  } catch (e) {}
  return Promise.resolve();
};
ReadableStream.prototype.cancel = function () { return Promise.resolve(); };
ReadableStream.prototype.tee = function () { return [this, this]; };
ReadableStream.prototype[Symbol.asyncIterator] = function () {
  var self = this;
  return { next: function () { return Promise.resolve({ value: undefined, done: true }); } };
};

function WritableStream(sink) {
  this._sink = sink || {};
  this.locked = false;
}
WritableStream.prototype.getWriter = function () {
  var self = this;
  return {
    write: function () { return Promise.resolve(); },
    close: function () { return Promise.resolve(); },
    abort: function () { return Promise.resolve(); },
    releaseLock: function () { self.locked = false; },
    ready: Promise.resolve(),
    closed: Promise.resolve(),
    desiredSize: 1
  };
};
WritableStream.prototype.abort = function () { return Promise.resolve(); };
WritableStream.prototype.close = function () { return Promise.resolve(); };

function TransformStream() {
  this.readable = new ReadableStream();
  this.writable = new WritableStream();
}

function Worker(url) {
  this.url = String(url || '');
  this.postMessage = noop; this.addEventListener = noop;
  this.removeEventListener = noop; this.terminate = noop;
}
Worker.prototype.postMessage = noop;
Worker.prototype.terminate = noop;
Worker.prototype.addEventListener = noop;
Worker.prototype.removeEventListener = noop;
Worker.prototype.pipeTo = function (dest) {
  try {
    if (dest && typeof dest.getWriter === 'function') {
      var writer = dest.getWriter();
      if (writer && typeof writer.close === 'function') writer.close();
    }
  } catch (e) {}
  return Promise.resolve();
};
function SharedWorker() {
  this.postMessage = noop; this.addEventListener = noop; this.terminate = noop;
}
function WebSocket() {
  this.readyState = 0;
  this.send = noop; this.close = noop; this.addEventListener = noop;
}
function RTCPeerConnection() {
  this.createDataChannel = function () { return { send: noop, close: noop }; };
  this.close = noop;
}
function AudioContext() {
  this.sampleRate = 44100; this.state = 'running'; this.destination = {};
  this.createOscillator = function () { return { start: noop, stop: noop, connect: noop }; };
}
function OfflineAudioContext() { AudioContext.call(this); }
function Notification() {}
function BroadcastChannel() { this.postMessage = noop; this.addEventListener = noop; this.close = noop; }

var indexedDB = {
  open: function () { return Promise.reject(new Error('indexedDB unavailable')); },
  deleteDatabase: function () { return Promise.reject(new Error('indexedDB unavailable')); }
};

var WebAssembly = {
  Module: function () {},
  Instance: function () {},
  Memory: function () {},
  compile: function () { return Promise.reject(new Error('WebAssembly unavailable')); },
  instantiate: function () { return Promise.reject(new Error('WebAssembly unavailable')); },
  validate: function () { return false; }
}
/* A missing function is the most common failure in a DOM shim: every
 * property the bundle touches is recorded, so a gap names itself. */
function __ka_missing(name) {
  __ka_note('missing', name);
  if (typeof globalThis.__ka_missing_names === 'undefined') globalThis.__ka_missing_names = [];
  globalThis.__ka_missing_names.push(name);
  return undefined;
}


function structuredClone(v) { return v; }

/* --------------------------------------------------------- window setup */

var CSS = {
  supports: function (property, value) {
    if (value === undefined) {
      var text = String(property || '');
      return /^(display|position|transform|flex|grid|animation|transition|opacity|clip-path|backdrop-filter|aspect-ratio|container-type|content-visibility|color-mix|oklch|subgrid|text-wrap|view-timeline|scroll-timeline|anchor-name|position-anchor|field-sizing|interpolate-size|text-box-trim|white-space-collapse)/.test(text);
    }
    return true;
  },
  escape: function (value) { return String(value); },
  registerProperty: function () { return {}; },
  unit: function (value) { return String(value); }
};

var window = __ka_probe;
/* window, self, top and parent are the SAME global object in a browser.
 * The challenge compares them by identity, so they must be one object. */
var self = __ka_probe;
var top = __ka_probe;
var parent = __ka_probe;
window.window = window;
window.self = self;
window.top = top;
window.parent = parent;
window.document = document;
/* window.readyState mirrors document.readyState in a browser. */
Object.defineProperty(window, 'readyState', {
  configurable: true,
  get: function () { return document.readyState; }
});
window.navigator = navigator;
window.location = location;
window.screen = screen;
window.history = history;
window.performance = performance;
window.crypto = crypto;
window.localStorage = localStorage;
window.sessionStorage = sessionStorage;
window.console = console;
window.addEventListener = addEventListener;
window.removeEventListener = removeEventListener;
window.dispatchEvent = dispatchEvent;
window.innerWidth = innerWidth;
window.innerHeight = innerHeight;
window.outerWidth = outerWidth;
window.outerHeight = outerHeight;
window.devicePixelRatio = devicePixelRatio;
window.getComputedStyle = getComputedStyle;
window.matchMedia = matchMedia;
window.requestAnimationFrame = requestAnimationFrame;
window.cancelAnimationFrame = cancelAnimationFrame;
window.setTimeout = setTimeout;
window.__ka_timers_ref = __ka_timers;
window.setInterval = setInterval;
window.clearTimeout = clearTimeout;
window.clearInterval = clearInterval;
window.queueMicrotask = queueMicrotask;
window.fetch = fetch;
window.XMLHttpRequest = XMLHttpRequest;
window.FormData = FormData;
window.Headers = Headers;
window.Request = Request;
window.Response = Response;
window.Blob = Blob;
window.File = File;
window.FileReader = FileReader;
window.URL = URL;
window.URLSearchParams = URLSearchParams;
window.TextEncoder = TextEncoder;
window.TextDecoder = TextDecoder;
window.atob = atob;
window.btoa = btoa;
window.Event = Event;
window.CustomEvent = CustomEvent;
window.UIEvent = UIEvent;
window.MouseEvent = MouseEvent;
window.ProgressEvent = ProgressEvent;
window.EventTarget = EventTarget;
window.Element = Element;
window.Node = Node;
window.HTMLElement = HTMLElement;
window.MutationObserver = MutationObserver;
window.IntersectionObserver = IntersectionObserver;
window.ResizeObserver = ResizeObserver;
window.PerformanceObserver = PerformanceObserver;
window.Image = Image;
window.Audio = Audio;
window.Worker = Worker;
window.SharedWorker = SharedWorker;
window.WebSocket = WebSocket;
window.RTCPeerConnection = RTCPeerConnection;
window.AudioContext = AudioContext;
window.OfflineAudioContext = OfflineAudioContext;
window.Notification = Notification;
window.BroadcastChannel = BroadcastChannel;
window.indexedDB = indexedDB;
window.WebAssembly = WebAssembly;
window.ReadableStream = ReadableStream;
window.WritableStream = WritableStream;
window.TransformStream = TransformStream;
window.structuredClone = structuredClone;
window.CSS = CSS;
window.isSecureContext = true;
window.origin = __ka_parsed.origin;
window.frameElement = null;
window.closed = false;
window.name = '';
window.onmessage = null;
window.onload = null;
window.onerror = null;
window.__ka_events = __ka_events;
window.__ka_cookies = __ka_cookies;
/* The Turnstile bundle checks these DOM constructors by identity. */
window.HTMLScriptElement = Element;
window.HTMLIFrameElement = Element;
window.HTMLDivElement = Element;
window.HTMLInputElement = Element;
window.HTMLCanvasElement = Element;
window.HTMLFormElement = Element;
window.HTMLAnchorElement = Element;
window.HTMLParagraphElement = Element;
window.HTMLSpanElement = Element;
window.HTMLBodyElement = Element;
window.HTMLHeadElement = Element;
window.HTMLHtmlElement = Element;
window.HTMLStyleElement = Element;
window.HTMLLinkElement = Element;
window.HTMLMetaElement = Element;
window.HTMLTemplateElement = Element;
window.HTMLButtonElement = Element;
window.HTMLLabelElement = Element;
window.HTMLElement = HTMLElement;
window.SVGElement = Element;
window.SVGGraphicsElement = Element;
window.SVGSVGElement = Element;
window.DocumentFragment = Element;
window.Text = Node;
window.Comment = Node;
window.DOMParser = function () {
  this.parseFromString = function () { return document; };
};

document.defaultView = window;
/* The page's readyState drives when the bundles start:
 *   'loading'    -> the bundle waits for DOMContentLoaded
 *   'interactive'-> the bundle waits for the load event
 *   'active'     -> the bundle starts immediately
 *   'complete'   -> the page is fully loaded
 * The challenge bundle runs while the page is still parsing, so the state
 * starts at 'loading'; the host advances it as the events fire. */
document.readyState = 'loading';

window.chrome = {
  runtime: { connect: noop, sendMessage: noop },
  loadTimes: function () { return {}; },
  csi: function () { return {}; },
  app: { isInstalled: false }
};
window.getSelection = function () {
  return {
    rangeCount: 0, isCollapsed: true, toString: function () { return ''; },
    removeAllRanges: noop, addRange: noop, collapse: noop,
    getRangeAt: function () { return document.createRange(); }
  };
};
window.requestIdleCallback = function (fn) {
  return setTimeout(function () {
    fn({ didTimeout: false, timeRemaining: function () { return 0; } });
  }, 0);
};
window.cancelIdleCallback = function () {};
window.visualViewport = {
  width: innerWidth, height: innerHeight, scale: 1, offsetLeft: 0, offsetTop: 0,
  addEventListener: noop, removeEventListener: noop
};
window.customElements = {
  define: noop, get: function () { return undefined; },
  whenDefined: function () { return Promise.resolve(); }
};
window.serviceWorker = {
  register: function () { return Promise.resolve(); },
  addEventListener: noop, controller: null
};
navigator.serviceWorker = window.serviceWorker;
window.SpeechSynthesisUtterance = function () {};
window.speechSynthesis = { speak: noop, cancel: noop, getVoices: function () { return []; } };
navigator.clipboard = {
  writeText: function () { return Promise.resolve(); },
  readText: function () { return Promise.resolve(''); }
};
document.getSelection = window.getSelection;

/* -------------------------------------------------- HTML page -> DOM tree */

function __ka_parse_html(html) {
  var voidTags = {
    area: 1, base: 1, br: 1, col: 1, embed: 1, hr: 1, img: 1,
    input: 1, link: 1, meta: 1, param: 1, source: 1, track: 1, wbr: 1
  };
  var stack = [document.body];
  var re = /<!--[\s\S]*?-->|<\/?([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^'">])*)\/?>|([^<]+)/g;
  var m, skip = null;
  while ((m = re.exec(String(html))) !== null) {
    if (m[0].indexOf('<!--') === 0) continue;
    if (m[3] !== undefined) {
      if (skip) continue;
      if (m[3].trim()) {
        var text = document.createTextNode(m[3]);
        stack[stack.length - 1].appendChild(text);
      }
      continue;
    }
    var tag = m[1].toLowerCase();
    var closing = m[0].charAt(1) === '/';
    if (skip) {
      if (closing && tag === skip) skip = null;
      continue;
    }
    if (closing) {
      for (var i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName === tag.toUpperCase()) { stack.length = i; break; }
      }
      continue;
    }
    if (tag === 'html') continue;
    if (tag === 'head' || tag === 'body') { stack.push(document[tag]); continue; }
    if (tag === 'script' || tag === 'style') skip = tag;
    var el = document.createElement(tag);
    var attrs = m[2] || '';
    var attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))/g;
    var a;
    while ((a = attrRe.exec(attrs)) !== null) {
      var value = a[3] !== undefined ? a[3] : (a[4] !== undefined ? a[4] : (a[5] || ''));
      el.setAttribute(a[1], value);
    }
    stack[stack.length - 1].appendChild(el);
    if (tag === 'title') {
      stack.push(el);
      continue;
    }
    if (!voidTags[tag] && attrs.charAt(attrs.length - 1) !== '/') stack.push(el);
  }
}

/* The challenged page itself: the bundle reads its own markup before it
 * starts, so it is parsed into the DOM before the challenge runs. */
if (typeof __KA_HTML_FILE === 'string' && __KA_HTML_FILE) {
  try {
    __ka_parse_html(__ka_load(__KA_HTML_FILE));
    var __ka_title = document.querySelector('title');
    if (__ka_title) document.title = String(__ka_title.textContent || '');
  } catch (e) {
    __ka_note('html-parse-error', String(e));
  }
}
