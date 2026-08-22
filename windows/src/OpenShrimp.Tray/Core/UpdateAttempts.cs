using System.Text.Json;
using System.Text.Json.Serialization;

namespace OpenShrimp.Tray.Core;

/// <summary>
/// The version last handed to the installer, and how often handing it over has
/// come to nothing.
/// </summary>
/// <remarks>
/// <c>Failures</c> counts against <c>Version</c> alone: a feed that moves on
/// starts from zero, because a package that will not install says nothing about
/// the next one. <c>Since</c> is when the failure was noticed rather than when
/// the install was started, so the wait runs from the last thing that went
/// wrong.
/// </remarks>
internal sealed record UpdateAttempt(
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("failures")] int Failures,
    [property: JsonPropertyName("since")] DateTimeOffset Since,
    // True between the handoff and the next launch: this tray is gone by then
    // and msiexec's verdict is not in yet.
    [property: JsonPropertyName("handed_off")] bool HandedOff);

/// <summary>
/// What the tray remembers about installing, across its own death.
///
/// An install ends this process. The core is stopped, the tray exits, and a
/// script NetSparkle generates runs msiexec over the executable and starts the
/// tray again — ignoring msiexec's exit code, so a package that installed and
/// one that was refused produce the same relaunch and the same silence. What is
/// left to read is the version that comes back. A relaunch at the version that
/// was handed over is a failed install, and nothing else is.
///
/// Without a record of the handoff the relaunched tray reads that as an
/// ordinary start, checks within seconds of the supervisor reaching Running,
/// finds the same version still offered and hands it over again — stopping and
/// respawning the core on every pass, for as long as the package keeps failing.
/// This is what makes the second attempt wait.
///
/// A file rather than the registry, so the tray still writes nothing there.
/// Every failure of this file fails toward attempting: unreadable, unwritable
/// and absent all leave the update to proceed, because a record only ever holds
/// an update back and a machine that will not update is worse than one that
/// retries too often.
/// </summary>
internal static class UpdateAttempts
{
    /// <summary>
    /// How long the same version is left alone after its first, second and
    /// every later failure. The first is short because the commonest failure is
    /// another installer holding the machine for a few minutes; the last bounds
    /// a machine that will never install this package — a policy block, a disk
    /// with no room — to one stop of the core a day.
    /// </summary>
    private static readonly TimeSpan[] Backoffs =
    {
        TimeSpan.FromHours(1),
        TimeSpan.FromHours(6),
        TimeSpan.FromHours(24),
    };

    /// <summary>
    /// Serialises the read-modify-write pairs below. Reentrant, so the public
    /// methods can hold it across their own <see cref="Read"/> and
    /// <see cref="Write"/>.
    /// </summary>
    private static readonly object Gate = new();

    /// <summary>
    /// Beside config.yaml rather than under the logs: this is state the tray
    /// acts on, not a record of what it did. Not instance-scoped either — one
    /// executable is replaced however many cores are configured against it.
    /// </summary>
    private static string FilePath =>
        Path.Combine(CorePaths.StateDirectory, "tray-updates.json");

    /// <summary>
    /// How much longer <paramref name="version"/> is to be left alone, or null
    /// to hand it over now.
    /// </summary>
    public static TimeSpan? Wait(string version)
    {
        lock (Gate)
        {
            var attempt = Read();
            if (attempt is null || attempt.Failures == 0) return null;
            if (!string.Equals(attempt.Version, version, StringComparison.Ordinal)) return null;

            // A clock that moved backwards would otherwise hold the version off
            // until it caught up, which on a machine whose clock is wrong is
            // forever.
            var elapsed = DateTimeOffset.Now - attempt.Since;
            if (elapsed < TimeSpan.Zero) return null;

            var backoff = Backoff(attempt);
            return elapsed < backoff ? backoff - elapsed : null;
        }
    }

    /// <summary>
    /// Record that the installer is about to be handed
    /// <paramref name="version"/>. Written before the tray exits, because after
    /// that there is nothing left running to write it.
    /// </summary>
    public static void Handing(string version)
    {
        lock (Gate)
        {
            Write(new UpdateAttempt(version, FailuresAgainst(version), DateTimeOffset.Now, HandedOff: true));
        }
    }

    /// <summary>
    /// Settle a handoff this process did not see the end of, returning the
    /// attempt that failed or null if nothing is outstanding.
    ///
    /// Called on every launch and not only on the one the update script makes.
    /// A handoff outlives whatever ends the tray, so a reboot or a crash in the
    /// middle of one has to settle it too, or a version stays pending and is
    /// handed over again unbounded.
    /// </summary>
    public static UpdateAttempt? Settle(string installedVersion)
    {
        lock (Gate)
        {
            var attempt = Read();
            if (attempt is not { HandedOff: true }) return null;

            if (string.Equals(attempt.Version, installedVersion, StringComparison.Ordinal))
            {
                // What was handed over is what is running, so msiexec did the
                // work and there is nothing to hold against it.
                Clear();
                return null;
            }

            var failed = Failed(attempt.Version);
            TrayLog.Write(
                $"OpenShrimp {failed.Version} was handed to the installer and this build is still "
                + $"{installedVersion}, so it did not install (failure {failed.Failures}); "
                + $"leaving it for {Roughly(Backoff(failed))}");
            return failed;
        }
    }

    /// <summary>
    /// Count a failure against <paramref name="version"/> and return the record
    /// that results. Reached only from <see cref="Settle"/>: a handoff
    /// that came back at the old version is the whole definition of a failed
    /// install here, and no other event is allowed to add to the count.
    /// </summary>
    private static UpdateAttempt Failed(string version)
    {
        lock (Gate)
        {
            var settled = new UpdateAttempt(
                version, FailuresAgainst(version) + 1, DateTimeOffset.Now, HandedOff: false);
            Write(settled);
            return settled;
        }
    }

    /// <summary>What the balloon says about an install that did not take.</summary>
    public static string Describe(UpdateAttempt attempt)
    {
        var named = attempt.Version.Length > 0 ? attempt.Version : "the update";
        return $"OpenShrimp {named} could not be installed; still on {TrayVersion.Current}. "
               + $"Trying again in {Roughly(Backoff(attempt))}.";
    }

    /// <summary>How long <paramref name="attempt"/> holds its version off.</summary>
    public static TimeSpan Backoff(UpdateAttempt attempt) =>
        Backoffs[Math.Clamp(attempt.Failures, 1, Backoffs.Length) - 1];

    /// <summary>
    /// A duration as a balloon and a log line want it. Nothing here is precise
    /// enough to be worth minutes and seconds — the schedule around it is six
    /// hours wide.
    /// </summary>
    public static string Roughly(TimeSpan wait)
    {
        if (wait.TotalMinutes < 90) return $"{Math.Max(1, (int)Math.Round(wait.TotalMinutes))} minutes";
        return $"{Math.Max(2, (int)Math.Round(wait.TotalHours))} hours";
    }

    /// <summary>What is already held against this version, or zero for any other.</summary>
    private static int FailuresAgainst(string version)
    {
        var attempt = Read();
        return attempt is not null && string.Equals(attempt.Version, version, StringComparison.Ordinal)
            ? attempt.Failures
            : 0;
    }

    private static UpdateAttempt? Read()
    {
        try
        {
            if (!File.Exists(FilePath)) return null;
            return JsonSerializer.Deserialize<UpdateAttempt>(
                File.ReadAllText(FilePath), ControlJson.Options);
        }
        catch (Exception ex)
        {
            // Unreadable is treated as absent, deliberately: see the type
            // remarks on which direction this fails in.
            TrayLog.Write("Could not read what was remembered about the last update", ex);
            return null;
        }
    }

    private static void Write(UpdateAttempt attempt)
    {
        try
        {
            Directory.CreateDirectory(CorePaths.StateDirectory);
            File.WriteAllText(FilePath, JsonSerializer.Serialize(attempt, ControlJson.Options));
        }
        catch (Exception ex)
        {
            // Nothing else bounds the retry, so a machine that cannot write this
            // is a machine that can loop. Worth a line even though there is
            // nothing to be done about it from here.
            TrayLog.Write("Could not record the update attempt", ex);
        }
    }

    private static void Clear()
    {
        try
        {
            File.Delete(FilePath);
        }
        catch (Exception ex)
        {
            // A record left behind after a successful install names the version
            // now running, and nothing is ever held against that one.
            TrayLog.Write("Could not clear what was remembered about the last update", ex);
        }
    }
}
