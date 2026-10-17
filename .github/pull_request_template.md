<!--
Thanks for the PR! Please fill this in and delete anything that doesn't
apply. CONTRIBUTING.md has the details.
-->

## What this changes

<!-- What was wrong or missing, and what you changed. Link the issue, e.g. "Fixes #123". -->

## How you tested it

<!-- What you did, and what you saw happen. -->

**System.** Paste the output of these two commands (they work in any shell):

```
sh -c '. /etc/os-release; echo "distro: $PRETTY_NAME"; echo "pipewire: $(pipewire --version | tail -1)"; echo "wireplumber: $(wireplumber --version | tail -1)"'
python3 -c 'import gi; gi.require_version("Adw", "1"); from gi.repository import Adw; print("libadwaita: %d.%d.%d" % (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()))'
```

```
paste here
```

## Checklist

- [ ] This PR is one change.
- [ ] I ran it on a real PipeWire session, from a git checkout (`./pipewire-control-center`).
- [ ] An existing setup still works afterwards (chains, virtual devices, equalizers, settings).
- [ ] It writes only to the paths listed under [Where things are written](https://github.com/knightinfected/PipeWireController/blob/main/README.md#where-things-are-written) in the README.
- [ ] It adds no new dependencies, or I've explained why below.
- [ ] For a UI change: before and after screenshots, in light and dark.
- [ ] `python3 -m unittest discover -s tests` passes.

## Not tested

<!-- Anything you couldn't check: particular hardware, Bluetooth, a distro you don't have. -->
