// Opens the sealed payload (scripts/perf_eval/seal.py) with the dashboard
// login. Every parameter must match seal.py. No DOM: tests/js runs it in Node.
// A plain script: the page reads window.PerfUnlock, Node requires it.
(function (root) {
'use strict';

const subtle = root.crypto.subtle;

function fromB64(s) {
  return Uint8Array.from(atob(s), c => c.charCodeAt(0));
}
function toB64(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)));
}

function checkEnvelope(envelope) {
  if (!envelope || envelope.v !== 1 || envelope.kdf !== 'PBKDF2-SHA256') {
    throw new Error('unknown data format');
  }
}

// The AES key for this login. Extractable, so the page can keep it for the
// session instead of the password.
async function deriveKey(user, password, envelope) {
  checkEnvelope(envelope);
  const secret = await subtle.importKey('raw', new TextEncoder().encode(user + '\n' + password),
    'PBKDF2', false, ['deriveKey']);
  return subtle.deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt: fromB64(envelope.salt), iterations: envelope.iter },
    secret, { name: 'AES-GCM', length: 256 }, true, ['decrypt']);
}

// The payload, or a rejection if the key is wrong or the file damaged.
async function openWith(envelope, key) {
  checkEnvelope(envelope);
  const gz = await subtle.decrypt({ name: 'AES-GCM', iv: fromB64(envelope.iv) }, key, fromB64(envelope.data));
  const text = await new Response(new Blob([gz]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  return JSON.parse(text);
}

async function exportKey(key) {
  return toB64(await subtle.exportKey('raw', key));
}
function importKey(b64) {
  return subtle.importKey('raw', fromB64(b64), 'AES-GCM', true, ['decrypt']);
}

const api = { deriveKey, openWith, exportKey, importKey };
if (typeof module !== 'undefined' && module.exports) module.exports = api;
else root.PerfUnlock = api;
})(typeof window !== 'undefined' ? window : globalThis);
