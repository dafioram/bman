"""Sample output of the real tools, in the formats bman parses (btrfs-progs 6.x, C locale)."""

U1 = "3c5d7a9e-1111-4222-8333-000000000001"
U2 = "8f9e0a1b-2222-4333-8444-000000000002"
U3 = "77777777-3333-4444-8555-000000000003"

FI_SHOW = f"""\
Label: 'tank'  uuid: {U1}
\tTotal devices 2 FS bytes used 1267015770112
\tdevid    1 size 4000787030016 used 1395864371200 path /dev/sdb
\tdevid    2 size 4000787030016 used 1395864371200 path /dev/sdc

Label: none  uuid: {U2}
\tTotal devices 2 FS bytes used 950000000000
\tdevid    1 size 2000398934016 used 951000000000 path /dev/sdf
\tdevid    2 size 0 used 0 path <missing disk> MISSING

Label: 'old disk'  uuid: {U3}
\tTotal devices 2 FS bytes used 1000
\tdevid    1 size 3000592982016 used 500 path /dev/sdi
\t*** Some devices missing

"""

FI_USAGE = """\
Overall:
    Device size:\t\t      8001574060032
    Device allocated:\t\t      2791728742400
    Device unallocated:\t\t      5209845317632
    Device missing:\t\t                  0
    Device slack:\t\t                  0
    Used:\t\t\t      2534031540224
    Free (estimated):\t\t      2674922680320\t(min: 2674922680320)
    Free (statfs, df):\t\t      2674922680320
    Data ratio:\t\t\t               2.00
    Metadata ratio:\t\t               2.00
    Global reserve:\t\t          536870912\t(used: 0)
    Multiple profiles:\t\t                 no

Data,RAID1: Size:1387274436608, Used:1265270960128 (91.21%)
   /dev/sdb\t1387274436608
   /dev/sdc\t1387274436608

Metadata,RAID1: Size:8589934592, Used:3244998656 (37.78%)
   /dev/sdb\t8589934592
   /dev/sdc\t8589934592

System,RAID1: Size:33554432, Used:212992 (0.63%)
   /dev/sdb\t  33554432
   /dev/sdc\t  33554432

Unallocated:
   /dev/sdb\t2604922658816
   /dev/sdc\t2604922658816
"""

FI_USAGE_MIXED = """\
Overall:
    Device size:\t\t      4000797868032
    Device allocated:\t\t      1849000000000
    Device unallocated:\t\t      2151797868032
    Device missing:\t\t      2000398934016
    Used:\t\t\t      1798000000000
    Free (estimated):\t\t       100000000000\t(min: 90000000000)
    Data ratio:\t\t\t               1.98
    Metadata ratio:\t\t               2.00
    Multiple profiles:\t\t                yes\t(data)

Data,single: Size:20000000000, Used:18000000000 (90.00%)
   /dev/sdf\t20000000000

Data,RAID1: Size:900000000000, Used:880000000000 (97.78%)
   /dev/sdf\t900000000000
   missing\t900000000000

Metadata,RAID1: Size:4000000000, Used:2000000000 (50.00%)
   /dev/sdf\t4000000000
   missing\t4000000000

System,RAID1: Size:33554432, Used:16384 (0.05%)
   /dev/sdf\t33554432
   missing\t33554432
"""

DEV_STATS = """\
[/dev/sdb].write_io_errs    0
[/dev/sdb].read_io_errs     0
[/dev/sdb].flush_io_errs    0
[/dev/sdb].corruption_errs  0
[/dev/sdb].generation_errs  0
[/dev/sdc].write_io_errs    0
[/dev/sdc].read_io_errs     2
[/dev/sdc].flush_io_errs    0
[/dev/sdc].corruption_errs  3
[/dev/sdc].generation_errs  0
[devid:2].write_io_errs    0
"""

SCRUB_FINISHED = f"""\
UUID:             {U1}
Scrub started:    Sat Oct 17 03:00:01 2026
Status:           finished
Duration:         2:01:43
Total to scrub:   2.31TiB
Rate:             331.88MiB/s
Error summary:    no errors found
"""

SCRUB_RUNNING = f"""\
UUID:             {U1}
Scrub started:    Tue Oct 27 10:00:00 2026
Status:           running
Duration:         0:05:00
Time left:        0:15:00
ETA:              Tue Oct 27 10:20:00 2026
Total to scrub:   1.95TiB
Bytes scrubbed:   500.00GiB  (25.03%)
Rate:             1.67GiB/s
Error summary:    no errors found
"""

SCRUB_ERRORS = f"""\
UUID:             {U1}
Scrub started:    Sat Oct 17 03:00:01 2026
Status:           finished
Duration:         2:01:43
Total to scrub:   2.31TiB
Rate:             331.88MiB/s
Error summary:    read=2 csum=3
  Corrected:      4
  Uncorrectable:  1
  Unverified:     0
"""

SCRUB_ABORTED = SCRUB_RUNNING.replace("Status:           running", "Status:           aborted")
SCRUB_NONE = f"UUID:             {U1}\n\tno stats available\n"

BALANCE_RUNNING = "Balance on '/srv/tank' is running\n2 out of about 10 chunks balanced (3 considered),  80% left\n"
BALANCE_PAUSED = "Balance on '/srv/tank' is paused\n5 out of about 10 chunks balanced (6 considered),  50% left\n"
BALANCE_NONE = "No balance found on '/srv/tank'\n"

REPLACE_RUNNING = "12.3% done, 0 write errs, 0 uncorr. read errs\n"
REPLACE_DONE = "Started on 27.Oct 10:00:00, finished on 27.Oct 11:00:00, 0 write errs, 0 uncorr. read errs\n"
REPLACE_CANCELED = ("Started on 27.Oct 10:00:00, canceled on 27.Oct 10:30:00 at 45.0%, 0 write errs, "
                    "0 uncorr. read errs\n")
REPLACE_NEVER = "Never started\n"

FINDMNT = f"""{{
   "filesystems": [
      {{"target":"/", "source":"/dev/sda2[/@rootfs]", "fsroot":"/@rootfs", "options":"rw,noatime,compress=zstd:1,ssd,space_cache=v2,subvolid=256,subvol=/@rootfs", "uuid":"{U3}"}},
      {{"target":"/srv/tank", "source":"/dev/sdb[/data]", "fsroot":"/data", "options":"rw,noatime,compress=zstd:1,space_cache=v2,subvolid=256,subvol=/data", "uuid":"{U1.upper()}"}},
      {{"target":"/mnt/top", "source":"/dev/sdb", "fsroot":"/", "options":"ro,degraded,subvolid=5,subvol=/", "uuid":"{U1}"}},
      {{"target":"", "uuid":"x"}}
   ]
}}"""

SUBVOL_LIST = """\
ID 256 gen 1234 cgen 10 parent 5 top level 5 parent_uuid - received_uuid - uuid 1a2b3c4d-0000-4000-8000-000000000256 path data
ID 257 gen 1240 cgen 12 parent 5 top level 5 parent_uuid - received_uuid - uuid 1a2b3c4d-0000-4000-8000-000000000257 path media
ID 259 gen 1300 cgen 1290 parent 5 top level 5 parent_uuid 1a2b3c4d-0000-4000-8000-000000000256 received_uuid - uuid 5e6f-259 path .snapshots/data/manual-2026-10-01
ID 260 gen 1301 cgen 1301 parent 257 top level 257 parent_uuid - received_uuid - uuid 5e6f-260 path media/.snapshots
ID 261 gen 1400 cgen 1400 parent 5 top level 5 parent_uuid 5e6f-259 received_uuid - uuid 5e6f-261 path data-copy
not a subvolume line
"""

SUBVOL_LIST_RO = "ID 259 gen 1300 top level 5 path .snapshots/data/manual-2026-10-01\n"
SUBVOL_LIST_SNAPS = ("ID 259 gen 1300 cgen 1290 top level 5 otime 2026-10-01 14:00:00 path "
                     ".snapshots/data/manual-2026-10-01\n"
                     "ID 261 gen 1400 cgen 1400 top level 5 otime 2026-10-20 09:30:00 path data-copy\n")

QGROUP_NEW = """\
Qgroupid    Referenced    Exclusive   Path
--------    ----------    ---------   ----
0/5              16384        16384   <toplevel>
0/256     800000000000  700000000000   data
0/259     790000000000   4100000000   .snapshots/data/manual-2026-10-01
1/100           16384        16384   <0 member qgroups>
"""
QGROUP_OLD = """\
qgroupid         rfer         excl
--------         ----         ----
0/5             16384        16384
0/256      1000000000    500000000
"""

DEFAULT_TOP = "ID 5 (FS_TREE)\n"
DEFAULT_SUB = "ID 256 gen 1234 top level 5 path @rootfs\n"

SUBVOL_SHOW = """\
data
\tName: \t\t\tdata
\tUUID: \t\t\t1a2b3c4d-0000-4000-8000-000000000256
\tParent UUID: \t\t-
\tCreation time: \t\t2025-09-22 15:05:00 -0400
\tSubvolume ID: \t\t256
"""

FI_DU = "     Total   Exclusive  Set shared  Filename\n1000000000   100000000   900000000  /srv/x\n"

LSBLK = f"""{{
   "blockdevices": [
      {{"name":"sda", "kname":"sda", "path":"/dev/sda", "size":500107862016, "type":"disk", "model":"Samsung SSD 870", "serial":"S6P", "rota":false, "tran":"sata", "fstype":null,
         "children": [
            {{"name":"sda1", "kname":"sda1", "path":"/dev/sda1", "size":536870912, "type":"part", "fstype":"vfat", "mountpoint":"/boot/efi"}},
            {{"name":"sda2", "kname":"sda2", "path":"/dev/sda2", "size":499570991104, "type":"part", "fstype":"btrfs", "uuid":"{U3}", "mountpoint":"/"}}
         ]
      }},
      {{"name":"sdb", "kname":"sdb", "path":"/dev/sdb", "size":"4000787030016", "type":"disk", "model":"WDC WD40EFRX", "serial":"WD-1", "rota":"1", "fstype":"btrfs", "uuid":"{U1}", "mountpoint":"/srv/tank"}},
      {{"name":"sdc", "kname":"sdc", "path":"/dev/sdc", "size":4000787030016, "type":"disk", "model":"WDC", "serial":"WD-2", "rota":true, "fstype":"btrfs", "uuid":"{U1}", "mountpoint":null}},
      {{"name":"sdd", "kname":"sdd", "path":"/dev/sdd", "size":3000592982016, "type":"disk", "model":"T", "serial":"T-3", "rota":true, "fstype":"LVM2_member"}},
      {{"name":"sde", "kname":"sde", "path":"/dev/sde", "size":2000398934016, "type":"disk", "model":"S", "serial":"S-4", "rota":true, "fstype":"btrfs", "uuid":"deadbeef-0000-4000-8000-000000000000"}},
      {{"name":"sdf", "kname":"sdf", "path":"/dev/sdf", "size":1000204886016, "type":"disk", "model":"M", "serial":"M-5", "rota":true,
         "children": [{{"name":"sdf1", "kname":"sdf1", "path":"/dev/sdf1", "size":1000203837440, "type":"part", "fstype":"ext4", "label":"old"}}]}},
      {{"name":"sdg", "kname":"sdg", "path":"/dev/sdg", "size":1000204886016, "type":"disk", "model":"M", "serial":"M-6", "rota":true}},
      {{"name":"nvme0n1", "kname":"nvme0n1", "path":"/dev/nvme0n1", "size":1000204886016, "type":"disk", "model":"Samsung\\u001b[31m", "serial":"S5", "rota":false, "tran":"nvme"}},
      {{"name":"loop0", "kname":"loop0", "path":"/dev/loop0", "size":4096, "type":"loop"}}
   ]
}}"""

FSTAB = f"""\
# /etc/fstab
UUID={U3}  /  btrfs  subvol=/@rootfs,noatime  0  0
UUID={U1.upper()}  /srv/tank  btrfs  subvol=data,noatime,nofail  0  0
/dev/disk/by-uuid/{U1}  /srv/top  btrfs  subvolid=5  0  0
/dev/sdc  /srv/plain  btrfs  defaults  0  0
UUID=other  /mnt/x  ext4  defaults  0  2
broken
"""

DISKSTATS = """\
   8       0 sda 1000 0 200000 100 2000 0 400000 200 0 300 300
   8      16 sdb 50 0 1024 10 70 0 2048 20 0 30 30 0 0 0 0
 259       0 nvme0n1 x y z
"""

JOURNAL = """\
2026-10-27T09:01:12-04:00 host kernel: BTRFS warning (device sdf): devid 2 uuid 1234 is missing
2026-10-27T09:01:13-04:00 host kernel: BTRFS info (device sdf): allowing degraded mounts
"""
