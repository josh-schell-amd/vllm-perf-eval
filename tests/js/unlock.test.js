// The page opens what seal.py sealed. fixtures/sealed.json is sealed by
// seal.py with the login below; tests/test_seal.py checks the same file.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const u = require('../../site/js/unlock.js');
const envelope = require(path.join(__dirname, 'fixtures', 'sealed.json'));

test('the right login opens the payload', async () => {
  const key = await u.deriveKey('viewer', 'fixture-password', envelope);
  const payload = await u.openWith(envelope, key);
  assert.deepEqual(payload.models, [{ model: 'org/Fixture-8B' }]);
});

test('a wrong password or username does not', async () => {
  for (const [user, pass] of [['viewer', 'wrong'], ['someone', 'fixture-password']]) {
    const key = await u.deriveKey(user, pass, envelope);
    await assert.rejects(u.openWith(envelope, key));
  }
});

test('a kept key opens it again without the password', async () => {
  const key = await u.deriveKey('viewer', 'fixture-password', envelope);
  const kept = await u.importKey(await u.exportKey(key));
  assert.deepEqual((await u.openWith(envelope, kept)).models, [{ model: 'org/Fixture-8B' }]);
});

test('an unknown format is refused before any decryption', async () => {
  await assert.rejects(u.deriveKey('viewer', 'fixture-password', { ...envelope, v: 2 }), /unknown data format/);
});
