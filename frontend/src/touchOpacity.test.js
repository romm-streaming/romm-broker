// Run with `npm test` in frontend/; no dependencies beyond Node itself.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
    MIN_TOUCH_OPACITY,
    STYLE_ID,
    applyTouchOpacity,
    parseTouchOpacity,
} from './touchOpacity.js';

// Just enough of a Document for the one <style> element the helper manages.
const fakeDoc = () => {
    const byId = new Map();
    const head = {
        children: [],
        appendChild(el) {
            this.children.push(el);
            el.parentNode = this;
            byId.set(el.id, el);
            return el;
        },
        removeChild(el) {
            this.children = this.children.filter(c => c !== el);
            byId.delete(el.id);
            el.parentNode = null;
        },
    };
    return {
        head,
        getElementById: (id) => byId.get(id) || null,
        createElement: (tag) => ({
            tagName: tag.toUpperCase(),
            id: '',
            textContent: '',
            remove() { if (this.parentNode) this.parentNode.removeChild(this); },
        }),
    };
};

test('nothing stored means fully opaque, the add-on as it ships', () => {
    assert.equal(parseTouchOpacity(null), 1);
    assert.equal(parseTouchOpacity(undefined), 1);
    assert.equal(parseTouchOpacity(''), 1);
    assert.equal(parseTouchOpacity('  '), 1);
});

test('a stored value inside the range comes back as is', () => {
    assert.equal(parseTouchOpacity('0.5'), 0.5);
    assert.equal(parseTouchOpacity(0.35), 0.35);
});

test('a stored value outside the range is clamped, never lost', () => {
    assert.equal(parseTouchOpacity('0'), MIN_TOUCH_OPACITY);
    assert.equal(parseTouchOpacity('-3'), MIN_TOUCH_OPACITY);
    assert.equal(parseTouchOpacity('7'), 1);
});

test('garbage in storage falls back to fully opaque', () => {
    assert.equal(parseTouchOpacity('abc'), 1);
    assert.equal(parseTouchOpacity('NaN'), 1);
    assert.equal(parseTouchOpacity('Infinity'), 1);
});

const LIVE = '#universal-touch-gamepad-controls-overlay';
const cssFor = (opacity) =>
    `${LIVE} .touch-gamepad-control, ${LIVE} .settings-icon-host { opacity: ${opacity}; }`;

test('a partial opacity adds one style covering the controls and the settings icon', () => {
    const doc = fakeDoc();
    applyTouchOpacity(doc, 0.4);
    assert.equal(doc.head.children.length, 1);
    assert.equal(doc.getElementById(STYLE_ID).textContent, cssFor(0.4));
});

test('every selector is scoped to the live overlay, so profile-picker previews keep full opacity', () => {
    const doc = fakeDoc();
    applyTouchOpacity(doc, 0.4);
    const selectors = doc.getElementById(STYLE_ID).textContent.split('{')[0].split(',');
    assert.equal(selectors.length, 2);
    for (const selector of selectors) assert.ok(selector.trim().startsWith(`${LIVE} `), selector);
});

test('changing the value updates the style in place rather than stacking another', () => {
    const doc = fakeDoc();
    applyTouchOpacity(doc, 0.4);
    applyTouchOpacity(doc, 0.7);
    assert.equal(doc.head.children.length, 1);
    assert.equal(doc.getElementById(STYLE_ID).textContent, cssFor(0.7));
});

test('full opacity takes the style out and leaves the add-on untouched', () => {
    const doc = fakeDoc();
    applyTouchOpacity(doc, 0.4);
    applyTouchOpacity(doc, 1);
    assert.equal(doc.head.children.length, 0);
    assert.equal(doc.getElementById(STYLE_ID), null);
});

test('full opacity on a fresh document adds nothing', () => {
    const doc = fakeDoc();
    applyTouchOpacity(doc, 1);
    assert.equal(doc.head.children.length, 0);
});

test('an out-of-range value is clamped before it reaches the css', () => {
    const doc = fakeDoc();
    applyTouchOpacity(doc, 0);
    assert.equal(doc.getElementById(STYLE_ID).textContent, cssFor(MIN_TOUCH_OPACITY));
});

test('a frame with no document yet, or no head, is skipped without throwing', () => {
    assert.doesNotThrow(() => applyTouchOpacity(null, 0.5));
    assert.doesNotThrow(() => applyTouchOpacity({}, 0.5));
    assert.doesNotThrow(() => applyTouchOpacity({ head: null }, 0.5));
});
