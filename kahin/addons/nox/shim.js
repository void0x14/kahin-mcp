// Minimal DOM/window shim for Baidu ADAS nox JS (Banti JSVMP).
//
// The nox script does not need a real browser. It needs a `window` object with
// `resetNoxJstV1()` exposed and a `document.cookie` setter it can write to.
// Everything below is the smallest surface that satisfies it.
//
// Kahin-owned knobs, read by the host before script load:
//   __KA_USER_AGENT  - navigator.userAgent; pinned to the live engine UA so a
//                      cookie stays consistent with the browser identity.
//   __KA_LOCATION    - absolute origin the cookie is minted for.
var __cookies = {};
var __timers = [];
var __listeners = {};

function Window() {}
var noop = function () {};

function el(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(), nodeName: (tag || 'div').toUpperCase(), nodeType: 1,
    style: {}, setAttribute: noop, removeAttribute: noop, getAttribute: function () { return null; },
    appendChild: function (c) { return c; }, removeChild: function (c) { return c; },
    insertBefore: function (c) { return c; }, replaceChild: function (c, d) { return d; },
    cloneNode: function () { return el(tag); },
    getElementsByTagName: function () { return []; }, getElementsByClassName: function () { return []; },
    querySelector: function () { return null; }, querySelectorAll: function () { return []; },
    hasChildNodes: function () { return false; },
    addEventListener: noop, removeEventListener: noop, dispatchEvent: noop,
    ownerDocument: null, parentNode: null, childNodes: [], children: [],
    innerHTML: '', textContent: '', value: '',
    getBoundingClientRect: function () { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
    getContext: function () { return null; }, toDataURL: function () { return 'data:,'; },
  };
}

var document = {
  nodeType: 9, readyState: 'complete', referrer: '', title: '', domain: 'gitee.com',
  URL: 'https://gitee.com/', characterSet: 'UTF-8',
  createElement: el,
  createTextNode: function (t) { return { nodeType: 3, textContent: t }; },
  createDocumentFragment: function () { return el('fragment'); },
  getElementById: function () { return null; },
  getElementsByTagName: function () { return []; }, getElementsByClassName: function () { return []; },
  querySelector: function () { return null; }, querySelectorAll: function () { return []; },
  addEventListener: function (t, f) { (__listeners[t] = __listeners[t] || []).push(f); },
  removeEventListener: noop, dispatchEvent: noop,
  body: null, head: null, location: null, defaultView: null,
  createEvent: function () { return { initEvent: noop }; },
  implementation: { createHTMLDocument: function () { return document; } },
};
document.documentElement = el('html');
document.body = el('body');
document.head = el('head');

Object.defineProperty(document, 'cookie', {
  configurable: true,
  get: function () { return this.__raw || ''; },
  set: function (v) {
    this.__raw = String(v).replace(/^;\s*/, '');
    var first = this.__raw.split(';')[0];
    var i = first.indexOf('=');
    if (i > 0) __cookies[first.slice(0, i).trim()] = first.slice(i + 1).trim();
  },
});

var navigator = {
  userAgent: typeof __KA_USER_AGENT === 'string' ? __KA_USER_AGENT
    : 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
  appVersion: '5.0 (X11)', appName: 'Netscape', appCodeName: 'Mozilla',
  platform: 'Linux x86_64', vendor: '', language: 'en-US', languages: ['en-US'],
  cookieEnabled: true, doNotTrack: null, hardwareConcurrency: 8, deviceMemory: 8,
  maxTouchPoints: 0, webdriver: false, plugins: { length: 0 }, mimeTypes: { length: 0 },
};

var screen = {
  width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
  colorDepth: 24, pixelDepth: 24,
};

var __KA_URL = typeof __KA_LOCATION === 'string' ? __KA_LOCATION : 'https://gitee.com/';
var __KA_PARSED = (function () {
  var m = /^([a-z]+):\/\/([^/]+)(\/[^?#]*)?/.exec(__KA_URL);
  return {
    protocol: m ? m[1] + ':' : 'https:',
    host: m ? m[2] : 'gitee.com',
    origin: m ? m[1] + '://' + m[2] : 'https://gitee.com',
    pathname: (m && m[3]) || '/',
    search: __KA_URL.indexOf('?') >= 0 ? __KA_URL.slice(__KA_URL.indexOf('?')) : '',
  };
})();

var location = {
  href: __KA_URL, protocol: __KA_PARSED.protocol, host: __KA_PARSED.host,
  hostname: __KA_PARSED.host.split(':')[0], origin: __KA_PARSED.origin,
  pathname: __KA_PARSED.pathname, search: __KA_PARSED.search,
  hash: '', port: '', reload: noop, replace: noop,
};
document.location = location;

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

// The nox script drives its work through timers; the host drains this queue.
function setTimeout(f) { __timers.push(f); return __timers.length; }
function setInterval(f) { __timers.push(f); return __timers.length; }
function clearTimeout() {}
function clearInterval() {}
function requestAnimationFrame(f) { __timers.push(function () { f(0); }); return __timers.length; }
function cancelAnimationFrame() {}

var console = { log: noop, warn: noop, error: noop, info: noop, debug: noop };

var performance = {
  now: function () { return 0; }, timing: {}, mark: noop, measure: noop,
  getEntriesByType: function () { return []; },
};

function Store() {}
Store.prototype.getItem = function () { return null; };
Store.prototype.setItem = noop;
Store.prototype.removeItem = noop;
Store.prototype.clear = noop;
var localStorage = new Store();
var sessionStorage = new Store();

function addEventListener(t, f) { (__listeners[t] = __listeners[t] || []).push(f); }
function removeEventListener() {}
function dispatchEvent() {}

var history = { length: 1, pushState: noop, replaceState: noop };

function XMLHttpRequest() {
  this.open = noop; this.send = noop; this.setRequestHeader = noop;
  this.addEventListener = noop; this.readyState = 0; this.status = 0;
}
function Image() { return el('img'); }
function Worker() { this.postMessage = noop; this.addEventListener = noop; this.terminate = noop; }
function Blob() {}
var URL = { createObjectURL: function () { return 'blob:x'; }, revokeObjectURL: noop };
function matchMedia() {
  return { matches: false, addListener: noop, removeListener: noop, addEventListener: noop };
}
function getComputedStyle() { return { getPropertyValue: function () { return ''; } }; }

var innerWidth = 1920, innerHeight = 947, outerWidth = 1920, outerHeight = 1040;
var devicePixelRatio = 1;

var window = globalThis, self = globalThis, top = globalThis, parent = globalThis;
window.Window = Window;
window.document = document;
window.navigator = navigator;
window.location = location;
document.defaultView = window;