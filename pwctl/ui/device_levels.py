"""State reconciliation for the live controls on the Devices page."""


def sync_device_levels(controls, nodes, now, grace_seconds):
    """Apply node volume/mute values without replacing existing row widgets.

    ``controls`` is keyed by ``(node.id, node.serial)``. ``node.name`` is not
    unique in PipeWire, and a freed id is handed to the next new object, so
    only the pair names one live node.

    Return ``False`` when node membership or object ids changed so the caller
    can rebuild the rows and their callbacks once.
    """
    current = {(node.id, node.serial): node for node in nodes}
    if set(controls) != set(current):
        return False

    for key, state in controls.items():
        if now - state['local_ts'] < grace_seconds:
            continue
        node = current[key]
        if node.volume is not None:
            state['volume'].set_value(node.volume)
        state['updating'] = True
        state['mute'].set_active(node.muted)
        state['updating'] = False
    return True
