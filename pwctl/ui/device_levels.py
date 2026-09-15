"""State reconciliation for the live controls on the Devices page."""


def sync_device_levels(controls, nodes, now, grace_seconds):
    """Apply node volume/mute values without replacing existing row widgets.

    Return ``False`` when node membership or object ids changed so the caller
    can rebuild the rows and their callbacks once.
    """
    current = {node.name: node for node in nodes}
    expected = {name: state['node_id'] for name, state in controls.items()}
    observed = {name: node.id for name, node in current.items()}
    if expected != observed:
        return False

    for name, state in controls.items():
        if now - state['local_ts'] < grace_seconds:
            continue
        node = current[name]
        if node.volume is not None:
            state['volume'].set_value(node.volume)
        state['updating'] = True
        state['mute'].set_active(node.muted)
        state['updating'] = False
    return True
