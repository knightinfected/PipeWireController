"""State reconciliation for the live controls on the Devices page."""


def sync_device_levels(controls, nodes, now, grace_seconds,
                       default_held=False):
    """Apply node volume/mute/default values without replacing row widgets.

    ``controls`` is keyed by ``(node.id, node.serial)``. ``node.name`` is not
    unique in PipeWire, and a freed id is handed to the next new object, so
    only the pair names one live node.

    The default star is repainted on every row unless ``default_held`` — the
    user has just clicked one and the graph has not caught up yet.  It is
    separate from the per-row grace, which only covers the row's own volume
    and mute.

    Return ``False`` when node membership or object ids changed so the caller
    can rebuild the rows and their callbacks once.
    """
    current = {(node.id, node.serial): node for node in nodes}
    if set(controls) != set(current):
        return False

    for key, state in controls.items():
        node = current[key]
        if not default_held:
            state['star'](node.is_default)
        if now - state['local_ts'] < grace_seconds:
            continue
        if node.volume is not None:
            state['volume'].set_value(node.volume)
        state['updating'] = True
        state['mute'].set_active(node.muted)
        state['updating'] = False
    return True
