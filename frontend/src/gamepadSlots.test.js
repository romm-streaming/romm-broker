// Run with `npm test` in frontend/; no dependencies beyond Node itself.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { slotsOf } from './gamepadSlots.js';

test('one gamepad is its number, as the room always sent it', () => {
    assert.deepEqual(slotsOf(2), [2]);
});

test('several keep the order their pads take them in', () => {
    assert.deepEqual(slotsOf([3, 4]), [3, 4]);
});

test('no gamepad is an empty list', () => {
    assert.deepEqual(slotsOf(null), []);
    assert.deepEqual(slotsOf(undefined), []);
    assert.deepEqual(slotsOf([]), []);
});

test('anything that is no slot is left out', () => {
    assert.deepEqual(slotsOf([0, '2', true, 1.5, 3]), [3]);
    assert.deepEqual(slotsOf('1'), []);
});
