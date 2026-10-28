"""Help texts. Each list explains its keys, then its columns and status words."""
from __future__ import annotations

LIST_KEYS = """\
Keys
  1-N      open the item with that number (numbers count from the top of the page)
  Enter    refresh: read the live state again
  n / p    next / previous page
  s        choose how the list is sorted
  f        filter: type words that must all match; a blank filter clears it
  h        this help
  b        back    m  main menu    q  quit
  Ctrl-C at a prompt is the same as b; Ctrl-D quits.
"""

MAIN = """\
bman manages Btrfs on this machine. It runs the btrfs tools for you and shows
the exact command before anything long or destructive.

Words Btrfs uses
  filesystem  One Btrfs filesystem, on one or more devices (disks).
  device      A disk (or partition) that is part of a filesystem.
  profile     How blocks are stored: single (one copy), DUP (two copies on
              one disk), RAID1 (two copies on different disks), RAID1C3/C4
              (three/four copies), RAID10 (two copies, striped). Data and
              metadata each have their own profile.
  chunk       Btrfs stores data in chunks of about 1 GB; each has a profile.
  subvolume   A separately mountable tree of files inside a filesystem.
  top level   The root of the filesystem, which holds all subvolumes.
  snapshot    A read-only copy of a subvolume at one moment. It only costs
              the space of data that changed since.
  scrub       Reads every block and checks it, repairing from another copy.
  balance     Rewrites chunks: to change profiles or give back space.
  degraded    Mounted with a device missing. Writes are then not mirrored.
  quotas      Optional accounting that shows how much space each subvolume
              and snapshot holds on its own.

Keys everywhere
  numbers pick things · Enter refresh · b back · m main menu · q quit
  Ctrl-C at a prompt goes back; while a command runs it stops that command.
  Ctrl-D quits.

Sizes are 1000-based (1 GB = 1,000,000,000 bytes), the way drives are sold.
When you type a size, 500G means 500 GB; write 500GiB for 1024-based.

bman never schedules anything. For automatic snapshots use snapper or btrbk;
for scheduled scrubs, the btrfsmaintenance package.

bman keeps its own snapshots in .snapshots/<subvolume>/ at the top level of
each filesystem. To reach the top level it mounts it under /run/bman while it
runs, and unmounts it when you quit.
"""

FILESYSTEMS = LIST_KEYS + """\
  c        create a new filesystem on free disks

Columns
  STATE    ok · not mounted · N devices missing (a disk is gone; replace it)
           · mounted degraded (writes are not mirrored) · N device errors
           (read, write or checksum errors since the counters were reset)
           · scrub found errors · not all chunks mirrored (some data was
           written with less protection, e.g. while degraded; fix it with a
           balance) · RAID5/6 (unstable) · almost full (under 10% free).
  PROFILE  data profile, then metadata profile when they differ.
  FREE     Btrfs's estimate of space left for data.
"""

DEVICES = LIST_KEYS + """\
Columns
  DEVID    Btrfs's number for the device inside the filesystem.
  STATE    ok · missing · N errors (see the device's view for which kind).
  SIZE / USED   the device's size in the filesystem and how much is allocated.
  SERIAL   the drive's serial number, printed on its label.
"""

SUBVOLUMES = LIST_KEYS + """\
  c        create a new subvolume

Snapshots are listed separately (main menu: Snapshots). Each mounted filesystem
also has a "(top level)" row: its root, where files live when there are no
subvolumes. It can be snapshotted and has a compression setting, but it can
never be deleted, renamed or restored over.
Columns
  STATE    mounted · mounted read-only · not mounted · default (what a plain
           mount of the filesystem shows) · system (your running system lives
           on it).
  SNAPS    how many snapshots of it exist.
  SIZE     space only it holds; needs quotas (filesystem view: Enable quotas).
  MOUNTED AT   where it is mounted (+N when it is mounted in more places).
"""

SNAPSHOTS = LIST_KEYS + """\
  z        measure the space each snapshot on this page holds on its own (slow)

Columns
  CREATED  how long ago it was taken.
  SIZE     space only this snapshot holds: what deleting it alone would free.
           Shown when quotas are on, or after z. Several snapshots can share
           data, so deleting a group can free more than the sum.
  MADE BY  bman (in .snapshots/), snapper, Timeshift, or other.
"""

DISKS = LIST_KEYS + """\
Columns
  USE      btrfs <name>: part of that filesystem.
           free: blank, can be used for a filesystem.
           free, has <fs> data / partitions / a ZFS label: not in use right
           now, but holds something; using it erases that.
           mounted, swap, LVM member, RAID member, in use: used by the system;
           bman never offers these.
  TYPE     HDD, SSD or NVMe.
  BY-ID    the stable name bman gives to the btrfs tools.
"""
