# Building and validating the Windows app

## Why a Windows guest is required

The WinUI tray's project imports the `AppxPackage` targets, which ship only with
the Visual Studio toolset. The build must therefore run `MSBuild.exe` from
BuildTools; `dotnet publish` fails in `MrtCore.PriGen.targets`, because the .NET
SDK's own MSBuild does not carry those targets. No Linux host can produce this
artifact, which is why the VM is the only route that does not go through CI.

## What each guest carries

Both guests are built by `TESTING.md`, which also covers reaching them over ssh.

**`win11-spike` (Pro)** is the build machine: .NET SDK 9, Visual Studio 2022
BuildTools at `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`
with the `MSBuild\Microsoft\VisualStudio\v17.0\AppxPackage` targets present, WiX
7 as a global dotnet tool with the UI extension added, git, and `uv` from WinGet.
The toolset major has to match what `OpenShrimp.wxs` is authored for and what
the release job installs: WiX 5 cannot build it, because the `Files` harvest
element the tray's component group uses arrived in 6.

It is also where the HCS sandbox is normally exercised, since the repo checkout
and the staged kernel and rootfs live there.

**`win11-home-spike` (Home)** carries no .NET, no Visual Studio, and no SDK. It
is where MSI installation, missing prerequisites, and unelevated behaviour are
tested. Installing a toolchain there destroys the only environment in which a
framework-dependent binary fails the way a user's machine would.

Home runs HCS. `setup.ps1` logs a `hyperv-exit` failure on that edition because
it lacks the `Microsoft-Hyper-V-All` role, and that is not an HCS blocker: what
the backend needs is `VirtualMachinePlatform`, the same feature WSL2 runs on.
`hcs_prereq.py` bears this out — none of its checks looks for the Hyper-V role.

That feature is **not** enabled on `win11-home-spike`: the guest was
provisioned from media predating the feature-enable step, so `vmcompute.exe`
is absent there and every HCS call fails to reach a service. The `HCS platform`
check is what reports it, and the wizard's setup step is what turns it on.

## Build the tray and the MSI

Tar `VERSION` and `windows/` from the repo, `scp` the archive over, unpack, then:

```powershell
$BT = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools'
& "$BT\MSBuild\Current\Bin\MSBuild.exe" windows\src\OpenShrimp.Tray\OpenShrimp.Tray.csproj `
  -t:Publish -restore -p:Configuration=Release -p:Platform=x64 `
  -p:RuntimeIdentifier=win-x64 -p:SelfContained=true -p:WindowsAppSDKSelfContained=true `
  -p:VersionPrefix=<version> -p:PublishDir=<abs>\publish\

dotnet publish windows\src\OpenShrimp.Elevate\OpenShrimp.Elevate.csproj `
  -c Release -p:VersionPrefix=<version> -o <abs>\publish-elevate

wix build windows\packaging\OpenShrimp.wxs -arch x64 -d ProductVersion="<version>.0" `
  -d TrayPublishDir=<abs>\publish -d CoreExe=<abs>\core.exe `
  -d ElevateExe=<abs>\publish-elevate\OpenShrimp.Elevate.exe -o <out>.msi
```

`OpenShrimp.Elevate.exe` is the elevated setup helper the wizard runs to join
the account to `Hyper-V Administrators`, turn on `VirtualMachinePlatform` and
install WSL. It goes to its own
directory because it is self-contained and single-file: published into the
tray's, it would be a second copy of the runtime assemblies under the same
names. Plain `dotnet publish` builds it — it has no Windows App SDK targets to
resolve, so it needs neither the VS toolset nor a `-p:Platform`.

Check free space on `C:` first. Under 1 GB, the publish fails with `MSB3021`
after a successful compile, which reads like a build error and is not one; see
the disk-pressure note in `TESTING.md`.

The core executable the MSI embeds is a separate CI artifact. Download the
released one rather than building PyApp in the guest:

```bash
curl -L -o core.exe \
  https://github.com/yjwong/open-shrimp/releases/download/v<version>/openshrimp-windows-x86_64.exe
```

### Always bump the version for a test build

`OpenShrimp.wxs` declares `<MajorUpgrade>` without `AllowSameVersionUpgrades`, so
an MSI stamped at the version already installed refuses to install. A test
artifact built at the current `VERSION` is unusable on any machine that already
has it.

## Running the tray

`OpenShrimp.Tray.exe` needs the interactive desktop. Launched over SSH it exits
`0xC000027B` with no `tray.log` at all, because `Application.Start` faults before
`App.OnLaunched` and therefore before `TrayLog` is ever reached. Stand up a
desktop with the Run-key recipe in `TESTING.md` and launch it from there.

Two flags are the exception. `Program.Main` handles `--uninstall` and `--stop`
before `Application.Start`, precisely so an MSI custom action need not have a
desktop. Both paths are testable over plain SSH and exit 0.

## What to validate where

| Subject | Guest | Notes |
| --- | --- | --- |
| Tray build, MSI build | Pro | the only machine with the VS toolset |
| The setup helper's three fixes | either | over plain SSH: that token is already elevated, so they run with no prompt |
| The consent prompt and the restart | either | needs a desktop and a filtered token; see `TESTING.md` |
| Tray UI, Mica and theming | Pro | needs the throwaway desktop and `ForceEffectMode` |
| HCS sandbox backend | Pro | the only guest with `VirtualMachinePlatform` on |
| MSI install, upgrade, uninstall | Home | prerequisites are only missing here |
| Unelevated behaviour | Home | `InteractiveToken` + `LeastPrivilege` needs a signed-in session |
| Session-end and shutdown veto | either | see the measurements in `TESTING.md` |

## Core process shutdown

The core is served by `python.exe`, not by a process named `openshrimp`, so
`Stop-Process -Name openshrimp` misses it entirely and the next tray adopts the
survivor. PyApp's launcher is a different process from the venv interpreter that
holds the sandbox, so deferring the interpreter's shutdown makes the launcher
exit first, mid-drain; anything waiting on the spawned handle has to use the
control channel instead.

The core calls `SetProcessShutdownParameters(0x100, 0)` so it is closed after the
tray, which stays at the default `0x280`. Without that ordering the tray's drain
reaches a core that has already exited and writes into a broken pipe.
