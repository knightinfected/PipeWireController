# Contributing

Thanks for wanting to help. PipeWire Controller is maintained by one person,
and it holds itself to one rule above the rest: **a change must not break
anyone's working audio setup.** That means on any distro, for someone
installing fresh and for someone upgrading from an older version.
Everything below follows from that.

## Before you start

- **Bug fixes:** go ahead. If there's no issue yet, say in the PR what was
  wrong and how to see it.
- **Anything bigger** (a feature, a new page, a change to how something
  works): **open an issue first** and describe what you have in mind. Bigger
  changes here get designed before they're built, and it's better to agree
  on the shape before you spend time on code that might not fit.
- **Questions and ideas** go in
  [Discussions](https://github.com/knightinfected/PipeWireController/discussions).
- Using AI tools is fine. What matters is that you understand the change,
  have run it, and can answer questions about it.

## Running it

```bash
git clone https://github.com/knightinfected/PipeWireController.git
cd PipeWireController
./pipewire-control-center
```

There's no build step. To open a page directly, set `PWCTL_PAGE`, for
example `PWCTL_PAGE=devices ./pipewire-control-center`. The page names are
`dashboard`, `paths`, `graph`, `devices`, `virtual`, `surround`, `server`,
`streams`, `policy`, `wireplumber`, `chains`, `effects`, `enhance`, `hrir`,
`monitor` and `tools`.

The unit tests run with:

```bash
python3 -m unittest discover -s tests
```

If your change has logic that can be tested without a running PipeWire, add
a test under `tests/`.

## What a good change looks like

**It's been run on a real PipeWire session.** Say which distro, and which
PipeWire and WirePlumber versions. If it touches devices, say what hardware.
If there's something you couldn't test (Bluetooth, a distro you don't have),
say so. That's useful to know, not a weakness.

**It doesn't break existing setups.**
- The app writes only to the paths listed under
  [Where things are written](README.md#where-things-are-written). Never edit
  PipeWire's or WirePlumber's own config files.
- Someone upgrading must keep their chains, virtual devices, equalizers and
  settings working. If your change alters something the app saves, raise it
  in the issue first.
- No new dependencies beyond the ones under
  [Requirements](README.md#requirements) without discussing it first.

**It checks instead of guessing.** Distros package PipeWire very
differently. Fedora, Debian and Ubuntu split plugins and backends into
separate packages, libraries can live in `/usr/lib64` or
`/usr/lib/<triplet>/` instead of `/usr/lib`, and versions vary. Don't
decide what a system has from a version number, or from how Arch does it.
Check for the actual file, flag or feature at the moment you need it, and
treat "can't tell" differently from "no". `pwctl/backend/backends.py` is the
example to follow. The portability bugs this project has shipped so far
came from guesses like these.

**It fits the code that's there.**
- `pwctl/backend/` is plain logic with no GTK. `pwctl/ui/` is the
  interface.
- Anything that runs a command (`pw-dump`, `wpctl`, `systemctl`, …) runs off
  the UI thread, through `widgets.async_call`.
- Text from the live system that goes into a libadwaita row's title or
  subtitle goes through `widgets.esc()`. Those are Pango markup, and a stray
  `&` or `<` blanks the whole label.
- A page that shows live state updates its rows in place. Rebuilding every
  row on each refresh closes expanded rows, fights a slider while it's being
  dragged and restarts the level meters.
- New settings go in `pwctl/backend/schema.py`. The pages build their rows
  from it.
- Match the surrounding code (naming, comment style) and the app's look, in
  both light and dark mode.

## Pull requests

- **One change per PR.** Describe what was wrong or missing, what you
  changed, and how you checked it. The PR template asks for exactly that.
- **UI changes:** include before and after screenshots, in light and dark.
- **Leave `CHANGELOG.md` and `README.md` to me.** I write those up from your
  PR description, and I edit both often, so changes to them tend to
  conflict.
- **Reviews:** I'll reply as soon as I can. A PR waiting on me is mine to
  sort out. If I ask you something and there's no reply within **14 days**,
  I may finish the change myself on top of your commits (your commits and
  the credit stay yours), or close it if it can't go in as it is. You're
  always welcome to pick it back up.

## License

By contributing, you agree that your work is licensed under the project's
license, [GPL-3.0-or-later](LICENSE).
