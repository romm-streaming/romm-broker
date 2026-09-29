// Opacity for the Selkies touch gamepad, set from the room.
//
// The gamepad is the universal-touch-gamepad add-on, bundled into the Selkies
// page that runs in the stream frame, and it has no opacity setting of its
// own. The frame is same-origin, so the room adds a <style> to its document
// instead. The selectors are the add-on's class names: if upstream renames
// them the rule stops matching and the controls simply stay opaque.

export const STORAGE_KEY = 'collab_touch_opacity';
export const STYLE_ID = 'collab-touch-opacity';
/** Below this the controls are too faint to find again. */
export const MIN_TOUCH_OPACITY = 0.2;

/** Reads a stored value; anything missing or unreadable means fully opaque. */
export const parseTouchOpacity = (raw) => {
    if (raw === null || raw === undefined || raw === '') return 1;
    const value = Number(raw);
    if (!Number.isFinite(value)) return 1;
    return Math.min(1, Math.max(MIN_TOUCH_OPACITY, value));
};

/**
 * Applies `value` to the gamepad in `doc`. Full opacity removes the style so
 * the add-on renders exactly as it ships. Returns false when `doc` has no
 * head to add to yet (the frame is still on about:blank or mid-load).
 */
export const applyTouchOpacity = (doc, value) => {
    if (!doc || !doc.head) return false;
    const opacity = parseTouchOpacity(value);
    let style = doc.getElementById(STYLE_ID);
    if (opacity >= 1) {
        if (style) style.remove();
        return true;
    }
    if (!style) {
        style = doc.createElement('style');
        style.id = STYLE_ID;
        doc.head.appendChild(style);
    }
    // The joystick handle and trigger fill are children, so they fade with
    // their base. Opacity has no effect on hit testing: faint buttons still
    // take touches.
    style.textContent = `.touch-gamepad-control, .settings-icon-host { opacity: ${opacity}; }`;
    return true;
};
