// A room member holds no gamepad (null), one (its number), or several (a list
// in slot order): selkies hands a token's list to that member's local pads in
// turn, the first pad the first slot.

/** The slots a member's `slot` names, in the order its pads take them. */
export const slotsOf = (slot) => {
    const named = Array.isArray(slot) ? slot : [slot];
    return named.filter(s => Number.isInteger(s) && s > 0);
};
