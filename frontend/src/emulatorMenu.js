// The room's button into the emulator's own menu (RetroArch's Quick Menu).
// A phone has no hotkey and no stick clicks, so this is its only way in.

/**
 * Whether the room shows the button: the controller only, and only when the
 * emulator is RetroArch. Viewers are left out even with input permission,
 * since the menu changes the session for everyone.
 */
export const showsMenuButton = (ctx) =>
    Boolean(ctx) && ctx.userRole === 'controller' && ctx.emulator === 'retroarch';

/**
 * Asks the broker to open or close the menu. Resolves to whether it took the
 * toggle; a refusal or a network failure is false, never a throw.
 */
export const toggleEmulatorMenu = async (fetchImpl, token) => {
    const query = new URLSearchParams({ token });
    try {
        const resp = await fetchImpl(`api/session/menu?${query}`, { method: 'POST' });
        return Boolean(resp.ok);
    } catch {
        return false;
    }
};
