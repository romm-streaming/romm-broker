// Selkies' touch gamepad add-on has no opacity setting, so the room styles it
// through the same-origin stream frame. If upstream renames the add-on's ids or
// classes, the rule stops matching and the controls simply stay opaque.

export const STORAGE_KEY = 'collab_touch_opacity';
export const STYLE_ID = 'collab-touch-opacity';
/** Below this the controls are too faint to find again. */
export const MIN_TOUCH_OPACITY = 0.2;

/** Reads a stored value; anything missing or unreadable means fully opaque. */
export const parseTouchOpacity = (raw) => {
    // Number() reads a blank string as 0, which would clamp to the faintest.
    if (raw === null || raw === undefined || String(raw).trim() === '') return 1;
    const value = Number(raw);
    if (!Number.isFinite(value)) return 1;
    return Math.min(1, Math.max(MIN_TOUCH_OPACITY, value));
};

/** The add-on's live overlay; its profile-picker previews sit outside it. */
const OVERLAY = '#universal-touch-gamepad-controls-overlay';

/**
 * Applies `value` to the gamepad in `doc`; full opacity removes the style.
 * A frame with no head yet is skipped, since its load event applies it again.
 */
export const applyTouchOpacity = (doc, value) => {
    if (!doc || !doc.head) return;
    const opacity = parseTouchOpacity(value);
    let style = doc.getElementById(STYLE_ID);
    if (opacity >= 1) {
        if (style) style.remove();
        return;
    }
    if (!style) {
        style = doc.createElement('style');
        style.id = STYLE_ID;
        doc.head.appendChild(style);
    }
    // Opacity leaves hit testing alone, so faint buttons still take touches.
    style.textContent = `${OVERLAY} .touch-gamepad-control, ${OVERLAY} .settings-icon-host { opacity: ${opacity}; }`;
};
