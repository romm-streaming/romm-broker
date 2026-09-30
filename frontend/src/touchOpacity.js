// Opacity for the Selkies touch gamepad, set from the room.
//
// The gamepad is the universal-touch-gamepad add-on, bundled into the Selkies
// page that runs in the stream frame, and it has no opacity setting of its
// own. The frame is same-origin, so the room adds a <style> to its document
// instead. The selectors are the add-on's own id and class names: if upstream
// renames them the rule stops matching and the controls simply stay opaque.

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
 * Applies `value` to the gamepad in `doc`. Full opacity removes the style so
 * the add-on renders exactly as it ships. A frame with no head yet (still on
 * about:blank, or mid-load) is skipped; its load event applies it again.
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
    // The joystick handle and trigger fill are children, so they fade with
    // their base. Opacity has no effect on hit testing: faint buttons still
    // take touches.
    style.textContent = `${OVERLAY} .touch-gamepad-control, ${OVERLAY} .settings-icon-host { opacity: ${opacity}; }`;
};
