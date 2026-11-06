# foundation and foundationd

`foundation` is the command line of the Ersa Compute Platform. `foundationd` is the management
service behind it. Together they are the single supported way to inspect and change the operating
system: boot slots, applications, compatibility packages, system extensions and settings.

```
 foundation (CLI)   installer app   recovery UI   settings app        <- clients, no policy
        \                |              |              /
         +---------------+--------------+-------------+
                     system bus: org.ersa.Foundation1                 <- the contract (one XML file)
                                  |
                         foundationd  (root, polkit-gated)            <- broker: never blocks
                                  | one worker process per job
                         foundationd-worker <kind>                    <- does the real work
                                  |
      libflatpak · systemd-logind (D-Bus) · sfdisk · bootctl · systemd-sysext · rpm
```

The rules this design follows:

- **One API, many clients.** The CLI contains no system logic. Anything it can do, a graphical
  client can do over D-Bus with the same authorization.
- **Prefer an interface to a command line.** D-Bus (logind for rebooting), a library (libflatpak
  for applications), or reading files directly (partition table, EFI variables, mountinfo, sysfs)
  come first. A command is run only where nothing else exists: `bootctl` to write the default boot
  entry, `sfdisk` to change a partition label, `systemd-sysext`/`systemd-confext` to merge,
  `rpm`/`rpm2cpio` to read packages, and `flatpak search`/`remote-info` (libflatpak has no search).
- **The broker never blocks.** It validates, authorizes with polkit, takes a lock and starts a
  worker. Anything slow runs in a separate process, so one stuck operation cannot freeze the service.
- **Honest state.** A feature that is not implemented is not in the contract. A result that was
  not verified says so (see [Verification status](#verification-status)).
- **Reversible, previewable, confirmed.** Destructive operations show a plan first. Overwriting a
  boot slot makes you type its name. The order of steps inside each operation is chosen so a failure
  at any point leaves the system bootable.

In text a person reads (command output, help, prompts, package descriptions) the product is called
**Ersa Compute Platform**. `ECP` is only for code: variables, labels, `IMAGE_ID=ecp`.

## Contents

1. [A tour of the command line](#a-tour-of-the-command-line)
2. [How a request travels](#how-a-request-travels)
3. [Boot slots](#boot-slots)
4. [Compatibility RPMs](#compatibility-rpms)
5. [Applications](#applications)
6. [Repository map](#repository-map)
7. [Running it on your machine](#running-it-on-your-machine)
8. [Testing](#testing)
9. [Adding a feature](#adding-a-feature)
10. [Packaging and the image](#packaging-and-the-image)
11. [API reference](#api-reference)
12. [Verification status](#verification-status)
13. [Troubleshooting](#troubleshooting)

## A tour of the command line

Run `foundation COMMAND --help` for any command. Everything that prints data has `--json`; anything
that changes the system asks first (`--yes` skips the question) and can be started without waiting
(`--no-wait`).

```
foundation status                         the system, boot slot, storage and services
foundation slot status                    which slot you are on, the boot assessment, every slot
foundation slot boot SLOT [--once]        choose the slot to boot (once, or from now on)
foundation slot copy --to SLOT [--from SLOT] [--force]
                                          copy a slot (default: the running one) onto another
foundation deploy list                      disks and whether each can be installed to
foundation deploy install DISK [--dry-run]  erase a disk and install this system on it (typed confirmation)
foundation slot release SLOT              mark a standby slot empty so updates can use it
foundation reboot [--firmware] [--force]  restart (into the firmware setup with --firmware)
foundation apps list|search|info|install|remove|update|setup
foundation compat inspect|install|remove|list|revalidate
foundation ext list|merge|unmerge|refresh [--kind sysext|confext]
foundation config list|get|describe|set|reset
foundation job list|show|cancel|logs
foundation base [FILTER]                  packages in the read-only base image
```

A copy, as it looks on a terminal (illustrative; the device names depend on the machine):

```
$ foundation slot copy --to empty2

Copy slot 0.2.0 to empty2
-------------------------
  Source  0.2.0  (/dev/nvme0n1p4, 8 GiB)
  Target  empty2  (/dev/nvme0n1p5, 8 GiB)
  Result  a new slot named 0.2.0-copy with its own kernel image

Slot empty2 is empty; it will become 0.2.0-copy.
Copy slot 0.2.0 over empty2? [y/N] y
› Copying the root filesystem
  ██████████████░░░░░░░░░░░░░░  51%
```

Ctrl-C while following a job asks the service to cancel it. Before a job's point of no return that
stops it cleanly; after it, the command says so and the job finishes in the background
(`foundation job show N`). Errors that need an administrator say how to get one; on a text-only
login with no polkit agent, run the command with `sudo`.

## How a request travels

Take `foundation slot copy --to empty2 --yes`:

1. **CLI** reads `Boot.GetSlots`, checks the arguments, shows the plan, asks, then calls
   `Boot.CopySlot("", "empty2", false)`. Before the call it subscribes to *all* job signals, so a
   fast job's first steps are not lost.
2. **Broker** (`foundationd/service.py`) receives the call. The method table is built from
   `foundationd/api/*.py`; the contract check at start-up refuses to run if any method in
   `data/dbus/org.ersa.Foundation1.xml` has no handler or vice versa.
3. **Precheck**: cheap validation and lock availability (`Busy` if another job holds `slots`). A bad
   request fails here, so nobody is asked for a password for a call that cannot succeed.
4. **Polkit** is asked asynchronously (a prompt can wait for a person; the broker keeps serving).
   The caller is identified by its unique bus name, not a pid. If polkit cannot be reached the
   answer is *no*.
5. **Job**: the broker creates `/org/ersa/Foundation1/Job/N`, takes the locks, starts
   `foundationd-worker boot.copy`, writes the request as one JSON line to its stdin, and replies
   with the job's path.
6. **Worker** (`workerlib.py`, `workers/boot.py`, `backends/slots.py`) does the work and writes JSON
   events to stdout: `step`, `progress`, `log`, `uncancellable`, and finally `result` or `error`.
7. **Broker** turns events into D-Bus: properties (`Step`, `Progress`, ...) and signals
   (`StepChanged`, `ProgressChanged`, `Message`, `Finished`). When the worker exits it releases the
   locks and writes a record to `/var/lib/foundation/jobs/` (the last 50 are kept and survive a
   restart; a job that was running when the daemon died is reported as interrupted).
8. **CLI** receives the signals, draws the progress bar, then prints the outcome.

Things worth knowing:

- **Queries** (`List*`, `Get*`, `Inspect`, `Search`) use the same workers but have no job object, no
  locks and a 60-second timeout; the result goes straight back to the caller.
- **Locks** are named: `slots`, `extensions` (shared by Compat and Extensions), `flatpak`. A
  conflicting request fails at once with `Busy`; nothing queues silently.
- **Cancellation** is SIGTERM to the worker. After `ctx.commit()` the worker ignores it and the job
  reports `Cancellable = false`. Libflatpak calls cannot be interrupted by a Python signal handler,
  so that worker cancels through a `Gio.Cancellable` instead.
- **Files are passed as file descriptors** (`Compat.Inspect(h)`), never as paths: the root service
  never opens a path chosen by an unprivileged caller. The broker copies the descriptor into a
  private staging directory (without blocking), hands the copy to the worker, and deletes it when
  the work ends.
- **Variants**: results are plain JSON converted by `variants.py` (dict -> `a{sv}`, list of strings
  -> `as`, list of dicts -> `aa{sv}`). D-Bus has no null, so absent facts are omitted: clients must
  treat every key as optional.
- **Idle exit**: the service exits after 60 seconds with no jobs, queries or pending
  authorizations, and D-Bus activation starts it again.

## Boot slots

A **slot** is a root-type GPT partition on the disk that holds the running root. Its label is
`ecp-root_<name>` (or `_empty`) and the unified kernel image (UKI)
`/EFI/Linux/ecp_<name>[+tries].efi` on the ESP boots it: the UKI's command line says
`root=PARTLABEL=ecp-root_<name>`. Everything is read directly from the partition table, the ESP's
files and the loader's EFI variables (`backends/slots.py`, `gpt.py`, `efi.py`).

**Naming and why a copy gets a new name.** The update tool expects one version per slot, and
`root=PARTLABEL=` must match exactly one partition. Copying a slot onto another would give two
partitions the same label, which makes boot ambiguous and leaves updates with no free slot. So a
copy becomes a slot of its own: the label is `ecp-root_<source>-copy`, and the copy gets its own
UKI, a byte-for-byte copy of the source's with only the `root=PARTLABEL=` token changed in the
`.cmdline` section (`backends/pe.py`). That edit is refused, and nothing is touched, if the image is
signed, if the token is not there exactly once, or if the new text does not fit the section.

**Order of a copy** (each step is chosen so a failure leaves a bootable system):

| Step | What happens | If it fails here |
| --- | --- | --- |
| prepare | checks, reads the source's UKI, produces the patched UKI in memory, measures the data | nothing was changed; cancellable |
| *(commit)* | point of no return | |
| invalidate | removes the target's old UKI, relabels it `_empty` | target is empty and unbootable |
| copy | copies the filesystem (only the EROFS image's real length), fsync | target is empty and unbootable |
| verify | re-reads both and compares SHA-256 | job fails; target stays empty |
| label | relabels the target `ecp-root_<source>-copy` (checked in the table on disk) | label exists, no UKI: harmless |
| image | writes the UKI under a temporary name and renames it into place | as above |

Refused up front: the running slot as a target; a target that is mounted; a populated target without
`--force`; a source with no UKI; a target smaller than the data; a name clash with another slot.
**Release** relabels a standby slot `_empty` and removes its UKI, so updates can use it again; it
will not release the running slot or the only slot that can boot.

**Choosing what boots.** `slot boot SLOT` calls `bootctl set-default` (or `set-oneshot` with
`--once`) with the entry id *without* its boot counter (`ecp_0.2.0.efi`), so the choice survives
`systemd-bless-boot` renaming `ecp_0.2.0+3.efi` to `ecp_0.2.0.efi`. The job's result includes the
value the loader variable ended up with. A slot just copied is tried a few times (the `+3`
counter) and boot falls back to the previous entry if it does not start.

## Repositories and bundles

`foundation repo` and `foundation compat install PACKAGE...` give the immutable image what a package manager
would, without making the base writable: every package becomes part of a **bundle**, and a bundle is **one system
extension** overlaid on `/usr` and `/opt`.

```
foundation repo list                          # the repositories, whether each is on, how fresh its lists are
foundation repo refresh                       # download the signed package lists
foundation repo search vlc                    # find packages in the enabled repositories
foundation repo info ffmpeg                   # the newest version in each repository

foundation compat install vlc gimp --into media   # packages (and what they need) into one bundle
foundation compat install @codecs                 # a ready-made bundle from the catalog
foundation compat remove media gimp               # take a package (and what only it needed) out
foundation compat remove media                    # or remove the whole bundle
foundation compat update                          # newest packages, for every bundle
foundation compat bundles | catalog | list
```

**How it works.** The repositories are described in zypp's `.repo` format (`/usr/lib/foundation/repos.d`, local
overrides in `/etc/foundation/repos.d`). foundationd reads their metadata with **libsolv** (the solver and metadata
reader libzypp and dnf use) through its Python bindings; there is no `zypper` and no libzypp on the system. To add
packages to a bundle it re-solves the whole bundle against the installed base plus the other bundles, downloads
what is missing, builds the new extension beside the old one, swaps it in and merges it. If the merge fails the
previous bundle comes back. Downloaded RPMs stay in `/var/lib/foundation/bundles/<name>.rpms`, so a rebuild fetches
only what is new. Solving the bundle as a whole means conflicts show up together and shared dependencies are
stored once; the other bundles count as installed, so nothing is duplicated, and a bundle that others use
libraries from cannot be removed.

**What is trusted.** Each repository names its signing key by file (`gpgkey=file:///usr/share/foundation/keys/...`);
keys are never fetched from the network. `repomd.xml` must carry a valid detached signature from that key, the
package list must match the checksum the signed `repomd.xml` lists, and every RPM must match its checksum and carry
a valid signature from the repository's key (checked with `rpmkeys` against a private keyring). Only https and file
URLs are accepted, sizes are capped, and a remote repository cannot switch the checks off. Packages are unpacked by
a strict reader (no scriptlets, nothing outside `/usr` and `/opt`, no path escapes) and **every file is labelled for
SELinux for the path it will have once merged**, from the policy's own `file_contexts`: an overlay shows the
extension's directories on top of the real ones, so unlabelled ones made `/usr` unreadable to confined services
(that is what stopped an early installer image from reaching the desktop).

**Shipped repositories.** `opensuse-oss` (on) and `packman-essentials` (off). Packman is a third-party community
repository with the full FFmpeg, x264, x265 and GStreamer codec plugins that openSUSE's own build leaves out. It
stays off until something asks for it; installing `@codecs` shows what it is, says that some codecs are
patent-encumbered in some countries, and asks before turning it on. The fingerprints of the pinned keys are:
openSUSE Project Signing Key `AD48 5664 E901 B867 051A B15F 35A2 F86E 29B7 00A4`, PackMan Project
`F887 5B88 0D51 8B6B 8C53 0D13 45A1 D067 1ABD 1AFB`. They were downloaded from the projects' servers, over
https, and match across two mirrors; compare them with the projects' own pages before relying on them.

**The catalog** (`/usr/lib/foundation/catalog.d/*.json`) lists ready-made bundles: a name, a description, a notice,
the repositories it needs and its packages. A package can say `"repo": "..."` (only from that repository) and
`"when_installed": true` (only if the base has it, which replaces a library the base ships only where there is
one). `codecs` replaces the base's limited FFmpeg libraries with Packman's full builds and adds the encumbered
codec plugins. Flatpak applications bring their own codecs and do not need it.

**Tested.** The repository layer (signed metadata, tampering, wrong key, checksum mismatch, unsafe locations, size
limits, signature checks) and the bundle builder (build, add, remove, rollback, update, dependents, catalog,
labelling) have unit tests with real signatures and real small RPMs. The solver, the downloads and the signature
verification were run against the live openSUSE and Packman repositories in a Tumbleweed container, including a
base with openSUSE's limited FFmpeg installed, where the solver correctly chose Packman's replacements
(`scripts/test-bundles-container`). The real `codecs` entry built there in 87 s from 29 packages (265 files) on a base with GStreamer and openSUSE's limited FFmpeg: Packman's `libavcodec62` with the H.264 decoder, the x264/x265/de265/faac GStreamer plugins, the FFmpeg 9 family skipped (`when_installed`, the base has none). **Not tested:** a bundle merged on a booted image under enforcing SELinux
(the labelling code is tested, the result on a running system is not), and the updates of a bundle after the base
image itself has been updated (`compat revalidate` rechecks bundles like other extensions).

## Compatibility RPMs

`foundation compat install foo.rpm` runs a legacy or third-party RPM without touching the immutable
base. The payload is unpacked into `/var/lib/extensions/<name>`, an `extension-release` file is
written (`ID=` from the base's `os-release`, plus `VERSION_ID`/`SYSEXT_LEVEL` if the base has them and
`ARCHITECTURE=` for architecture-specific packages), and `systemd-sysext refresh` overlays it on
`/usr` and `/opt`.

What is checked before installing (`Compat.Inspect`, shown by `compat inspect`):

- every requirement is looked up in the base's RPM database **by name**; version constraints are
  not compared. **Rich dependencies** such as `(libcurl.so.4()(64bit) or libgtk-3.so.0()(64bit))` are
  evaluated (`and`, `or`, `if`, `unless`, `with`, `without`, nesting), and one that cannot be parsed is
  reported rather than assumed satisfied (checked against Google Chrome's real requirement lines);
- every ELF file's `DT_NEEDED` libraries must be in the linker cache or inside the package
  (`backends/elf.py` reads them without tools). Libraries are classified by what loads them: those
  a **program** in the package links against, directly or through other libraries of the package, are
  *required*; libraries nothing links against (plugins loaded on demand, such as Chrome's optional Qt
  theme shims) are *optional*: a missing one is a note, not a problem. A package with no program in it
  treats all its libraries as required;
- files that would replace files of the base are reported as conflicts.

**Problems stop an install** (unless forced): missing requirements or required libraries, conflicts, a
wrong architecture. **Notes do not**: optional libraries that are missing, files outside `/usr` and
`/opt` (these are never installed: `/bin`, `/sbin`, `/lib`, `/lib64` are mapped into `/usr`, anything
else such as `/etc/cron.daily/...` is dropped and listed), and scriptlets (which are not run).

Scriptlets are never run. The payload is read by a strict parser of our own (`backends/cpio.py`),
not by `cpio`: it rejects `..`, absolute escapes, and writes through symlinks, so a crafted RPM
cannot write outside its directory. A failed merge removes the extension again and restores the
previous overlay. After a system update, `compat revalidate` rechecks every extension and *disables*
(moves aside, never deletes) those that no longer fit; they come back when they fit again.

Limits to know about: the extension's files carry whatever SELinux labels the filesystem gives them
(not tested with the enforcing policy); kernel modules built for another kernel do not work; an
extension cannot change `/etc` (that needs a confext).

## Provisioning

`system.provisioned` is Android's `DEVICE_PROVISIONED`: false until first-boot setup finishes, then
true, one way. It is machine **state** (`/var/lib/foundation/state.json`), not a setting, and latching
also creates the marker `/var/lib/foundation/provisioned` for `ConditionPathExists=` in units.
Two ways to latch it:

- **Evidence** (`Manager.CheckProvisioning`, no authorization): the service looks for
  `~/.config/gnome-initial-setup-done` of an administrator (a `wheel` member, UID 1000 or above, a
  regular file owned by that user). The user path unit `foundation-provisioning.path` watches for that file in the
  new account's session and starts `foundation-provisioning.service` when it appears (Initial Setup
  writes it during the account's first session, so a one-time check at login would be too early); it
  is skipped once the marker exists. Nothing polls.
- **Explicit** (`Manager.CompleteProvisioning`, polkit `provision`): for a front end that is not
  GNOME Initial Setup (a custom setup app, an MDM enrolment agent). A second call fails with
  `AlreadyProvisioned`.

`foundation status` shows it. Design and the MDM/first-boot front-end discussion are in
[docs/provisioning.md](../docs/provisioning.md).

## Applications

System-wide Flatpaks, stored in `/var/lib/flatpak`. Listing, installing, removing, updating and adding
the Flathub remote use **libflatpak** through GObject introspection (`backends/flatpak.py`), which
gives structured errors and real progress; `Search` and `GetInfo` run the `flatpak` command, because
the library has no search. Applications install from the `stable` branch of Flathub for the system's
architecture. Application data under `~/.var/app` is kept when an application is removed.

## Repository map

```
foundation/
  foundation/              the command line (a client; no system logic)
    cli.py                 commands, previews, job following, error hints
    client.py              the D-Bus client: calls, jobs, signal replay, file descriptors
    ui.py                  terminal output: colour, tables, progress bar, confirmations
  foundationd/             the service
    service.py             D-Bus objects, dispatch, authorization, jobs, staging, idle exit
    jobs.py                jobs, locks, worker processes, persistence, queries
    workerlib.py           the worker protocol and runtime (shared with the broker)
    auth.py                polkit (async, fail closed) and test authorizers
    config.py schema.py    layered settings with a schema; state.py machine state
    paths.py variants.py errors.py
    api/                   one module per interface: validate, authorize, start a worker
    workers/               one module per job kind: thin wrappers around backends
    backends/              the real work, with injected paths and runners so tests can fake them
      slots.py gpt.py pe.py efi.py       boot slots
      compat.py cpio.py elf.py           compatibility packages
      extensions.py flatpak.py osinfo.py runner.py
  data/
    dbus/                  org.ersa.Foundation1.xml (THE CONTRACT), bus policy, activation file
    polkit/                the polkit actions
    systemd/               foundationd.service
    libexec/               foundationd and foundationd-worker launchers
    config/schema.d/       settings schema
  tests/                   see Testing
  bin/foundation           the launcher installed as /usr/bin/foundation
```

## Running it on your machine

You need Python 3.9+, PyGObject (`python3-gobject`), `dbus-daemon`, and for the boot tests `sfdisk`
(util-linux). Nothing below touches your real system bus, disks or boot configuration.

```sh
cd foundation
# 1. a private bus and a daemon with a stub authorizer, scratch directories, a scripted worker
eval "$(dbus-launch --sh-syntax)"
mkdir -p /tmp/fd/{conf,state}
PYTHONPATH=. python3 -m foundationd --bus session --authorizer allow --idle-timeout 0 \
    --worker-command "python3 tests/scripted_worker.py" \
    --xml-path data/dbus/org.ersa.Foundation1.xml --schema-path data/config/schema.d \
    --conf-path /tmp/fd/conf --state-path /tmp/fd/state -v &
# 2. the real CLI against it (--bus session is a hidden development option)
PYTHONPATH=. python3 -m foundation --bus session ext merge --yes
PYTHONPATH=. python3 -m foundation --bus session job list
busctl --user introspect org.ersa.Foundation1 /org/ersa/Foundation1
```

`--authorizer allow|deny` and every `--*-path` option exist only with `--bus session`; the daemon
refuses them on the system bus. Replace `--worker-command` with the default to run the real
workers, and point `--efivars-path`, `--mountinfo-path`, `--sysfs-path`, `--dev-path`, `--esp-path`
and `--root-path` at a fake system (see `tests/support.py`) to try the slot code without a disk.

## Testing

```sh
cd foundation && python3 -m unittest discover -s tests          # about a minute
python3 -m unittest tests.test_backends                         # one file
```

| File | What it proves |
| --- | --- |
| `test_foundationd_config.py` | schema, layered settings, atomic writes, machine state |
| `test_foundationd_bus.py` | the contract on a real private bus; polkit allow/deny/fail/missing against a fake polkitd; the in-flight counter; idle exit |
| `test_foundationd_jobs.py` | steps, progress and signals, failure and crash, cancellation and the point of no return, locks, queries, validation, staged descriptors, persistence, idle with a job running, reboot through a fake logind |
| `test_foundationd_boot.py` | the real boot workers through D-Bus on a real GPT image: state, selection, copy, release, refusals |
| `test_backends.py` | GPT parsing against `sfdisk` images, the PE edit (also read back with `objcopy`), the ELF reader (against `readelf`), the safe cpio, the whole slot logic including a corrupted copy |
| `test_compat.py` | a real RPM through inspect, install, merge failure rollback, remove, revalidate |
| `test_apps.py` | the libflatpak backend against a fake library module |
| `test_cli.py` | the real CLI as a process: output, `--json`, exit codes, Ctrl-C, confirmations, every slot command |
| `test_foundation.py` | terminal output, rendering helpers, the parser |

How the fakes work: `tests/support.py` builds a **real** GPT disk image with `sfdisk`, a fake
`/sys`, `mountinfo`, EFI variable directory and ESP, and a `SliceFile` that stands in for a partition
device. `tests/scripted_worker.py` replaces chosen worker kinds with scripted behaviour (sleep, crash,
commit, fail) so the job machinery is tested through the real API. `tests/fake_polkit.py`,
`fake_logind.py` and `fake_bootctl.py` stand in for those services. Tests needing a tool that is
absent (`sfdisk`, `rpm`, a built RPM) skip rather than fail.

**Never point a test at a real disk.** Nothing in the suite does.

## Adding a feature

Worked example: a hypothetical `Boot.Ping` job. Follow the same order for anything.

1. **Contract first.** Add the method to `data/dbus/org.ersa.Foundation1.xml` with a comment saying
   what it does, what it returns and which authorization it needs. Changes inside `Foundation1` are
   additive only.
2. **Polkit.** If it changes anything, add or reuse an action in `data/polkit/` and decide its
   defaults (`auth_admin_keep` normally; `auth_admin` for something destructive).
3. **API handler** in `foundationd/api/<interface>.py`: validate arguments with
   `api/validate.py`, then return `service.start_job(kind, args, locks)` for a change or
   `service.query(call, kind, args, reply_dict | reply_list)` for a read. Register it in the
   module's `register()` as `Method(handler, action, precheck)`. Start the service: if the XML and
   the handlers disagree, it refuses to start and says which methods.
4. **Worker** in `foundationd/workers/<name>.py`:

   ```python
   @worker("boot.ping")
   def ping(ctx):
       ctx.step("ping", "Pinging")          # shown to the user
       ctx.progress(0.5)                    # 0.0 to 1.0
       ctx.commit()                         # only before an irreversible step
       return {"pong": True}                # JSON; becomes the job's Result
   ```

   Raise a `FoundationError` subclass (`NotFound`, `InvalidArgument`, `Unavailable`, `Failed`, ...)
   for an expected failure; its name reaches the caller as `org.ersa.Foundation1.Error.<Name>`.
5. **Backend** in `foundationd/backends/`: the real logic, taking paths from `ctx.paths` and any
   command runner as a parameter, so a test can fake both. Order the steps so that a failure at any
   point is safe, and put the point of no return as late as possible.
6. **CLI** in `foundation/cli.py`: a handler that previews, confirms with `sure()`, and calls
   `run_job(...)`; add it to `build_parser()`. User-facing words say "Ersa Compute Platform".
7. **Tests**: the backend against a fake system, the whole path through `scripted_worker.py` or the
   real worker on a fake system, and the CLI.
8. **Packaging**: new files under existing directories are picked up by the spec's globs; a new
   directory needs a line in `packages/foundation/foundation.spec`.

## Packaging and the image

`scripts/build-foundation-package` builds two RPMs from `packages/foundation/foundation.spec`:
`foundation` (the CLI) and `foundationd` (the service); `scripts/prepare-build` runs it and mkosi
installs both from `mkosi.packages/`. The spec's `%check` runs this test suite.

`foundationd.service` is a `Type=dbus` unit with `StateDirectory=foundation` and
`ConfigurationDirectory=foundation`. It deliberately uses **no mount-namespace sandboxing**
(`ProtectSystem=`, `PrivateTmp=`, `ReadWritePaths=`, ...): workers merge system extensions (an overlay
mounted on `/usr`), write the boot slots and ESP, and install Flatpaks, and an overlay mounted
inside a private namespace would be invisible to the rest of the system. For the same reason it
does not set `NoNewPrivileges=` (Flatpak runs bubblewrap) and allows the network. Stronger
isolation is planned as one transient systemd unit per worker kind.

Runtime dependencies are in the spec: `python3-gobject`, `polkit`, `dbus-1`, `rpm`, `util-linux`
(`sfdisk`), and recommended `systemd-boot` (`bootctl`), `flatpak`, `typelib-1_0-Flatpak-1_0`.

## API reference

Full detail, with field lists, is in `data/dbus/org.ersa.Foundation1.xml`.

| Interface | Reads (no authorization) | Changes (job) | Polkit action | Lock |
| --- | --- | --- | --- | --- |
| `Manager` | `GetStatus`, `GetSystemInfo`, `ListBasePackages`, `ListJobs` | `CheckProvisioning` (records evidence; no authorization) | | |
| | | `CompleteProvisioning` (one-way latch) | `provision` | |
| `Config` | `Get`, `List`, `Describe` | `Set`, `Reset` (immediate) | `config` | |
| `Job` | properties, `Logs` | `Cancel` | `jobs` | |
| `Boot` | `GetBootState`, `GetSlots` | `SetDefaultSlot`, `SetNextBoot` | `boot` | `slots` |
| | | `CopySlot`, `ReleaseSlot` | `slots` (always asks) | `slots` |
| | | `Reboot`, `RebootToFirmware` (immediate, via logind) | `boot` | |
| `Apps` | `ListInstalled`, `Search`, `GetInfo` | `SetupRemote`, `Install`, `Remove`, `Update` | `apps` | `flatpak` |
| `Compat` | `Inspect`, `List` | `Install`, `Remove`, `Revalidate` | `compat` | `extensions` |
| `Extensions` | `List` | `Merge`, `Unmerge`, `Refresh` | `extensions` | `extensions` |

Errors are `org.ersa.Foundation1.Error.*`: `NotAuthorized`, `InvalidArgument`, `UnknownKey`,
`ReadOnly`, `Busy`, `NotFound`, `NotCancellable`, `Unavailable`, `AlreadyProvisioned`, `Failed`.

Settings (`Config`) are described by `data/config/schema.d/` and layered: image defaults, then
`/etc/foundation/foundation.conf.d/*.conf`; the API writes only `90-api.conf`. The `ota.*` keys are
stored and validated but nothing reads them yet.

## Verification status

Dated 2026-11-05. Read this before trusting any of it on hardware.

**Run and passing**

- The test suite (about 240 tests) on the development host (Python 3.14, PyGObject 3.58) **and** on
  openSUSE Tumbleweed in a container (Python 3.13.16, PyGObject 3.58.1, flatpak 1.18.4, rpm 4.20.1,
  polkit 128). The RPM build also runs it in `%check`.
- The installed RPMs on Tumbleweed, with the service on a **real system bus and a real `polkitd`**,
  driven by the real CLI: root changes a setting and `nobody` is refused with a useful hint; reads
  work for everyone; `apps list` through real libflatpak; `ext list`; `base`; `compat inspect` of an
  unrelated real package (a real ELF scan against the real linker cache: "compatible"), and of a
  package whose files are already installed (conflicts reported); a text file offered as an RPM
  is refused before any tool sees it; `compat install` runs the real inspect, unpack and install and,
  when the merge fails (the container is unprivileged), removes the extension again and leaves no
  extension, metadata or staging file behind; `reboot` without logind and the slot commands on a
  root that is not a partition fail with clear errors.
- The parts checked against independent tools: the GPT reader against `sfdisk` images, the edited
  kernel command line read back by `objcopy`, the ELF reader against `readelf`, the safe unpacker
  against `rpm -qlp`.
- The slot code on real GPT images, including a corrupted copy (the target is left empty and
  unbootable) and every refusal rule, and through D-Bus with the real workers.

**Verified on a booted image (QEMU/OVMF VM, 2026-11-05, from the user's screenshots)**

- `foundationd` starts by D-Bus activation under `dbus-broker` and `foundation slot status` reads the real
  partition table, ESP and EFI variables.
- `slot copy` made a copy; `slot boot --once` selected it with the counter-free id; the loader matched it
  (`Current Entry: ecp_0.1.0-copy.efi`, no counter, although the file was `+3`) and the copy booted,
  was blessed (`good`) and shows `blessed` in the slot table. So the edited kernel image starts and
  `root=PARTLABEL=ecp-root_<name>-copy` resolves.
- `apps setup` added the real Flathub remote through libflatpak.
- `apps install app.zen_browser.zen` **failed** with ostree's "min-free-space-size 500MB would be exceeded" at 99%: the 2 GiB `/var` was too small. `/var` is now 8 GiB; the install has not been retried.

- The btrfs data volume and the `/etc` overlay boot under enforcing SELinux once the overlay's upper
  directory is labelled `etc_t` (done by hand on the test disk; the build now does it, unverified).

**Not tested: needs a booted image on real hardware or a VM**

- D-Bus activation by systemd and `dbus-broker` (tests used `dbus-daemon`); the unit itself.
- SELinux with the enforcing policy: whether the service, its workers and the compat extensions'
  files work under it.
- `systemd-sysext` actually merging (the container could not), the overlay being visible, a compat
  program running, and `systemd-sysext` accepting an `ID`-only `extension-release` (the image's
  `os-release` has no `VERSION_ID`).
- `bootctl set-default` (persistent) with the counter-free entry id. The one-shot form is verified (below);
  the persistent form uses the same matching but has not been run.
- That `systemd-sysupdate` copes with a `-copy` label.
- `sfdisk --part-label` on a disk that has mounted partitions (the code checks the table on disk
  afterwards rather than trusting the exit status); 4096-byte-sector disks; aarch64.
- Installing, removing and updating Flatpaks against the real Flathub (only a fake library and the
  empty listing were exercised); the polkit authentication prompt in a GNOME session.

**Not implemented**: OTA updates, installing to a disk, diagnostics, a recovery entry (so no
`RebootToRecovery`), per-worker sandboxes, and anything reading the `ota.*` settings.

## Troubleshooting

| Symptom | Likely cause and what to do |
| --- | --- |
| `the Ersa Compute Platform management service (foundationd) is not available` | not installed or crashed: `systemctl status foundationd`, `journalctl -u foundationd` |
| `NotAuthorized` | an administrator is required: approve the prompt in a graphical session, or use `sudo foundation ...` on a text login (there is no polkit text agent) |
| `Busy` | another job holds the lock: `foundation job list`, wait for it or `foundation job cancel N` |
| `Unavailable` on slot commands | not booted through UEFI, or the root is not on a GPT partition (a live ISO, a container) |
| a job failed | `foundation job show N` prints the error and the job's log |
| need the raw API | `busctl introspect org.ersa.Foundation1 /org/ersa/Foundation1`, `busctl call ...` |
