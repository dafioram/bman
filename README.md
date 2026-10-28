# bman

A menu-driven manager for Btrfs on Debian. Run `sudo bman` and pick from numbered
menus to look after filesystems, devices, subvolumes and snapshots on this
machine. It runs the `btrfs` tools for you, shows the exact command before
anything long or destructive, and asks before it changes anything.

```
bman  Btrfs on this host - 3 filesystems, 7 subvolumes, 5 snapshots, 1 with missing devices

1  Filesystems       5  Space & cleanup
2  Subvolumes        6  System info
3  Snapshots         h  Help
4  Disks
b back · q quit
>
```

There are no subcommands and nothing to remember: every screen lists what you can
do. Type a number or a letter and press Enter. Enter alone refreshes, `b` goes
back, `m` returns to the main menu, `q` quits. bman runs no background service and
keeps no history; every screen reads the live state again. It is the Btrfs
sibling of `zman` and works the same way.

## Requirements

- Debian 12 or 13 (or a close relative such as Ubuntu), Linux with Btrfs support.
- `btrfs-progs` (`apt install btrfs-progs`), plus `util-linux` and `mount`, which
  are always installed.
- Python 3.9 or newer, standard library only.
- Root: Btrfs details can only be read as root, so bman refuses to start
  otherwise and says so.

## Install

```sh
git clone <this repository> bman
cd bman
sudo ./install.sh          # or: sudo ./install.sh --yes
sudo bman
```

| Path | What |
| --- | --- |
| `/usr/local/bin/bman` | the launcher |
| `/usr/local/lib/bman/` | the program, `VERSION` and an install marker |
| `/etc/bman/bman.conf` | config, written only if it does not exist |

If `./install.sh` says `Permission denied`, the files lost their executable bit
(this happens when a zip is unpacked or committed by some tools). Run
`sudo sh install.sh` instead, and restore the bit in git so CI's check passes:
`git update-index --chmod=+x install.sh uninstall.sh bman.py`.

Reinstalling (for example after `git pull`) replaces the program in place and keeps
the config. The installed copy does not depend on the folder you installed from.
Both scripts refuse to touch any file at those paths that they did not create.

To remove it: `sudo ./uninstall.sh` (asks, and asks separately about the config),
or `sudo ./uninstall.sh --yes --purge` to remove the config too. Neither script
ever touches Btrfs or your data. `BMAN_PREFIX` and `BMAN_CONF_DIR` change where
they install, which is how the tests run them without root.

## How bman lays things out

- **One subvolume per filesystem by default.** A new filesystem gets a subvolume
  (default name `data`) mounted where you choose, with `subvol=/data`. The top
  level stays unmounted. This keeps restore possible and the layout tidy.
- **Snapshots next to the subvolumes.** bman keeps its read-only snapshots in
  `.snapshots/<subvolume>/<name>` at the top level of the filesystem, not inside
  the subvolume. They survive restoring or deleting the subvolume.
- **A private working mount.** To reach the top level, bman mounts it under
  `/run/bman/<uuid>` when it needs to, and unmounts it when you quit. When you
  unmount a filesystem's last mount from bman, it keeps this working mount so you
  can still restore or snapshot it.
- **Seeing compression.** Each mount line in a filesystem's view shows its
  compression (`/data  (top level, compress=zstd:3)`), and each subvolume's view,
  including `(top level)`, shows what applies to new files there: its own
  property if set, otherwise the mount option.
- **Compression on by default.** bman suggests `compress=zstd:1,noatime` when it
  mounts a filesystem or writes an fstab line (change it in the config). Each
  subvolume also has its own compression setting, stored in the filesystem.
  Mount options apply to the whole filesystem: the first mount's options win, and
  bman shows what is really in effect.
- **fstab only when you ask, safely.** Adding a line backs up the file
  (`fstab.bman-YYYYMMDD-HHMMSS`), checks the new file with `findmnt --verify`, and
  only then swaps it in. Lines use the filesystem UUID and `nofail`, so a missing
  disk does not stop the machine from booting. Removing a line comments it out.

## What it does

**Filesystems.** List with state, profile, size, free space and where each is
mounted; problems (missing device, degraded mount, device errors, scrub errors,
chunks that are not mirrored, RAID5/6, almost full) are red and listed first. A
filesystem's view shows profiles per chunk type, space, mounts, the last scrub,
running balances and replaces, and quotas. From there:

- Start, stop and resume a scrub, and watch it with auto-refresh.
- **Balance: reclaim space** (rewrites data chunks less than half full),
  **Balance: change profiles** (e.g. single → RAID1 after adding a disk, or DUP on
  one disk), and **Fix chunks that are not mirrored** (after a degraded mount),
  with pause, resume and stop.
- Add a device. Reset error counters. Change the label.
- Enable or disable quotas: full quotas (sizes for every subvolume and snapshot,
  but slower snapshot deletion and balance) or, with kernel and btrfs-progs 6.7+,
  simple quotas (fast, per-subvolume usage, no snapshot sizes).
- Kernel messages about the filesystem (from the journal), including following
  them live.
- Mount an unmounted filesystem (its default subvolume, the top level, or a
  subvolume by path), with the `degraded` option and a clear warning when a device
  is missing. Wipe an unmounted filesystem.

**Creating a filesystem** (`c` on the Filesystems list): a label, a layout, free
disks, then a setup screen.

- Layouts: single disk; single disk with two copies (DUP: every block twice on the
  one disk, which protects against bad sectors but not against the disk dying);
  RAID1; RAID1C3; RAID1C4; RAID10. Metadata gets its own profile, at least as safe
  as data.
- Disks of different sizes need nothing special: Btrfs fills them on its own, and
  the setup screen shows the real capacity (for example 4 TB + 2 TB + 2 TB in
  RAID1 gives 4 TB).
- Only free disks are offered: not mounted, not swap, not in a Btrfs filesystem,
  not used by LVM, RAID or an open LUKS device. A disk that still holds a
  filesystem or partitions is shown in yellow and needs an extra typed
  confirmation. Disks are given to the tools by their `/dev/disk/by-id` name.
- Settings: subvolume name (or none), mountpoint, compression, access times, and
  whether to add the fstab line. You see every command, then type the label.

**Devices** (filesystem → Devices): devid, device, state, size, allocation and
serial number. For a device:

- **Replace with another disk** (`btrfs replace`, also for a missing device),
  then watch its progress. Afterwards, Grow to full size if the new disk is bigger.
- **Remove from filesystem** (moves its data to the others) or **Remove the
  missing device** (rebuilds the lost copies on the others). Only offered when the
  profiles still fit on the remaining devices.
- Grow to full size, reset error counters, and links to the disk and filesystem.

The usual way to deal with a dead disk in RAID1: Filesystems → the red filesystem
(mount it first if needed; bman adds `degraded`) → Devices → the missing device →
Replace with another disk → watch it finish → **Fix chunks that are not
mirrored** if bman shows it, because writes made while degraded have only one copy.

**Subvolumes.** Across all mounted filesystems, with state, snapshot count, size
(with quotas) and where each is mounted. Each mounted filesystem also has a
`(top level)` row: its root, which is where your files are if you use the drive
without subvolumes. It shows the compression in effect, can be snapshotted (into
`.snapshots/_top/`), and is never offered for delete, rename or restore; to get
files back from a top-level snapshot, open a shell in the snapshot and copy them. A subvolume's view: take a snapshot, list
its snapshots, mount and unmount, add to or remove from fstab, compression, open a
shell in it, rename, delete. Delete is refused while it is mounted, is the default
subvolume, holds other subvolumes, is in fstab, or is your running system.

**Snapshots.** All snapshots, or one subvolume's, newest first, with who made them
(bman, snapper, Timeshift or other) and their size when known (`z` measures the
page with `btrfs filesystem du`). For a snapshot:

- **Restore it over the subvolume.** The current subvolume is renamed
  `<name>.before-restore-<date>` and kept, and a writable copy of the snapshot
  takes its name, so mounts and fstab lines find it. Not offered while the
  subvolume is mounted, is the default subvolume, or is your running system (use
  snapper or grub-btrfs for that).
- Copy to a new subvolume, open a read-only shell in it to copy files out, measure
  it, rename, delete.

**Disks.** Every disk with size, how it is used, type, serial, model and by-id
name, linking to its filesystem and device.

**Space & cleanup.** Raw size, used and free per filesystem, data chunks that are
allocated but unused (with a link to reclaim them), snapshot counts and sizes, and
**Prune snapshots**: pick from a list and type the count. Snapshots made by snapper
or Timeshift and mounted snapshots are never listed.

**System info.** bman and btrfs-progs versions, kernel, whether simple quotas are
available, the config, fstab and working mounts, scheduled scrubs, and snapshot
tools found.

## Safety

Every change goes preview → confirm → re-check → run:

- The preview says what is affected, what is kept, and the exact commands.
- Confirmation matches the risk: `[Y/n]` for harmless actions (Enter means yes),
  `[y/N]` for destructive ones (Enter means no), and typing a name for anything
  that erases data: using a disk (the disk's name), creating or wiping a
  filesystem (its label), deleting a subvolume or restoring over it (its name),
  and bulk pruning (the count).
- Right before running, bman reads the state again. If anything changed, it runs
  nothing and says so.
- No command goes through a shell. Text from the tools and disks is cleaned of
  control characters before it is shown.

## Left out on purpose

- Automatic snapshots and scheduled scrubs: they need a background service. Use
  snapper or btrbk, and the btrfsmaintenance package; their snapshots show up in
  bman.
- Creating or converting to RAID5/6 (still marked unstable upstream; existing ones
  are flagged red), defragmenting (it breaks the sharing between snapshots),
  send/receive, seed devices, swap files, and per-file `nodatacow`.
- Restoring the subvolume your running system boots from.
- Live I/O monitoring: use `iostat` or `btop`.
- A diff between a snapshot and the current files: Btrfs has no fast way; open a
  shell in the snapshot instead.

## Configuration

An INI file; the first that exists wins: `--config FILE`, `$BMAN_CONFIG`,
`~/.config/bman/bman.conf`, `/etc/bman/bman.conf`. A bad value shows a warning in
the app and the default is used.

```ini
[bman]
page_size = 0                           # rows per list page; 0 fits the terminal
command_timeout = 60                    # seconds for quick btrfs calls
long_timeout = 600                      # seconds for slow ones (measuring, pruning)
refresh_interval = 2                    # seconds between auto-refresh redraws
snapshot_prefix = manual                # suggested names: manual-2026-10-27-1422
mount_options = compress=zstd:1,noatime # used when bman mounts or writes fstab lines
work_dir = /run/bman                    # where bman mounts top levels while it works
fstab = /etc/fstab                      # the fstab bman reads and edits
```

The only command-line options are `--config FILE`, `--version` and `--help`.
`NO_COLOR` turns colors off. bman works at 80×24 and over SSH and in tmux.

## Development

```
bman.py               entry script (run it; never imported)
bmanlib/
  app.py              options, terminal check, startup check, cleanup on exit
  runner.py           run / run_fg, the pinned tools, working mounts, error hints
  model.py            data classes, parsers, profiles and capacity, pure decisions
  btrfs.py            reading Btrfs, mount and disk state (runner passed in)
  fstab.py            back up, verify and swap /etc/fstab
  ui.py               menus, lists, prompts, confirmations, paging
  textfmt.py, config.py, errors.py
  screens/            main menu, filesystems, devices, subvolumes, snapshots, disks, space, info
tests/
  test_model.py, test_core.py, test_decisions.py   unit tests, nothing installed
  test_install.py + install_cycle.sh                install → reinstall → uninstall
  test_integration.py                               real Btrfs on loop devices, opt-in
  test_walkthrough.py + term.py + mock/             screens in a pseudo-terminal
```

Run the tests:

```sh
python3 -m unittest discover -s tests -t .          # unit + install cycle (+ walkthrough if pyte is installed)
pip install pyte && python3 -m unittest tests.test_walkthrough
sudo BMAN_INTEGRATION=1 python3 -m unittest tests.test_integration -v
shellcheck install.sh uninstall.sh tests/install_cycle.sh
```

The integration tests need root, `btrfs-progs` and `losetup`. They build
filesystems on loop devices backed by sparse files in a temporary folder, check
every parser and decision against the real tools (inventory, subvolumes,
properties, quotas, scrub, device stats, add/convert/remove, replace, fstab
verification, snapshots), and remove everything afterwards. They never run unless
`BMAN_INTEGRATION=1` is set.

The walkthrough drives bman in a pseudo-terminal against a mock system (fake
`btrfs`, `mkfs.btrfs`, `mount`, `findmnt`, `lsblk` and friends that keep state in
a JSON file) containing a missing device mounted degraded, chunks that are not
mirrored, device errors, a scrub that found errors, an unmounted filesystem, a
Btrfs root filesystem, nested and snapper snapshots, a writable copy, quotas, and
free, partitioned, LVM and system disks. It checks every list, view, cancel path
and the keys at 80×24 and 160×40. `WALK_OUT=dir` saves every rendered screen. To
try the screens on any machine: `python3 tests/mock/run_mock.py`.

CI (`.github/workflows/ci.yml`) runs the unit tests on Python 3.9 to 3.14, the
walkthrough, the integration tests on a real kernel, ShellCheck and the install
cycle.

`VERSION` holds one whole number; raise it by one for each release.
