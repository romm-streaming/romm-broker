// Run with `npm test` in frontend/; no dependencies beyond Node itself.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { showsMenuButton, toggleEmulatorMenu } from './emulatorMenu.js';

test('the controller sees the button when the emulator is RetroArch', () => {
    assert.equal(showsMenuButton({ userRole: 'controller', emulator: 'retroarch' }), true);
});

test('viewers never see it, whatever their permission', () => {
    assert.equal(showsMenuButton({ userRole: 'viewer', userPermission: 'participant', emulator: 'retroarch' }), false);
    assert.equal(showsMenuButton({ userRole: 'viewer', userPermission: 'readonly', emulator: 'retroarch' }), false);
});

test('any other emulator hides it from the controller too', () => {
    for (const emulator of ['pcsx2', 'dolphin', 'duckstation', 'desktop', 'RetroArch', '']) {
        assert.equal(showsMenuButton({ userRole: 'controller', emulator }), false, emulator);
    }
});

test('a broker too old to name the emulator hides it', () => {
    assert.equal(showsMenuButton({ userRole: 'controller' }), false);
    assert.equal(showsMenuButton({ userRole: 'controller', supportsMenu: true }), false);
    assert.equal(showsMenuButton(null), false);
});

test('a toggle posts the seat token to the menu route', async () => {
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push({ url, init });
        return { ok: true };
    };

    assert.equal(await toggleEmulatorMenu(fetchImpl, 'tok en&x'), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'api/session/menu?token=tok+en%26x');
    assert.equal(calls[0].init.method, 'POST');
});

test('a refused or failed toggle reports false instead of throwing', async () => {
    assert.equal(await toggleEmulatorMenu(async () => ({ ok: false, status: 409 }), 't'), false);
    assert.equal(await toggleEmulatorMenu(async () => { throw new Error('offline'); }, 't'), false);
});
