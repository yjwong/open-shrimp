# Windows 11 test VMs on libvirt

Two disposable Windows 11 guests, driven entirely over SSH from a Linux host,
with a route to the interactive desktop when a test needs one. Nothing here is
specific to any one application; `BUILDING.md` covers what OpenShrimp builds and
validates on them.

| Domain | Edition | Computer name | SSH port |
| --- | --- | --- | ---: |
| `win11-spike` | Windows 11 Pro | `WIN11SPIKE` | 2299 |
| `win11-home-spike` | Windows 11 Home | `WIN11HOME` | 2298 |

The guests run under the calling user's libvirt session (`qemu:///session`).
They use KVM, Q35, UEFI Secure Boot, an emulated TPM 2.0, 8 vCPUs, 12 GiB of
RAM, and a 64 GiB qcow2 disk. QEMU user networking exposes guest SSH only on the
host loopback interface. VNC also listens only on loopback.

The Pro guest is the tool-equipped development environment. The Home guest is
the clean installation and acceptance-test environment. **Do not install build
tools globally in the Home guest.** A framework-dependent binary starts silently
on a machine that already carries the .NET SDK, so a missing-prerequisite bug
reproduces only where nothing has been installed.

## Host requirements

The verified host is Ubuntu 24.04 with libvirt 10, virt-install 4.1, and QEMU
8.2. Install the required packages:

```bash
sudo apt install \
  qemu-kvm libvirt-daemon-system libvirt-clients virtinst virt-viewer \
  ovmf swtpm xorriso
```

The user must be able to use session libvirt and `/dev/kvm`:

```bash
virsh -c qemu:///session list --all
test -w /dev/kvm
```

Nested virtualization is required to exercise Hyper-V or the Host Compute
Service inside Windows:

```bash
cat /sys/module/kvm_amd/parameters/nested
# or
cat /sys/module/kvm_intel/parameters/nested
```

The result must be `1` or `Y`.

## Asset layout

Keep each VM's mutable and secret-bearing assets in its own directory outside
the repository. The rest of this document calls it `$VM_DIR`:

```text
$VM_DIR/
  win11.iso
  unattend.iso
  vm_key
  win11.qcow2
```

`chmod 600` the ISO holding the unattend answer file, the private key, and the
disk image; the directory itself should not be world-readable.

The two guests may share the same Windows installation ISO. The build these
instructions were verified against has volume label `CCCOMA_X64FRE_EN-US_DV9`
and SHA-256:

```text
768984706b909479417b2368438909440f2967ff05c6a9195ed2667254e465e3
```

Obtain Windows media from Microsoft. A different release is acceptable, but
record its hash: Windows build changes affect installer behaviour and the set of
HCS APIs present.

## Unattended media

Each `unattend.iso` contains two files at its root:

```text
autounattend.xml
setup.ps1
```

Treat this ISO as a secret. `autounattend.xml` necessarily contains the local
test account's password in cleartext, and `setup.ps1` contains the SSH public
key. Generate a password for these disposable VMs and use it nowhere else. The
ISO is also the only record of it, so recover it later with:

```bash
7z x "$VM_DIR/unattend.iso" autounattend.xml
```

Generate a dedicated SSH key per guest:

```bash
ssh-keygen -t ed25519 -N '' -f "$VM_DIR/vm_key"
```

`autounattend.xml` must perform the following operations:

1. Set locale and UI language to `en-US`.
2. Disable Dynamic Update during installation.
3. Create a 300 MiB EFI partition, a 16 MiB MSR partition, and an NTFS
   partition using the remaining disk.
4. Select `Windows 11 Pro` or `Windows 11 Home` through `/IMAGE/NAME` metadata.
5. Create local administrator `spike` with the test-only password.
6. Enable auto-logon (see [Auto-logon expires](#auto-logon-expires) below).
7. Set the computer name to `WIN11SPIKE` or `WIN11HOME`.
8. Run `setup.ps1` from the unattended CD as the first-logon command.

The existing media also sets the standard `LabConfig` bypasses for TPM, Secure
Boot, CPU, and RAM checks. The VM supplies TPM and Secure Boot anyway; the
bypasses make the installation less sensitive to host firmware details.

The first-logon command locates the script without assuming the unattended CD's
drive letter:

```xml
<FirstLogonCommands>
  <SynchronousCommand wcm:action="add">
    <Order>1</Order>
    <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-PSDrive -PSProvider FileSystem | ForEach-Object { $p = Join-Path $_.Root 'setup.ps1'; if (Test-Path $p) { &amp; $p } }"</CommandLine>
    <Description>run spike setup from the unattended CD</Description>
  </SynchronousCommand>
</FirstLogonCommands>
```

Use this shape for `setup.ps1`, replacing the public-key placeholder with the
contents of `vm_key.pub`:

```powershell
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Path C:\spike -Force | Out-Null
Start-Transcript -Path C:\spike\setup-transcript.txt -Append
Add-Content C:\spike\status.txt 'setup-start'

powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /hibernate off

for ($i = 0; $i -lt 20; $i++) {
    $state = (Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0).State
    if ($state -eq 'Installed') { break }
    try { Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 } catch {}
    Start-Sleep -Seconds 15
}
Add-Content C:\spike\status.txt ("openssh-capability: " + (Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0).State)

Set-Service sshd -StartupType Automatic
New-Item -ItemType Directory -Path C:\ProgramData\ssh -Force | Out-Null
Set-Content C:\ProgramData\ssh\administrators_authorized_keys 'SSH_PUBLIC_KEY'
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'
Start-Service sshd
New-NetFirewallRule -Name spike-sshd-in -DisplayName 'OpenSSH inbound (spike)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction SilentlyContinue
Add-Content C:\spike\status.txt 'sshd-up'

dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
Add-Content C:\spike\status.txt ("vmp-exit: " + $LASTEXITCODE)
dism /online /enable-feature /featurename:Microsoft-Hyper-V-All /all /norestart
Add-Content C:\spike\status.txt ("hyperv-exit: " + $LASTEXITCODE)

[System.Environment]::SetEnvironmentVariable('WSL_UTF8', '1', 'Machine')
Add-Content C:\spike\status.txt 'ready'
Copy-Item C:\spike\status.txt C:\spike\ready.txt
Stop-Transcript
shutdown /r /t 10
```

The Hyper-V enable attempt is intentionally retained on Home. That edition does
not provide `Microsoft-Hyper-V-All`; its failure is useful evidence for
feature-gating tests.

Build the ISO from a directory containing the two files:

```bash
xorriso -as mkisofs \
  -V UNATTEND \
  -o unattend.iso \
  autounattend.xml setup.ps1
chmod 600 unattend.iso
```

## Create a VM

Set the parameters for the desired guest:

```bash
VM=win11-spike
VM_DIR=/path/to/win11-spike
SSH_PORT=2299
```

For Home use `win11-home-spike`, its matching directory, and port `2298`.

Create a fresh disk:

```bash
qemu-img create -f qcow2 -o lazy_refcounts=on "$VM_DIR/win11.qcow2" 64G
chmod 600 "$VM_DIR/win11.qcow2"
```

Create and start the domain:

```bash
virt-install \
  --connect qemu:///session \
  --name "$VM" \
  --memory 12288 \
  --vcpus 8,sockets=1,cores=8,threads=1 \
  --cpu host-passthrough \
  --machine q35 \
  --os-variant win11 \
  --boot uefi \
  --features smm=on,hyperv_relaxed=on,hyperv_vapic=on,hyperv_spinlocks=on \
  --disk path="$VM_DIR/win11.qcow2",device=disk,bus=sata,format=qcow2,boot.order=1 \
  --cdrom "$VM_DIR/win11.iso" \
  --disk path="$VM_DIR/unattend.iso",device=cdrom,bus=sata \
  --network none \
  --tpm emulator,model=tpm-crb,version=2.0 \
  --graphics vnc,listen=127.0.0.1 \
  --video vga \
  --qemu-commandline="-netdev user,id=n0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 -device e1000e,netdev=n0,bus=pcie.0,addr=0x10" \
  --noautoconsole \
  --wait 0
```

The generated domain uses libvirt-managed UEFI variables and swtpm state. The
user-mode network is deliberate: it works with session libvirt without a
privileged bridge and publishes only the explicit loopback SSH forward.

**State the socket topology explicitly.** Without `sockets=1,cores=8`, QEMU
presents 8 vCPUs as 8 single-core sockets, and client editions of Windows use
only 2 of them — they cap at 2 sockets. Every measurement taken against "the
processor count" is then silently halved. Check it from inside the guest, where
the truth is, not from the domain XML's `<vcpu>`:

```powershell
Get-CimInstance Win32_ComputerSystem |
  Select-Object NumberOfProcessors, NumberOfLogicalProcessors
```

On an existing guest, `virsh edit` the domain and add the topology element:

```xml
<cpu mode='host-passthrough'>
  <topology sockets='1' cores='8' threads='1'/>
</cpu>
```

Windows installation media displays a short "Press any key to boot from CD"
prompt. Send Enter several times immediately after the first start:

```bash
for delay in 1 2 3 5 8; do
  sleep "$delay"
  virsh -c qemu:///session send-key "$VM" KEY_ENTER || true
done
```

With the nonblocking `--wait 0` invocation, Windows Setup's first reboot may
leave the domain shut off while libvirt transitions from its installer XML to
the final persistent definition. Wait for that stop, then continue installation
from disk:

```bash
while [ "$(virsh -c qemu:///session domstate "$VM")" != 'shut off' ]; do
  sleep 5
done
virsh -c qemu:///session start "$VM"
```

Follow installation through the local VNC console when needed:

```bash
virt-viewer -c qemu:///session "$VM"
```

Do not press a key on later reboots. The hard disk has the first boot order, and
Windows Setup will continue from it.

## Wait for provisioning

The install and the Windows Update-backed OpenSSH capability can take tens of
minutes. Poll for an SSH round-trip rather than using a fixed sleep:

```bash
while ! ssh \
  -i "$VM_DIR/vm_key" \
  -p "$SSH_PORT" \
  -o BatchMode=yes \
  -o ConnectTimeout=5 \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  spike@127.0.0.1 \
  'powershell -NoProfile -Command "Test-Path C:\spike\ready.txt"' \
  2>/dev/null | grep -q True
do
  sleep 15
done
```

**A TCP probe against the forwarded port proves nothing.** QEMU's `hostfwd`
socket listens from the moment the domain starts, so `</dev/tcp/127.0.0.1/2299>`
succeeds on the first try against a guest still sitting at the firmware screen,
and the connection then dies at banner exchange. Only a completed `ssh … echo`
means the guest is up, which is roughly a minute after a start on an already
installed guest.

When SSH stays unavailable, `virsh -c qemu:///session screenshot "$VM" out.ppm`
separates "still booting" from "booted, no sshd". If sshd is genuinely absent,
`net start sshd` from an elevated prompt in the guest fixes it.

## Validation

Confirm the installed edition, build, SSH service, VMP state, and setup
breadcrumb:

```bash
ssh -i "$VM_DIR/vm_key" -p "$SSH_PORT" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  spike@127.0.0.1 \
  'powershell -NoProfile -Command "Get-ComputerInfo | Select-Object WindowsProductName,WindowsVersion,OsBuildNumber; Get-Service sshd; Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform; Get-Content C:\spike\status.txt"'
```

`Get-ComputerInfo.WindowsProductName` may report `Windows 10` on Windows 11. Use
the build number and the selected installation image as the authoritative
identity.

Confirm that VNC is loopback-only:

```bash
virsh -c qemu:///session vncdisplay "$VM"
virsh -c qemu:///session dumpxml "$VM" | grep -A2 '<graphics'
```

Shut down and verify the qcow2 image is clean:

```bash
virsh -c qemu:///session shutdown "$VM"
while [ "$(virsh -c qemu:///session domstate "$VM")" != 'shut off' ]; do
  sleep 2
done
qemu-img info "$VM_DIR/win11.qcow2"
```

## Routine access

```bash
virsh -c qemu:///session start win11-spike
ssh -i "$VM_DIR/vm_key" -p 2299 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null spike@127.0.0.1
virt-viewer -c qemu:///session win11-spike
virsh -c qemu:///session shutdown win11-spike
```

The Home guest uses the same commands with `win11-home-spike`, its own key, and
SSH port `2298`. `shutdown /s /t 0` over SSH stops a guest in about a minute.
Both are configured for 12 GiB of RAM, so check host memory before booting them
together.

Neither domain has a `<filesystem>` device, so a bare `virsh start` is safe;
guests that use virtiofs need their `virtiofsd` started alongside and cannot be
started this way.

### Quoting and transport

Send PowerShell as a file, not as an argument:

```bash
scp -i "$VM_DIR/vm_key" -P "$SSH_PORT" script.ps1 spike@127.0.0.1:C:/spike/
ssh … 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\spike\script.ps1'
```

Inline `powershell -Command "…"` and `cmd /c` over SSH lose their quoting.
Registry keys containing spaces (`Windows NT`) fail with `ERROR: Invalid syntax`,
and whole blocks vanish without printing anything. `powershell -Command -` over
stdin truncates at the first multi-line block. When a file is inconvenient,
encode instead:

```bash
ssh … "powershell -NoProfile -EncodedCommand $(iconv -t utf-16le < script.ps1 | base64 -w0)"
```

`timeout /t` inside a `.bat` fails over SSH: there is no console for it to wait
on.

## Elevation

An SSH session on either guest runs with a **full, unfiltered admin token** —
`IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)` is true, and
HKLM is writable. So does a scheduled task with `<LogonType>S4U</LogonType>`. UAC
filtering applies to interactive logons only.

The one headless way to get a genuinely filtered token is a scheduled task with
`<LogonType>InteractiveToken</LogonType>` and `<RunLevel>LeastPrivilege</RunLevel>`,
which requires a logged-on session. Without one the task sits at
`267011 SCHED_S_TASK_HAS_NOT_RUN` having run nothing at all — a timeout that
looks like a measured failure but measured nothing. Guard any desktop run with an
explorer-process check and abort loudly.

Verify which token a step actually got:

```powershell
whoami /groups | Select-String 'Mandatory Label', 'BUILTIN\\Administrators'
```

A filtered token shows `Mandatory Label\Medium Mandatory Level` and
`BUILTIN\Administrators … Group used for deny only`.

Elevation changes results silently. A scheduled task registered by an unfiltered
token is owned by `BUILTIN\Administrators` and grants the registering user `Read`
only, so an application running as that user can never delete it; the same task
registered by a filtered token is owned by the user, who inherits `FullControl`
from `CREATOR OWNER`. An "Access is denied" that exists only because the test
harness was elevated is expensive to chase.

Poll a runner task until its `Status` leaves `Running` rather than sleeping a
fixed few seconds.

## The interactive desktop

An application that creates a window cannot be launched over SSH. WinUI's
`Microsoft.UI.Xaml.Application.Start` faults on a window station with no desktop
attached and the process exits **0xC000027B** (`STATUS_STOWED_EXCEPTION`) after a
few seconds, before any application code and therefore before any log file is
written. That silence reads exactly like a bug in whatever changed last; a
control build of the unmodified code exits identically.

### Auto-logon expires

The unattend sets `<LogonCount>10</LogonCount>`, so winlogon decrements it and
clears `AutoAdminLogon`. One reboot brings the desktop back, the next leaves a
lock screen. Re-arm unconditionally rather than checking first:

```powershell
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $k AutoAdminLogon '1'
Set-ItemProperty $k DefaultUserName 'spike'
Set-ItemProperty $k DefaultPassword '<password from autounattend.xml>'
Remove-ItemProperty $k AutoLogonCount -ErrorAction SilentlyContinue
```

Auto-logon fires at boot only. After a sign-out, use `virsh reboot`, never
`virsh start` on an already-running domain.

### Standing up a throwaway desktop

Use a separate account so the existing one is left alone. The cycle costs **two
reboots**, not one.

1. Create a local admin, then set `AutoAdminLogon`, `DefaultUserName`,
   `DefaultPassword` and `DefaultDomainName` under the Winlogon key above, and
   reboot.

   The first reboot exists to create the profile. `C:\Users\<new>\NTUSER.DAT`
   does not exist until the account has logged on once, so a `reg load` of it
   during staging fails with "The system was unable to find the specified
   registry key or value" — and because `reg` is a native command, that failure
   does *not* trip `$ErrorActionPreference = 'Stop'`. The rest of the script
   succeeds and the application silently never launches.

2. Copy the application somewhere the new account can read, then write a **Run
   key** entry into the live hive at
   `Registry::HKEY_USERS\<sid>\SOFTWARE\Microsoft\Windows\CurrentVersion\Run`.
   No `reg load` is needed while the user is signed in. Reboot again.

   Use the Run key, not `schtasks /IT`: the latter registers cleanly and then
   fails `/run` with `ERROR: Element not found`.

3. Poll for the process. A Run-key application appears roughly **35 seconds
   after sshd starts answering**. Sampling once at 40s and concluding "it never
   launched" costs a whole reboot cycle chasing a launch bug that is not there.
   `Get-Process` finding nothing *plus* an Application event log with no
   `.NET Runtime` or `Application Error` entry means not started yet, not
   crashed.

4. Set light or dark mode under the account's
   `…\CurrentVersion\Themes\Personalize` and let the **reboot** apply it.
   Restarting explorer works too but leaves the Start menu covering the
   screenshot.

A Run-key script can also drive the application. UI Automation works from that
session: `UIAutomationClient`, matching on `AutomationIdProperty`, which is what
`x:Name` becomes. A WinUI `PasswordBox` refuses `ValuePattern.SetValue`, so
`SetFocus()` then `SendKeys` for those. Have the script poll for a file if it
needs to be fed something mid-run without another reboot, and log every step to
disk — the log is the only way to tell "never launched" from "launched and could
not find a control".

### Screenshots

Capture from the host:

```bash
virsh -c qemu:///session screenshot win11-spike out.ppm
```

Despite the extension it writes **PNG**, 1280x800. Session 1's windows are
invisible to an SSH session's `EnumWindows`, so this is the only way to see them.

### Mica and acrylic need one registry value

```text
HKLM\SOFTWARE\Microsoft\Windows\Dwm   ForceEffectMode = 2   (REG_DWORD)
```

Then restart `dwm.exe` (it respawns) or reboot. Without it, libvirt's
`<model type='vga'>` means the guest loads `basicdisplay.sys` — WDDM 1.3,
display-only — Windows marks the machine not effects-capable, and every backdrop
including Windows Settings' own drops to flat `#202020` dark or `#F3F3F3` light.
DWM itself is fine and D3D works through WARP at feature level 12_1; what blocks
the effect is the classification, not the renderer.

Three things that do not work: the same value under HKCU, the subkey spelled
`DWM` rather than `Dwm`, and `VisualFXSetting=1`. Already-open windows do not
pick the change up, so restart the application after DWM.

To tell "this window's backdrop is broken" from "effects are off machine-wide",
screenshot Windows Settings alongside and compare each window's tint drift
against the wallpaper behind it. Flat, but matching Settings, is a pass.

### Teardown

Two phases, because a profile cannot be deleted while its account is signed in.
Phase 1 clears the Winlogon values, `ForceEffectMode`, and the staged
directories, then reboots to an empty console. Phase 2 runs `Remove-LocalUser`
plus `Remove-CimInstance` on the matching `Win32_UserProfile` — a plain `rmdir`
leaves the profile registration behind. Verify with `query user`, which should
report no user.

## Shutdown and sign-out

Measured on these guests, against both a real application and a standalone probe:

- **A locked session refuses an unforced shutdown** with `1271`
  (`ERROR_MACHINE_LOCKED`), which a scheduled task swallows into a Last Result
  nobody reads. `/f` gets past it and is useless whenever the point is to observe
  how applications are asked to close. Disabling the screensaver is not enough;
  a freshly booted session still returned 1271 on one of the guests.
- **Shutdown and restart honour a `WM_QUERYENDSESSION` veto.** The machine holds
  on "Closing 1 app and shutting down" with the `ShutdownBlockReasonCreate`
  string rendered under the window title, for 150s and beyond, and proceeds as
  soon as the blocking process exits. Releasing the block resumes the shutdown
  even after that screen is up: a release at 15s powered the machine off about
  10s later.
- **But the hold expires into a cancellation.** At exactly 60 seconds with no
  user input, Windows sends `WM_ENDSESSION(FALSE)` and abandons the shutdown —
  the machine stays on. Anything draining behind a block must finish well under
  60s, or the user's shutdown silently becomes a machine that never turned off.
- **Sign-out ignores the veto completely.** `WM_ENDSESSION(TRUE)` follows within
  25–360ms and the screen goes to "Signing out" with no reason shown. Window
  visibility makes no difference, tested both ways.
- **A windowless process is closed before any windowed application is asked.**
  Holding the session open protects nothing by itself. `SetProcessShutdownParameters`
  reorders it: applications default to `0x280`, and a lower level goes later.

A hidden top-level window does receive `WM_QUERYENDSESSION`; visibility is not
required for delivery. `EnumWindows` is the honest way to check that a process
owns one. `FindWindow` called from PowerShell reports a false negative, because
`$null` for a string argument marshals as `""` rather than `NULL`.

## Disk pressure

The 64 GiB disk fills. A .NET publish that runs with under 1 GB free fails deep
in `Microsoft.NET.Publish.targets` with `MSB3021: There is not enough space on
the disk`, **after a successful compile**, which reads like a build error and is
not one. Clear stale staging directories under `C:\Users\spike` and `%TEMP%`
before a build; that has recovered 4.4 GB in one pass.

## Replace an existing guest

This operation permanently deletes the installed system disk. Preserve
`win11.iso`, `unattend.iso`, and `vm_key`.

```bash
VM=win11-spike
VM_DIR=/path/to/win11-spike

test "$(virsh -c qemu:///session domstate "$VM")" = 'shut off'
virsh -c qemu:///session undefine "$VM" --nvram --tpm
rm -- "$VM_DIR/win11.qcow2"
```

Then follow **Create a VM** and **Validation**. Never remove both old disks until
the installation ISO, both unattended ISOs, and both keys have been checked
readable.
