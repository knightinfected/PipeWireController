"""Re-pointing a virtual device's output so the change survives a restart.

Shared by the Mixer's output picker and the Devices page's "Sends to" chip,
so the two can never disagree about which stored setting a move updates.
Moved here unchanged from `ui/dashboard.py` on 2026-10-22.
"""

from __future__ import annotations

from . import chains, enhance, paths, virtual


def remember_output(node_name: str, target_name: str) -> str:
    """Record a new output on whichever of our objects publishes this sink.

    Moving the stream takes effect at once, but an object that carries its own
    target in its generated conf goes straight back to it the next time its
    unit restarts — so the *stored* target has to move too or the change
    quietly undoes itself on the next reboot.  `save_meta` writes it without
    regenerating or restarting, which matters: restarting removes the sink and
    tears down every stream feeding it, and the whole point here is to re-point
    a device while audio keeps playing through it.

    Returns a note to append to the toast, or ''.  A hand-written chain in
    `filter-chain.conf.d` belongs to nobody and needs no note — nothing will
    overwrite the move.
    """
    for items, setter in (
            (chains.list_chains(),
             lambda o, t: (setattr(o, 'target', t), chains.save_meta(o))),
            (enhance.list_enhancements(),
             lambda o, t: (o.params.__setitem__('target', t),
                           enhance.save_meta(o))),
            (virtual.list_devices(),
             lambda o, t: (setattr(o, 'target', t), virtual.save_meta(o)))):
        for obj in items:
            if obj.node_name == node_name:
                setter(obj, target_name)
                return ''

    for strip in paths.list_strips():
        if strip.node_name != node_name:
            continue
        # A strip feeding several outputs is a fan, and which leg the user
        # meant is not answerable from one dropdown — so move it live and say
        # where the permanent version of the setting lives.
        if len(strip.outputs) > 1:
            return ' (until restart — set outputs in Signal Paths)'
        strip.outputs = [target_name]
        paths.save_meta(strip)
        return ''
    return ''
